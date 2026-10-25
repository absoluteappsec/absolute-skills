import functools
import json
import urllib.request

import pytest
from playwright.async_api import async_playwright

from appmap.cli import main
from appmap.crawl import build_runtime
from appmap.direct import DirectClient
from appmap.frontier import Frontier
from appmap.probe.replay import classify_outcome, forced_browse, run_active
from appmap.recording import record_exchange
from appmap.store import Store

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150}
LOGIN = "http://h/login"


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture
async def api():
    async with async_playwright() as p:
        ctx = await p.request.new_context()
        yield ctx
        await ctx.dispose()


def q(rd, sql, *args):
    store = Store(rd.db_path)
    try:
        return [dict(r) for r in store.conn.execute(sql, args)]
    finally:
        store.close()


def seed(cfg, rd, *, role, method, url, status=200, req_body=None, req_ct=None,
         resp_body=b"", resp_ct="text/html", origin="ui"):
    """Record a captured exchange as if the crawler had seen it (no network)."""
    rt = build_runtime(cfg, rd)
    try:
        req_h = {"content-type": req_ct} if req_ct else {}
        req_h["authorization"] = "Bearer seeded-token"
        return record_exchange(rt.store, rt.events, rt.redactor, rd, role=role, origin=origin,
                               method=method, url=url, req_headers=req_h,
                               req_body=req_body, status=status,
                               resp_headers={"content-type": resp_ct}, resp_body=resp_body,
                               duration_ms=1.0, caused_by=None, known_patterns=[])
    finally:
        rt.store.close()


def seed_endpoint(cfg, rd, host, method, pattern, source, kind="rest", **kw):
    store = Store(rd.db_path)
    store.init_schema()
    try:
        eid = store.upsert_endpoint(method, host, pattern, kind, **kw)
        store.upsert_source("endpoint", eid, source)
        return eid
    finally:
        store.close()


def access(rd, kind, where, *args):
    rows = q(rd, f"SELECT ra.role, ra.outcome FROM role_access ra JOIN "
                 f"{'routes' if kind == 'route' else 'endpoints'} x ON x.id=ra.item_id "
                 f"WHERE ra.item_kind='{kind}' AND {where}", *args)
    return {r["role"]: r["outcome"] for r in rows}


def _json_ok():
    from werkzeug.wrappers import Response
    return Response('{"ok": true}', mimetype="application/json")


def spa_roles(url):
    return [{"name": n, "login": {"type": "form", "url": f"{url}/",
                                  "username_env": f"APPMAP_{n.upper()}_USER",
                                  "password_env": f"APPMAP_{n.upper()}_PASS",
                                  "success_selector": "text=Signed in as"}}
            for n in ("admin", "alice")] + [{"name": "anonymous", "login": {"type": "none"}}]


@pytest.mark.parametrize("args,want", [
    ((200, None, 200, "http://h/admin", None, LOGIN), "allowed"),
    ((200, {"a": "int"}, 200, "http://h/x", {"a": "int"}, LOGIN), "allowed"),
    ((200, {"a": "int"}, 200, "http://h/x", {"b": "str"}, LOGIN), "different_content"),
    ((200, {"a": "int"}, 200, "http://h/x", None, LOGIN), "different_content"),
    ((403, None, 200, "http://h/x", {"a": "int"}, LOGIN), "allowed"),
    ((200, None, 200, "http://h/login", None, LOGIN), "redirect_login"),
    ((200, None, 302, "http://h/login?next=/admin", None, LOGIN), "redirect_login"),
    ((200, None, 200, "http://h/users/sign_in", None, None), "redirect_login"),
    ((200, None, 401, "http://h/api/x", None, LOGIN), "redirect_login"),
    ((200, None, 403, "http://h/admin", None, LOGIN), "forbidden"),
    ((200, None, 404, "http://h/admin", None, LOGIN), "not_found"),
    ((200, None, 302, "http://h/", None, LOGIN), "different_content"),
    ((200, None, 500, "http://h/x", None, LOGIN), "error"),
    ((200, None, 400, "http://h/x", None, LOGIN), "error"),
])
def test_classify_outcome(args, want):
    assert classify_outcome(*args) == want


async def test_alice_admin_access(browser, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    seed(cfg, rd, role="admin", method="GET", url=ssr_url + "/admin",
         resp_body=b"<html><h1>Admin</h1></html>")
    store = Store(rd.db_path)
    store.upsert_route("/admin", ssr_url.split("//")[1], "page")
    store.close()
    res = await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert res.counts["timed_out"] == 0 and not res.warnings
    got = access(rd, "route", "x.pattern='/admin'")
    assert got == {"admin": "allowed", "alice": "forbidden", "anonymous": "redirect_login"}
    assert access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/admin'") == got
    # /projects was only seen in admin's login flow: tested with a fresh GET, not a replay
    assert access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/projects'") == {
        "admin": "allowed", "alice": "allowed", "anonymous": "redirect_login"}
    # evidence points at the cross-role request itself
    ev = q(rd, "SELECT e.status, e.origin FROM role_access ra JOIN exchanges e "
               "ON e.id=ra.evidence_exchange_id WHERE ra.item_kind='route' AND ra.role='alice'")
    assert ev == [{"status": 403, "origin": "cross_role"}]
    # login endpoints are never replayed
    assert not q(rd, "SELECT 1 FROM exchanges WHERE url LIKE '%/login' AND method='POST' "
                     "AND origin IN ('replay','cross_role','schema')")
    # api replay as admin: OPTIONS + HEAD
    methods = {r["method"] for r in q(rd, "SELECT method FROM exchanges WHERE origin='replay'")}
    assert {"OPTIONS", "HEAD"} <= methods
    # idempotent: a re-run sends nothing new
    before = q(rd, "SELECT count(*) n FROM exchanges WHERE origin IN "
                   "('replay','cross_role','schema')")[0]["n"]
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    after = q(rd, "SELECT count(*) n FROM exchanges WHERE origin IN "
                  "('replay','cross_role','schema')")[0]["n"]
    assert after == before


async def test_expired_session_relogin(browser, ssr_url, make_cfg, run_dir, tmp_path):
    from appmap.cli import _auth

    cfg = make_cfg(ssr_url, limits=FAST, discovery={"api_replay": False})
    rd = run_dir(tmp_path, cfg)
    seed(cfg, rd, role="admin", method="GET", url=ssr_url + "/admin",
         resp_body=b"<html><h1>Admin</h1></html>")
    assert all((await _auth(cfg, rd, cfg.roles, headed=False)).values())
    urllib.request.urlopen(urllib.request.Request(ssr_url + "/__expire", method="POST",
                                                  data=b"")).read()
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    got = access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/admin'")
    assert got["alice"] == "forbidden" and got["anonymous"] == "redirect_login"
    events = [json.loads(line) for line in rd.events_path.read_text().splitlines()]
    assert any(e["type"] == "session_lost" and e.get("role") == "alice" for e in events)


async def test_cross_role_put_not_held_delete_held(browser, spa_url, make_cfg, run_dir,
                                                   tmp_path):
    cfg = make_cfg(spa_url, roles=spa_roles(spa_url), limits=FAST)
    rd = run_dir(tmp_path, cfg)
    host = spa_url.split("//")[1]
    seed(cfg, rd, role="admin", method="PUT", url=spa_url + "/api/items/3",
         req_body=b'{"title": "appmap-renamed"}', req_ct="application/json",
         resp_body=b'{"id": 3, "title": "appmap-renamed", "owner": "admin"}',
         resp_ct="application/json")
    seed_endpoint(cfg, rd, host, "POST", "/graphql", "bundle", kind="graphql_op",
                  graphql_op="DeleteItem", graphql_type="mutation")
    seed_endpoint(cfg, rd, host, "DELETE", "/api/items/{id}", "openapi")
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    puts = q(rd, "SELECT role, status FROM exchanges WHERE method='PUT' AND origin='cross_role'")
    assert {"role": "alice", "status": 200} in puts  # bearer learned from alice's session
    got = access(rd, "endpoint", "x.method='PUT' AND x.path_pattern='/api/items/{id}'")
    assert got == {"admin": "allowed", "alice": "allowed", "anonymous": "redirect_login"}
    approvals = q(rd, "SELECT label, category, predicted_request FROM approvals")
    gql = [a for a in approvals if "DeleteItem" in a["label"]]
    assert len(gql) == 1 and gql[0]["category"] == "delete"  # queued once, not per role
    deletes = [a for a in approvals
               if json.loads(a["predicted_request"] or "{}").get("method") == "DELETE"]
    assert len(deletes) == 1 and deletes[0]["category"] == "delete"
    assert not q(rd, "SELECT 1 FROM exchanges WHERE method='DELETE'")
    assert not q(rd, "SELECT 1 FROM exchanges WHERE req_body LIKE '%DeleteItem%'")
    held = access(rd, "endpoint", "x.method='DELETE'")
    assert set(held.values()) == {"not_tested"} and len(held) == 3
    assert rd.approvals_path.exists()


async def test_internal_flags_exercised(browser, spa_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(spa_url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   limits=FAST)
    rd = run_dir(tmp_path, cfg)
    host = spa_url.split("//")[1]
    seed_endpoint(cfg, rd, host, "GET", "/api/internal/flags", "bundle")
    seed_endpoint(cfg, rd, host, "GET", "/api/login", "bundle")
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    rows = q(rd, "SELECT x.status FROM exchanges x JOIN endpoints e ON e.id=x.endpoint_id "
                 "WHERE e.path_pattern='/api/internal/flags' AND x.origin='schema'")
    assert rows == [{"status": 200}]
    assert access(rd, "endpoint", "x.path_pattern='/api/internal/flags'") == {
        "anonymous": "allowed"}
    assert not q(rd, "SELECT 1 FROM exchanges WHERE url LIKE '%/api/login%'")


async def test_production_replay_post_held(browser, stub_server, make_cfg, run_dir, tmp_path):
    srv = stub_server({"/api/things": lambda r: _json_ok()})
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   profile="production")
    rd = run_dir(tmp_path, cfg)
    seed(cfg, rd, role="anonymous", method="POST", url=srv.url + "/api/things",
         req_body=b'{"name": "appmap-x"}', req_ct="application/json",
         resp_body=b'{"ok": true}', resp_ct="application/json")
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    held = q(rd, "SELECT category, predicted_request FROM approvals")
    assert len(held) == 1 and held[0]["category"] == "data_create"
    pr = json.loads(held[0]["predicted_request"])
    assert pr["method"] == "POST" and pr["url"] == srv.url + "/api/things"
    assert pr["origin"] == "replay"
    assert ("POST", "/api/things") not in srv.requests
    assert ("OPTIONS", "/api/things") in srv.requests


async def test_redacted_body_not_replayed(browser, stub_server, make_cfg, run_dir, tmp_path):
    srv = stub_server({"/api/profile": lambda r: _json_ok()})
    roles = [{"name": "anonymous", "login": {"type": "none"}},
             {"name": "guest", "login": {"type": "none"}}]
    cfg = make_cfg(srv.url, roles=roles, limits=FAST, discovery={"api_replay": False})
    rd = run_dir(tmp_path, cfg)
    seed(cfg, rd, role="anonymous", method="POST", url=srv.url + "/api/profile",
         req_body=b'{"api_key": "k-123", "name": "x"}', req_ct="application/json",
         resp_body=b'{"ok": true}', resp_ct="application/json")
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert access(rd, "endpoint", "x.path_pattern='/api/profile'") == {
        "anonymous": "allowed", "guest": "not_tested"}
    assert srv.requests == []


async def test_forced_browse_finds_secret_report(api, ssr_url, make_cfg, run_dir, tmp_path):
    words = tmp_path / "words.txt"
    words.write_text("# operator list\nsecret-report\nno-such-thing\n")
    cfg = make_cfg(ssr_url, limits=FAST, discovery={
        "forced_browsing": True, "passive_sources": False, "wordlists": [str(words)]})
    rd = run_dir(tmp_path, cfg)
    rt = build_runtime(cfg, rd)
    try:
        rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                                known_patterns=[])
        client = DirectClient(rt.gate, rec, rt.events)
        counts = await forced_browse(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
        assert counts["requests"] >= 100  # built-in list + operator list
        routes = {r[0]: r[1] for r in rt.store.conn.execute(
            "SELECT r.pattern, s.source FROM routes r JOIN sources s "
            "ON s.item_kind='route' AND s.item_id=r.id")}
        assert routes.get("/secret-report") == "wordlist"
        assert routes.get("/admin") == "wordlist"  # built-in word; redirects for anonymous
        assert "/no-such-thing" not in routes
        for role in ("admin", "alice", "anonymous"):
            assert rt.store.conn.execute(
                "SELECT count(*) FROM frontier WHERE role=? AND url=? AND source='wordlist'",
                (role, ssr_url + "/secret-report")).fetchone()[0] == 1
        again = await forced_browse(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
        assert again["requests"] == 0
    finally:
        rt.store.close()


async def test_forced_browse_off_by_default(api, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url)
    rd = run_dir(tmp_path, cfg)
    rt = build_runtime(cfg, rd)
    try:
        counts = await forced_browse(None, api, cfg, rt.store, Frontier(rt.store), "anonymous")
        assert counts["requests"] == 0
    finally:
        rt.store.close()


def test_cli_probe_active(ssr_url, make_cfg, tmp_path, capsys):
    cfg = make_cfg(ssr_url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   limits=FAST)
    out = tmp_path / "runs"
    assert main(["init", "--config", str(cfg.source_path), "--out", str(out)]) == 0
    run = next(out.iterdir())
    capsys.readouterr()
    assert main(["probe", "--run", str(run), "--phase", "passive"]) == 0
    capsys.readouterr()
    assert main(["probe", "--run", str(run), "--phase", "active"]) == 0
    st = json.loads(capsys.readouterr().out.strip().splitlines()[-1].removeprefix("APPMAP_STATUS "))
    assert st["ok"] and st["status"] == "done" and st["command"] == "probe"
    assert st["counts"]["requests"] > 0 and st["counts"]["role_access"] > 0
    assert st["next"].startswith("appmap ")
    assert main(["probe", "--run", str(run), "--phase", "active", "--budget-minutes", "0"]) == 0
    st = json.loads(capsys.readouterr().out.strip().splitlines()[-1].removeprefix("APPMAP_STATUS "))
    assert st["status"] == "in_progress" and "--phase active" in st["next"]
