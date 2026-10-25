"""Task 17: ``appmap approvals`` and ``crawl --approved-only`` (approved-action execution)."""
import json
import re

import pytest
import werkzeug
import yaml

from appmap.approvals import ApprovalQueue, PendingAction
from appmap.cli import main
from appmap.crawl import run_crawl
from appmap.store import Store

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150,
        "max_states_per_route_pattern": 3}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def _status(capsys) -> dict:
    out = capsys.readouterr().out.strip().splitlines()
    assert out[-1].startswith("APPMAP_STATUS ")
    return json.loads(out[-1].removeprefix("APPMAP_STATUS "))


def _lines(capsys) -> tuple[list[str], dict]:
    out = capsys.readouterr().out.strip().splitlines()
    return out[:-1], json.loads(out[-1].removeprefix("APPMAP_STATUS "))


def q(root, sql, *args):
    store = Store(root / "appmap.sqlite")
    try:
        return [dict(r) for r in store.conn.execute(sql, args)]
    finally:
        store.close()


def _init(make_cfg, url, tmp_path, capsys, **kw):
    cfg = make_cfg(url, limits=FAST, **kw)
    out = tmp_path / "runs"
    assert main(["init", "--config", str(cfg.source_path), "--out", str(out)]) == 0
    capsys.readouterr()
    return next(out.iterdir())


def _enqueue(root, *pas) -> list[str]:
    store = Store(root / "appmap.sqlite")
    try:
        store.init_schema()
        queue = ApprovalQueue(store, root / "approvals.yaml")
        return [queue.enqueue(pa) for pa in pas]
    finally:
        store.close()


def _pa(label, cat="delete", role="anonymous", path=None, pred=None):
    return PendingAction(id="", role=role, state_id=None, category=cat, label=label,
                         reasons=["r"], action_path=path or [], predicted_request=pred)


def _edit_yaml(root, decisions: dict | None = None, notes: dict | None = None,
               rules: list | None = None) -> None:
    p = root / "approvals.yaml"
    data = yaml.safe_load(p.read_text()) or {}
    for item in data.get("items") or []:
        if decisions and item["id"] in decisions:
            item["decision"] = decisions[item["id"]]
        if notes and item["id"] in notes:
            item["note"] = notes[item["id"]]
    if rules is not None:
        data["rules"] = rules
    p.write_text(yaml.safe_dump(data, sort_keys=False))


# -- end to end over the SSR fixture (CLI) -------------------------------------------
def test_apply_rule_and_execute(ssr_url, make_cfg, tmp_path, capsys):
    run = _init(make_cfg, ssr_url, tmp_path, capsys, roles=[
        {"name": "admin", "login": {"type": "form", "url": f"{ssr_url}/login",
                                    "username_env": "APPMAP_ADMIN_USER",
                                    "password_env": "APPMAP_ADMIN_PASS"}}])
    assert main(["crawl", "--run", str(run), "--role", "admin"]) == 0
    st = _status(capsys)
    assert st["status"] == "done" and st["pending_approvals"] >= 2

    assert main(["approvals", "--run", str(run), "write"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["command"] == "approvals" and st["counts"]["pending"] >= 2
    _edit_yaml(run, rules=[{"decision": "approve",
                            "match": {"category": "delete", "label_contains": "Delete"}}])
    assert main(["approvals", "--run", str(run), "apply"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["counts"]["approved"] >= 1
    assert st["next"].endswith("--approved-only")

    assert main(["crawl", "--run", str(run), "--approved-only"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["status"] == "done", st
    assert st["counts"]["executed"] >= 1 and st["counts"]["failed"] == 0, st

    deletes = q(run, "SELECT * FROM exchanges WHERE method='POST' AND url LIKE '%/delete'")
    assert deletes and all(d["origin"] == "approved" for d in deletes)
    assert all(re.search(r"/projects/\d+/delete$", d["url"]) for d in deletes)
    executed = q(run, "SELECT * FROM approvals WHERE category='delete' AND decision='approve'")
    assert executed and all(a["executed_event"] for a in executed)
    events = [json.loads(line) for line in (run / "events.jsonl").read_text().splitlines()]
    done = [e for e in events if e["type"] == "approval_executed"]
    assert {e["data"]["approval_id"] for e in done} == {a["id"] for a in executed}
    assert {a["executed_event"] for a in executed} == {e["id"] for e in done}
    # the logout item was never approved: still pending, never requested
    logout = q(run, "SELECT * FROM approvals WHERE category='logout'")
    assert logout and all(a["decision"] == "pending" and not a["executed_event"] for a in logout)
    assert not q(run, "SELECT 1 FROM exchanges WHERE url LIKE '%/logout'")
    # other held actions stay blocked
    assert not q(run, "SELECT 1 FROM exchanges WHERE method='POST' AND url LIKE '%/account/password'")
    # each approval runs at most once
    n = len(deletes)
    assert main(["crawl", "--run", str(run), "--approved-only"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["counts"]["executed"] == 0
    assert len(q(run, "SELECT * FROM exchanges WHERE method='POST' AND url LIKE '%/delete'")) == n
    # rule-approved decisions survive the yaml round trip
    data = yaml.safe_load((run / "approvals.yaml").read_text())
    assert {i["id"]: i["decision"] for i in data["items"]}[executed[0]["id"]] == "approve"


def test_write_preserves_decisions(ssr_url, make_cfg, tmp_path, capsys):
    run = _init(make_cfg, ssr_url, tmp_path, capsys)
    a, b, c = _enqueue(run, _pa("Delete a", pred={"method": "DELETE", "url": "http://t/a"}),
                       _pa("Delete b", pred={"method": "DELETE", "url": "http://t/b"}),
                       _pa("Delete c", pred={"method": "DELETE", "url": "http://t/c"}))
    assert main(["approvals", "--run", str(run), "write"]) == 0
    assert _status(capsys)["counts"]["pending"] == 3
    _edit_yaml(run, decisions={a: "approve", b: "deny"}, notes={a: "ok to drop", c: "ask Bob"})
    _enqueue(run, _pa("Delete d", pred={"method": "DELETE", "url": "http://t/d"}))
    assert main(["approvals", "--run", str(run), "write"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["counts"]["pending"] == 2
    items = {i["id"]: i for i in yaml.safe_load((run / "approvals.yaml").read_text())["items"]}
    assert len(items) == 4
    assert items[a]["decision"] == "approve" and items[a]["note"] == "ok to drop"
    assert items[b]["decision"] == "deny"
    assert items[c]["decision"] == "pending" and items[c]["note"] == "ask Bob"

    assert main(["approvals", "--run", str(run), "list"]) == 0
    lines, st = _lines(capsys)
    listed = [json.loads(line) for line in lines]
    assert {i["id"] for i in listed} == {c, "ap-0004"}
    assert all(i["category"] == "delete" and "label" in i for i in listed)
    assert st["ok"] and st["pending_approvals"] == 2


def test_denied_items_reported(ssr_url, make_cfg, tmp_path, capsys):
    run = _init(make_cfg, ssr_url, tmp_path, capsys)
    a, b = _enqueue(run, _pa("Delete a", pred={"method": "DELETE", "url": "http://t/a"}),
                    _pa("Remove b", pred={"method": "DELETE", "url": "http://t/b"}))
    assert main(["approvals", "--run", str(run), "write"]) == 0
    capsys.readouterr()
    _edit_yaml(run, decisions={a: "deny"}, rules=[{"decision": "approve", "match": {"bogus": "x"}}])
    assert main(["approvals", "--run", str(run), "apply"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["counts"]["denied"] == 1 and st["counts"]["pending"] == 1
    assert st["pending_approvals"] == 1
    assert any("rule #1" in e for e in st["errors"])


def test_approvals_bad_run_reports_error(tmp_path, capsys):
    assert main(["approvals", "--run", str(tmp_path / "nope"), "list"]) == 1
    st = _status(capsys)
    assert not st["ok"] and st["command"] == "approvals" and st["errors"]


# -- execution semantics (stub apps) ---------------------------------------------------
def H(body, title="Items"):
    return lambda r: werkzeug.Response(
        f"<html><head><title>{title}</title></head><body>" + body + "</body></html>",
        mimetype="text/html")


def _decide(rd, decisions: dict) -> None:
    store = Store(rd.db_path)
    try:
        queue = ApprovalQueue(store, rd.approvals_path)
        queue.write_yaml()
        _edit_yaml(rd.root, decisions=decisions)
    finally:
        store.close()


def _anon(make_cfg, url):
    return make_cfg(url, roles=[{"name": "anonymous", "login": {"type": "none"}}], limits=FAST)


async def test_only_the_approved_request_goes_through(browser, stub_server, make_cfg, run_dir,
                                                      tmp_path):
    srv = stub_server({
        "/": H("<button onclick=\"fetch('/api/items/7',{method:'DELETE'});"
               "fetch('/api/items/8',{method:'DELETE'})\">Go</button>"),
        "/api/items/7": lambda r: "ok", "/api/items/8": lambda r: "ok"})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    rows = q(rd.root, "SELECT id, predicted_request FROM approvals")
    by_url = {json.loads(r["predicted_request"])["url"].rsplit("/", 1)[-1]: r["id"] for r in rows}
    assert set(by_url) == {"7", "8"} and not any(m == "DELETE" for m, _ in srv.requests)
    _decide(rd, {by_url["7"]: "approve"})

    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.status == "done" and stats.approvals["executed"] == 1
    assert ("DELETE", "/api/items/7") in srv.requests
    assert ("DELETE", "/api/items/8") not in srv.requests
    ex = q(rd.root, "SELECT origin FROM exchanges WHERE method='DELETE'")
    assert ex == [{"origin": "approved"}]
    assert q(rd.root, "SELECT decision, executed_event FROM approvals WHERE id=?",
             by_url["8"])[0] == {"decision": "pending", "executed_event": None}


async def test_confirm_accepted_only_for_approved_action(browser, stub_server, make_cfg,
                                                         run_dir, tmp_path):
    srv = stub_server({
        "/": H("<button onclick=\"if (confirm('Sure?')) fetch('/api/thing',{method:'POST'})\">"
               "Proceed</button><a href='/next'>Next</a>"),
        "/next": H("<p>after</p>", title="Next"),
        "/api/thing": lambda r: "ok"})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    assert ("POST", "/api/thing") not in srv.requests
    aid = q(rd.root, "SELECT id FROM approvals WHERE label='Proceed'")[0]["id"]
    _decide(rd, {aid: "approve"})
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals == {"executed": 1, "failed": 0, "skipped": 0}
    assert srv.requests.count(("POST", "/api/thing")) == 1
    assert q(rd.root, "SELECT origin FROM exchanges WHERE method='POST'") == [
        {"origin": "approved"}]
    events = [json.loads(line) for line in rd.events_path.read_text().splitlines()]
    ex = [e for e in events if e["type"] == "approval_executed"]
    assert len(ex) == 1 and ex[0]["data"]["approval_id"] == aid
    assert q(rd.root, "SELECT count(*) AS n FROM transitions WHERE action_label='Proceed'")[0]["n"] >= 1


async def test_direct_request_approval_sent_via_direct_client(browser, stub_server, make_cfg,
                                                              run_dir, tmp_path):
    srv = stub_server({"/": H("<p>home</p>"), "/api/items/9": lambda r: "gone"})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    url = srv.url + "/api/items/9"
    step = {"type": "request", "method": "DELETE", "url": url, "origin": "replay"}
    [aid] = _enqueue(rd.root, _pa(f"DELETE {url}", path=[step],
                                  pred={"method": "DELETE", "url": url, "origin": "replay"}))
    _decide(rd, {aid: "approve"})
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals["executed"] == 1
    assert srv.requests.count(("DELETE", "/api/items/9")) == 1
    assert q(rd.root, "SELECT origin, role FROM exchanges WHERE method='DELETE'") == [
        {"origin": "approved", "role": "anonymous"}]
    assert q(rd.root, "SELECT executed_event FROM approvals")[0]["executed_event"]


async def test_mutating_prefix_fails_and_logout_is_skipped(browser, stub_server, make_cfg,
                                                           run_dir, tmp_path):
    srv = stub_server({"/": H("<button onclick=\"fetch('/api/star',{method:'POST'})\">Star"
                              "</button><button>Remove</button>"),
                       "/api/star": lambda r: "ok", "/logout": lambda r: "bye"})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    loc = lambda text: [{"strategy": "text", "value": text, "rank": 0}]
    path = [{"type": "goto", "url": srv.url + "/"},
            {"type": "click", "label": "Star", "locators": loc("Star"), "mutating": True},
            {"type": "click", "label": "Remove", "locators": loc("Remove")}]
    bad, out = _enqueue(rd.root, _pa("Remove", path=path),
                        _pa("Logout", cat="logout", path=[{"type": "goto",
                                                           "url": srv.url + "/logout"}],
                            pred={"method": "GET", "url": srv.url + "/logout"}))
    _decide(rd, {bad: "approve", out: "approve"})
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.status == "done"
    assert stats.approvals == {"executed": 0, "failed": 1, "skipped": 1}
    assert srv.requests == []  # nothing restored, nothing sent
    assert any("mutating" in w for w in stats.warnings)
    assert any("logout" in w for w in stats.warnings)
    rows = {r["id"]: r for r in q(rd.root, "SELECT * FROM approvals")}
    assert rows[bad]["executed_event"] is None and rows[out]["decision"] == "skip"
    items = {i["id"]: i for i in yaml.safe_load(rd.approvals_path.read_text())["items"]}
    assert "mutating" in items[bad]["execution"]


# -- fix round 1 -------------------------------------------------------------------------
async def _crawl_and_approve(srv, make_cfg, run_dir, tmp_path, browser, pick):
    """Normal crawl, then approve the approvals ``pick(predicted_url)`` selects."""
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    rows = q(rd.root, "SELECT id, predicted_request FROM approvals")
    chosen = {r["id"]: "approve" for r in rows
              if pick(json.loads(r["predicted_request"] or "{}").get("url") or "")}
    assert chosen, rows
    _decide(rd, chosen)
    return cfg, rd


async def test_page_load_approval_resent_by_the_browser(browser, stub_server, make_cfg,
                                                        run_dir, tmp_path):
    """A request the guard caught during a page load is executed by reloading the page,
    so the real request (body + content type) is what goes out."""
    def delete(r):
        ok = r.mimetype == "application/json" and r.get_json(silent=True) == {"confirm": True}
        return werkzeug.Response("deleted" if ok else "bad", status=201 if ok else 400)

    srv = stub_server({
        "/": H("<script>fetch('/api/items/7/delete',{method:'POST',headers:"
               "{'Content-Type':'application/json'},body:JSON.stringify({confirm:true})})"
               "</script><p>hi</p>"),
        "/api/items/7/delete": delete})
    cfg, rd = await _crawl_and_approve(srv, make_cfg, run_dir, tmp_path, browser,
                                       lambda u: u.endswith("/api/items/7/delete"))
    assert ("POST", "/api/items/7/delete") not in srv.requests
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals["executed"] == 1, stats.warnings
    ex = q(rd.root, "SELECT origin, status, req_body FROM exchanges WHERE method='POST'")
    assert len(ex) == 1 and ex[0]["origin"] == "approved" and ex[0]["status"] == 201
    assert "confirm" in ex[0]["req_body"]


def _go_page(extra=""):
    return H("<button onclick=\"fetch('/api/items/7',{method:'DELETE'});" + extra +
             "\">Go</button>")


async def test_interrupted_approval_is_never_resent(browser, stub_server, make_cfg, run_dir,
                                                    tmp_path, monkeypatch):
    import asyncio

    import appmap.crawl as crawl_mod

    srv = stub_server({"/": _go_page(), "/api/items/7": lambda r: "ok"})
    cfg, rd = await _crawl_and_approve(srv, make_cfg, run_dir, tmp_path, browser,
                                       lambda u: u.endswith("/api/items/7"))
    real = crawl_mod.wait_settled

    async def settle_then_die(page, ms):
        await real(page, ms)
        if ("DELETE", "/api/items/7") in srv.requests:
            raise asyncio.CancelledError()  # interrupted after the request went out

    monkeypatch.setattr(crawl_mod, "wait_settled", settle_then_die)
    with pytest.raises(asyncio.CancelledError):
        await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                        browser=browser)
    monkeypatch.setattr(crawl_mod, "wait_settled", real)
    assert srv.requests.count(("DELETE", "/api/items/7")) == 1
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals["executed"] == 0
    assert srv.requests.count(("DELETE", "/api/items/7")) == 1
    events = [json.loads(line) for line in rd.events_path.read_text().splitlines()]
    assert [e["data"]["outcome"] for e in events if e["type"] == "approval_executed"] == [
        "unknown (interrupted)"]


async def test_hard_kill_mid_action_is_recovered_not_resent(browser, stub_server, make_cfg,
                                                            run_dir, tmp_path, monkeypatch):
    """Process killed after the request left (no outcome recorded at all)."""
    import asyncio

    import appmap.crawl as crawl_mod
    from appmap.crawl import Crawler

    srv = stub_server({"/": _go_page(), "/api/items/7": lambda r: "ok"})
    cfg, rd = await _crawl_and_approve(srv, make_cfg, run_dir, tmp_path, browser,
                                       lambda u: u.endswith("/api/items/7"))
    real = crawl_mod.wait_settled

    async def settle_then_die(page, ms):
        await real(page, ms)
        if ("DELETE", "/api/items/7") in srv.requests:
            raise asyncio.CancelledError()

    monkeypatch.setattr(crawl_mod, "wait_settled", settle_then_die)
    monkeypatch.setattr(Crawler, "_finish_approval", lambda self, *a, **k: None)  # "killed"
    with pytest.raises(asyncio.CancelledError):
        await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                        browser=browser)
    monkeypatch.undo()
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert srv.requests.count(("DELETE", "/api/items/7")) == 1
    assert stats.approvals == {"executed": 0, "failed": 1, "skipped": 0}
    assert any("interrupted" in w for w in stats.warnings)
    items = yaml.safe_load(rd.approvals_path.read_text())["items"]
    assert "interrupted" in [i for i in items if i["predicted_request"]["url"].endswith(
        "/api/items/7")][0]["execution"]


async def test_timer_request_to_other_url_stays_blocked(browser, stub_server, make_cfg,
                                                        run_dir, tmp_path):
    srv = stub_server({
        "/": _go_page("setTimeout(()=>fetch('/api/items/8',{method:'DELETE'}),30)"),
        "/api/items/7": lambda r: "ok", "/api/items/8": lambda r: "ok"})
    cfg, rd = await _crawl_and_approve(srv, make_cfg, run_dir, tmp_path, browser,
                                       lambda u: u.endswith("/api/items/7"))
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals["executed"] == 1
    assert ("DELETE", "/api/items/7") in srv.requests
    assert ("DELETE", "/api/items/8") not in srv.requests
    other = [r for r in q(rd.root, "SELECT * FROM approvals")
             if json.loads(r["predicted_request"] or "{}").get("url", "").endswith("/8")]
    assert other and other[0]["decision"] == "pending"


async def test_window_rule_only_covers_the_acted_on_page(browser, stub_server, make_cfg,
                                                         run_dir, tmp_path):
    """R21: with no predicted write, a popup's held request is not let through."""
    srv = stub_server({
        "/": H("<button onclick=\"if (confirm('Sure?')) {fetch('/api/thing',{method:'POST'});"
               "window.open('/pop')}\">Proceed</button>"),
        "/pop": H("<script>fetch('/api/items/9',{method:'DELETE'})</script><p>pop</p>"),
        "/api/thing": lambda r: "ok", "/api/items/9": lambda r: "ok"})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    aid = q(rd.root, "SELECT id FROM approvals WHERE label='Proceed'")[0]["id"]
    _decide(rd, {aid: "approve"})
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals["executed"] == 1
    assert srv.requests.count(("POST", "/api/thing")) == 1
    assert ("DELETE", "/api/items/9") not in srv.requests
    held = [r for r in q(rd.root, "SELECT * FROM approvals")
            if json.loads(r["predicted_request"] or "{}").get("url", "").endswith("/api/items/9")]
    assert held and held[0]["decision"] == "pending"


async def test_approved_direct_write_does_not_follow_redirect(browser, stub_server, make_cfg,
                                                              run_dir, tmp_path):
    srv = stub_server({"/": H("<p>home</p>"), "/done": H("<p>done</p>"),
                       "/api/items/9": lambda r: werkzeug.Response(
                           "", status=302, headers={"Location": "/done"})})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    url = srv.url + "/api/items/9"
    [aid] = _enqueue(rd.root, _pa(f"DELETE {url}", path=[{"type": "request", "method": "DELETE",
                                                          "url": url, "origin": "replay"}],
                                  pred={"method": "DELETE", "url": url}))
    _decide(rd, {aid: "approve"})
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals["executed"] == 1
    assert srv.requests == [("DELETE", "/api/items/9")]
    assert q(rd.root, "SELECT status, origin FROM exchanges") == [
        {"status": 302, "origin": "approved"}]


async def test_circuit_open_before_send_does_not_spend_approval(browser, stub_server, make_cfg,
                                                                run_dir, tmp_path, monkeypatch):
    from appmap.gate import RequestGate
    from appmap.throttle import CircuitOpen

    srv = stub_server({"/": H("<p>home</p>"), "/api/items/9": lambda r: "ok"})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    url = srv.url + "/api/items/9"
    [aid] = _enqueue(rd.root, _pa(f"DELETE {url}", path=[{"type": "request", "method": "DELETE",
                                                          "url": url}],
                                  pred={"method": "DELETE", "url": url}))
    _decide(rd, {aid: "approve"})

    async def refuse(self, method, url, **kw):
        raise CircuitOpen("tripped")

    monkeypatch.setattr(RequestGate, "admit", refuse)
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    monkeypatch.undo()
    assert stats.status == "paused_circuit" and srv.requests == []
    row = q(rd.root, "SELECT decision, executed_event FROM approvals")[0]
    assert row == {"decision": "approve", "executed_event": None}
    assert not q(rd.root, "SELECT 1 FROM meta WHERE key LIKE 'approval_inflight:%'")
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, approved_only=True,
                            browser=browser)
    assert stats.approvals["executed"] == 1 and srv.requests == [("DELETE", "/api/items/9")]


async def test_yaml_edits_during_crawl_are_kept(browser, stub_server, make_cfg, run_dir,
                                                tmp_path, monkeypatch):
    from appmap.crawl import Crawler

    srv = stub_server({"/": H("<p>home</p>")})
    cfg = _anon(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    [aid] = _enqueue(rd.root, _pa("Delete x", pred={"method": "DELETE", "url": srv.url + "/x"}))
    _decide(rd, {})
    real = Crawler.run

    async def run_and_edit(self, *a, **k):
        stats = await real(self, *a, **k)
        _edit_yaml(rd.root, decisions={aid: "deny"}, notes={aid: "not this one"})
        return stats

    monkeypatch.setattr(Crawler, "run", run_and_edit)
    await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    item = yaml.safe_load(rd.approvals_path.read_text())["items"][0]
    assert item["decision"] == "deny" and item["note"] == "not this one"
    assert q(rd.root, "SELECT decision FROM approvals")[0]["decision"] == "deny"
    assert not list(rd.root.glob(".approvals.yaml.*.tmp"))
