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
         resp_body=b"", resp_ct="text/html", origin="ui", source=None, resp_headers=None):
    """Record a captured exchange as if the crawler had seen it (no network)."""
    rt = build_runtime(cfg, rd)
    try:
        req_h = {"content-type": req_ct} if req_ct else {}
        req_h["authorization"] = "Bearer seeded-token"
        return record_exchange(rt.store, rt.events, rt.redactor, rd, role=role, origin=origin,
                               method=method, url=url, req_headers=req_h,
                               req_body=req_body, status=status,
                               resp_headers={"content-type": resp_ct, **(resp_headers or {})},
                               resp_body=resp_body, duration_ms=1.0, caused_by=None,
                               known_patterns=[], source=source)
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
    ((200, None, 400, "http://h/x", None, LOGIN), "different_content"),
    ((200, None, 422, "http://h/x", None, LOGIN), "different_content"),
    ((200, None, 405, "http://h/x", None, LOGIN), "error"),
    ((200, None, 502, "http://h/x", None, LOGIN), "error"),
])
def test_classify_outcome(args, want):
    assert classify_outcome(*args) == want


def test_classify_outcome_followed_redirect():
    # a followed redirect to a different, non-login page is a denial, not "allowed"
    assert classify_outcome(200, None, 200, "http://h/", None, LOGIN,
                            requested_url="http://h/admin/reports") == "different_content"
    assert classify_outcome(200, None, 200, "http://h/admin/reports/", None, LOGIN,
                            requested_url="http://h/admin/reports") == "allowed"
    assert classify_outcome(200, None, 200, "http://h/login", None, LOGIN,
                            requested_url="http://h/admin/reports") == "redirect_login"


async def test_alice_admin_access(browser, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    seed(cfg, rd, role="admin", method="GET", url=ssr_url + "/admin",
         resp_body=b"<html><h1>Admin</h1></html>")
    seed(cfg, rd, role="admin", method="GET", url=ssr_url + "/admin/reports",
         resp_body=b"<html><h1>Reports</h1></html>")
    store = Store(rd.db_path)
    store.upsert_route("/admin", ssr_url.split("//")[1], "page")
    store.close()
    res = await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert res.counts["timed_out"] == 0 and not res.warnings
    got = access(rd, "route", "x.pattern='/admin'")
    assert got == {"admin": "allowed", "alice": "forbidden", "anonymous": "redirect_login"}
    assert access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/admin'") == got
    # alice is redirected home (followed redirect): a denial, not "allowed"
    assert access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/admin/reports'") == {
        "admin": "allowed", "alice": "different_content", "anonymous": "redirect_login"}
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


async def test_forbidden_401_with_live_session_is_not_a_loss(browser, ssr_url, make_cfg,
                                                              run_dir, tmp_path):
    """Field fix 2: alice gets 401 for a project she does not own. Her session is
    confirmed alive (a known-authenticated probe), so the 401 is ``forbidden``: no
    session_lost, no re-login, and her loss detection still works afterwards."""
    from appmap.cli import _auth

    cfg = make_cfg(ssr_url, limits=FAST, discovery={"api_replay": False})
    rd = run_dir(tmp_path, cfg)
    for pid in (1, 2, 3):
        seed(cfg, rd, role="admin", method="GET", url=f"{ssr_url}/projects/{pid}/private",
             resp_body=b"<html><h1>Private notes</h1></html>")
    # what a crawl leaves behind: alice saw /projects, anonymous was sent to login
    seed(cfg, rd, role="alice", method="GET", url=ssr_url + "/projects")
    seed(cfg, rd, role="anonymous", method="GET", url=ssr_url + "/projects", status=302,
         resp_headers={"location": "/login"})
    assert all((await _auth(cfg, rd, cfg.roles, headed=False)).values())
    res = await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert not [w for w in res.warnings if "landing page" in w]  # evidence, not landing
    got = access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/projects/{id}/private'")
    assert got["admin"] == "allowed" and got["alice"] == "forbidden"
    assert got["anonymous"] == "redirect_login"
    events = [json.loads(line) for line in rd.events_path.read_text().splitlines()]
    assert not [e for e in events if e["type"] == "session_lost" and e.get("role") == "alice"]
    assert sum(1 for e in events if e["type"] == "login" and e.get("role") == "alice") == 1
    assert [e for e in events if e["type"] == "observation" and e.get("role") == "alice"
            and e["data"].get("kind") == "access_denied"]


def test_auth_probe_never_uses_approved_or_held_gets(ssr_url, make_cfg, run_dir, tmp_path):
    """Fix round 1: a liveness probe must never re-send an approved (destructive) GET, a
    GET the classifier holds, or a logout URL (query included), even with 2xx + 401
    evidence on the same endpoint."""
    from types import SimpleNamespace

    from appmap.browser.session import auth_probe_urls

    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    for url, origin in ((ssr_url + "/projects/7/remove", "approved"),
                        (ssr_url + "/items/9/delete", "ui"),
                        (ssr_url + "/go?action=logout", "ui"),
                        (ssr_url + "/dashboard", "login")):
        seed(cfg, rd, role="alice", method="GET", url=url, origin=origin)
        seed(cfg, rd, role="anonymous", method="GET", url=url, status=401)
    seed(cfg, rd, role="alice", method="GET", url=ssr_url + "/projects", origin="ui")
    seed(cfg, rd, role="anonymous", method="GET", url=ssr_url + "/projects", status=401)
    sess = SimpleNamespace(role=SimpleNamespace(name="alice"), landing_url=None)
    store = Store(rd.db_path)
    try:
        got = auth_probe_urls(sess, store, ssr_url + "/login", profile=cfg.profile, limit=10)
    finally:
        store.close()
    assert [u for u, _ in got] == [ssr_url + "/projects"]


async def test_spa_expiry_without_evidence_is_not_silently_forbidden(browser, spa_url, make_cfg,
                                                                      run_dir, tmp_path):
    """Fix round 1: a direct GET of an SPA's landing page answers 200 HTML whether or not
    the token is valid, so with no session-gated evidence liveness is inconclusive: an
    expired session re-logs in (bounded by C1) instead of turning every 401 into
    ``forbidden``."""
    from appmap.cli import _auth

    cfg = make_cfg(spa_url, roles=spa_roles(spa_url), limits=FAST,
                   discovery={"api_replay": False})
    rd = run_dir(tmp_path, cfg)
    seed(cfg, rd, role="admin", method="GET", url=spa_url + "/api/items",
         resp_body=b'{"items": [{"id": 1, "title": "Item 1", "owner": "admin"}], '
                   b'"next_cursor": "10"}', resp_ct="application/json")
    assert all((await _auth(cfg, rd, cfg.roles, headed=False)).values())
    from appmap.browser.session import state_path
    for role in ("admin", "alice"):  # the app landed on a client route after login
        p = state_path(rd, role)
        st = json.loads(p.read_text())
        st["appmap_landing_url"] = spa_url + "/items"
        p.write_text(json.dumps(st))
    urllib.request.urlopen(urllib.request.Request(spa_url + "/__expire", method="POST",
                                                  data=b"")).read()
    res = await run_active(cfg, rd, budget_minutes=5, browser=browser)
    got = access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/api/items'")
    assert got["alice"] == "allowed"
    events = [json.loads(line) for line in rd.events_path.read_text().splitlines()]
    assert any(e["type"] == "session_lost" and e.get("role") == "alice" for e in events)
    assert not [e for e in events if e["type"] == "observation"
                and e["data"].get("kind") == "access_denied"]


async def test_expired_session_relogin_after_forbidden(browser, ssr_url, make_cfg, run_dir,
                                                       tmp_path):
    """Genuine expiry is still confirmed lost by the probe: re-login, then the item's
    real answer (403 for alice on /admin)."""
    from appmap.cli import _auth

    cfg = make_cfg(ssr_url, limits=FAST, discovery={"api_replay": False})
    rd = run_dir(tmp_path, cfg)
    seed(cfg, rd, role="admin", method="GET", url=ssr_url + "/admin",
         resp_body=b"<html><h1>Admin</h1></html>")
    seed(cfg, rd, role="admin", method="GET", url=ssr_url + "/projects/1/private",
         resp_body=b"<html><h1>Private notes</h1></html>")
    seed(cfg, rd, role="alice", method="GET", url=ssr_url + "/projects")  # crawl evidence
    seed(cfg, rd, role="anonymous", method="GET", url=ssr_url + "/projects", status=302,
         resp_headers={"location": "/login"})
    assert all((await _auth(cfg, rd, cfg.roles, headed=False)).values())
    urllib.request.urlopen(urllib.request.Request(ssr_url + "/__expire", method="POST",
                                                  data=b"")).read()
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert access(rd, "endpoint", "x.method='GET' AND x.path_pattern='/admin'")["alice"] \
        == "forbidden"
    assert access(rd, "endpoint", "x.method='GET' AND "
                  "x.path_pattern='/projects/{id}/private'")["alice"] == "forbidden"
    events = [json.loads(line) for line in rd.events_path.read_text().splitlines()]
    assert any(e["type"] == "session_lost" and e.get("role") == "alice" for e in events)
    assert sum(1 for e in events if e["type"] == "login" and e.get("role") == "alice"
               and e["data"].get("ok")) == 2


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
    # the bearer tokens the roles used never reach the run directory
    from appmap.browser.session import state_path
    tokens = []
    for role in ("admin", "alice"):
        st = json.loads(state_path(rd, role).read_text())
        tokens += [i["value"] for o in st.get("origins", []) for i in o.get("localStorage", [])
                   if i["name"] == "token"]
    assert tokens
    for f in rd.root.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert not any(t.encode() in data for t in tokens), f


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


async def test_forced_browse_refused_in_production(api, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url, profile="production")
    cfg.discovery.forced_browsing = True  # config validation rejects this; defence in depth
    rd = run_dir(tmp_path, cfg)
    rt = build_runtime(cfg, rd)
    try:
        counts = await forced_browse(None, api, cfg, rt.store, Frontier(rt.store), "anonymous")
        assert counts["requests"] == 0
    finally:
        rt.store.close()


def test_builtin_wordlist_has_no_setup_paths(make_cfg, ssr_url):
    from appmap.probe.replay import load_wordlist
    words = load_wordlist(make_cfg(ssr_url))
    assert len(words) >= 150 and not {"install", "installer", "setup", "logout"} & set(words)


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


def _members_app(log):
    from werkzeug.wrappers import Request, Response

    @Request.application
    def app(request):
        log.append((request.method, request.path))
        return Response('{"ok": true}', mimetype="application/json")
    return app


async def test_approval_flagged_actions_never_replayed(browser, stub_server, make_cfg, run_dir,
                                                       tmp_path):
    srv = stub_server(_members_app([]))
    roles = [{"name": "anonymous", "login": {"type": "none"}},
             {"name": "guest", "login": {"type": "none"}}]
    cfg = make_cfg(srv.url, roles=roles, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    form = "application/x-www-form-urlencoded"
    # an approved (crawl-flagged) action's exchange, benign-looking URL
    seed(cfg, rd, role="anonymous", method="POST", url=srv.url + "/members/5",
         req_body=b"name=x", req_ct=form, origin="approved", resp_ct="application/json",
         resp_body=b'{"ok": true}')
    # a captured POST whose URL an approval row references (any decision)
    seed(cfg, rd, role="anonymous", method="POST", url=srv.url + "/teams/7",
         req_body=b"name=y", req_ct=form, resp_ct="application/json", resp_body=b'{"ok": true}')
    store = Store(rd.db_path)
    store.upsert_approval("ap-0001", role="anonymous", category="delete", label="Remove member",
                          reasons=["label matches delete keyword"], action_path=[],
                          predicted_request={"method": "POST", "url": srv.url + "/teams/7"},
                          decision="approve")
    store.close()
    # destructive only by its body (Rails-style method override)
    seed(cfg, rd, role="anonymous", method="POST", url=srv.url + "/groups/9",
         req_body=b"_method=delete&id=9", req_ct=form, resp_ct="application/json",
         resp_body=b'{"ok": true}')
    # logout: captured and schema-only
    seed(cfg, rd, role="anonymous", method="GET", url=srv.url + "/signout")
    seed_endpoint(cfg, rd, srv.url.split("//")[1], "GET", "/api/logout", "bundle")
    srv.requests.clear()
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    sent = [r for r in srv.requests if r[0] not in ("OPTIONS", "HEAD")]
    assert not [r for r in sent if r[1].startswith(("/members", "/teams", "/groups"))], sent
    assert not [r for r in srv.requests if "sign" in r[1] or "logout" in r[1]]
    for path in ("/members/{id}", "/teams/{id}", "/groups/{id}", "/signout"):
        assert access(rd, "endpoint", "x.path_pattern=?", path)["guest"] == "not_tested", path
    approvals = q(rd, "SELECT category, predicted_request FROM approvals")
    assert not [a for a in approvals if a["category"] == "logout"]
    urls = [json.loads(a["predicted_request"])["url"] for a in approvals]
    assert urls.count(srv.url + "/teams/7") == 1  # not re-queued
    assert srv.url + "/members/5" not in urls
    assert [a["category"] for a in approvals
            if json.loads(a["predicted_request"])["url"] == srv.url + "/groups/9"] == ["delete"]


async def test_cross_role_sent_despite_own_exchange(browser, stub_server, make_cfg, run_dir,
                                                    tmp_path):
    srv = stub_server(_members_app([]))
    roles = [{"name": "anonymous", "login": {"type": "none"}},
             {"name": "guest", "login": {"type": "none"}}]
    cfg = make_cfg(srv.url, roles=roles, limits=FAST, discovery={"api_replay": False})
    rd = run_dir(tmp_path, cfg)
    for role in ("anonymous", "guest"):  # both saw /api/docs/{id}, for different objects
        seed(cfg, rd, role=role, method="GET",
             url=srv.url + ("/api/docs/1" if role == "anonymous" else "/api/docs/2"),
             resp_ct="application/json", resp_body=b'{"ok": true}')
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert ("GET", "/api/docs/1") in srv.requests  # guest requested anonymous's object
    assert access(rd, "endpoint", "x.path_pattern='/api/docs/{id}'") == {
        "anonymous": "allowed", "guest": "allowed"}


async def test_discovering_role_outcome_from_its_own_status(browser, stub_server, make_cfg,
                                                            run_dir, tmp_path):
    srv = stub_server(_members_app([]))
    roles = [{"name": "anonymous", "login": {"type": "none"}},
             {"name": "guest", "login": {"type": "none"}}]
    cfg = make_cfg(srv.url, roles=roles, limits=FAST, discovery={"api_replay": False})
    rd = run_dir(tmp_path, cfg)
    # a forced-browsing hit: anonymous got 403, recorded as a passive/wordlist exchange
    seed(cfg, rd, role="anonymous", method="GET", url=srv.url + "/backup", status=403,
         origin="passive", source="wordlist")
    seed(cfg, rd, role="anonymous", method="GET", url=srv.url + "/console", status=302,
         origin="passive", source="wordlist",
         resp_headers={"location": "/login?next=/console"})
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert access(rd, "endpoint", "x.path_pattern='/backup'")["anonymous"] == "forbidden"
    assert access(rd, "endpoint", "x.path_pattern='/console'")["anonymous"] == "redirect_login"


async def test_method_override_approval_blocks_post(browser, stub_server, make_cfg, run_dir,
                                                    tmp_path):
    srv = stub_server(_members_app([]))
    roles = [{"name": "anonymous", "login": {"type": "none"}},
             {"name": "guest", "login": {"type": "none"}}]
    cfg = make_cfg(srv.url, roles=roles, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    url = srv.url + "/projects/3"
    seed(cfg, rd, role="anonymous", method="POST", url=url, req_body=b"name=x",
         req_ct="application/x-www-form-urlencoded", resp_ct="application/json",
         resp_body=b'{"ok": true}')
    store = Store(rd.db_path)
    store.upsert_approval("ap-0001", role="anonymous", category="delete", label="Remove",
                          reasons=["label matches delete keyword"],
                          action_path=[{"type": "goto", "url": url},
                                       {"type": "submit", "form_label": "Remove",
                                        "method": "DELETE", "action": url}],
                          predicted_request={"method": "DELETE", "url": url},
                          decision="pending")
    store.close()
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    assert ("POST", "/projects/3") not in srv.requests
    assert access(rd, "endpoint", "x.method='POST' AND x.path_pattern='/projects/{id}'") == {
        "anonymous": "allowed", "guest": "not_tested"}


async def test_replay_uses_ids_created_by_the_role(browser, spa_url, make_cfg, run_dir,
                                                   tmp_path):
    """Spec 'API replay': IDs observed in other responses include records the role's own
    writes created, ahead of list ids (Task 22 e2e: POST /api/items -> GET /api/items/{id})."""
    cfg = make_cfg(spa_url, roles=spa_roles(spa_url)[:1], limits=FAST)
    rd = run_dir(tmp_path, cfg)
    page = json.dumps({"items": [{"id": i, "title": f"Item {i}"} for i in range(1, 11)],
                       "next_cursor": "10"}).encode()
    seed(cfg, rd, role="admin", method="GET", url=spa_url + "/api/items?cursor=",
         resp_body=page, resp_ct="application/json")
    seed(cfg, rd, role="admin", method="GET", url=spa_url + "/api/items/1",
         resp_body=b'{"id": 1, "title": "Item 1", "owner": "admin"}',
         resp_ct="application/json")
    seed(cfg, rd, role="admin", method="POST", url=spa_url + "/api/items", status=201,
         req_body=b'{"title": "appmap-x"}', req_ct="application/json",
         resp_body=b'{"id": 101, "title": "appmap-x", "owner": "admin"}',
         resp_ct="application/json")
    await run_active(cfg, rd, budget_minutes=5, browser=browser)
    urls = {r["url"] for r in q(rd, "SELECT url FROM exchanges WHERE origin='replay' "
                                    "AND method='GET' AND role='admin'")}
    assert spa_url + "/api/items/101" in urls, sorted(urls)
