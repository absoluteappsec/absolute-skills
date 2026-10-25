import asyncio
import json
import re
import shlex
import urllib.request

import pytest
import werkzeug

from appmap.cli import main
from appmap.crawl import CrawlStats, run_crawl
from appmap.store import Store

# Fast, deterministic limits for fixture crawls.
FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150,
        "max_states_per_route_pattern": 3}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def q(rd, sql, *args):
    store = Store(rd.db_path)
    try:
        return [dict(r) for r in store.conn.execute(sql, args)]
    finally:
        store.close()


def q1(rd, sql, *args):
    return next(iter(q(rd, sql, *args)[0].values()))


async def test_ssr_crawl_admin_and_alice(browser, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["admin", "alice", "anonymous"], budget_minutes=5,
                            browser=browser)
    assert isinstance(stats, CrawlStats)
    assert stats.status == "done"
    pages = {r["pattern"] for r in q(rd, "select pattern from routes")}
    assert {"/", "/projects", "/projects/{id}", "/projects/new", "/admin",
            "/account/password"} <= pages
    assert q1(rd, "select count(*) from approvals where category='delete'") >= 1  # project delete held
    # I8: the delete of a project this crawl created is tagged for a created_by_run rule
    assert q1(rd, "select count(*) from approvals where category='delete' "
                  "and created_by_run=1") >= 1
    assert q1(rd, "select count(*) from approvals where category='logout'") >= 1
    assert q1(rd, "select count(*) from approvals where category='credential_change'") >= 1
    assert q1(rd, "select count(*) from exchanges where url like '%/delete%'") == 0  # never executed
    assert q1(rd, "select count(*) from exchanges where url like '%/logout%'") == 0
    # admin-only page reached by admin, not linked for alice
    admin_states = {r["role"] for r in q(rd, "select role from states where route_pattern='/admin'")}
    assert "admin" in admin_states and "alice" not in admin_states
    # a successful create-project submission produced a transition with locators + exchanges
    subs = q(rd, "select * from transitions where action_type='submit'")
    assert subs and all(t["locator_set_id"] for t in subs)
    assert any(json.loads(t["exchange_ids"] or "[]") for t in subs)
    assert q1(rd, "select count(*) from forms where succeeded=1") >= 1
    assert q1(rd, "select count(*) from fields") >= 3
    # I7: the values filled are kept per field (passwords redacted)
    tried = q(rd, "select fi.name, fi.type, fi.values_tried from fields fi where "
                  "fi.values_tried is not null")
    names = {r["name"]: json.loads(r["values_tried"]) for r in tried}
    assert any(isinstance(v, str) and "appmap-" in v for v in names.get("name", []))
    assert all(json.loads(r["values_tried"]) == ["<redacted:password>"]
               for r in tried if r["type"] == "password")
    # artifacts written for states, DOM redacted of password values
    st = q(rd, "select * from states where screenshot is not null")
    assert st and all((rd.root / s["screenshot"]).exists() for s in st)
    assert all((rd.root / s["dom_snapshot"]).exists() for s in st)
    for p in rd.artifacts_dir.glob("*.html"):
        assert "admin-pw-Fx1" not in p.read_text() and "alice-pw-Fx2" not in p.read_text()
    # list sampling: of 25 linked projects only `cap` are visited per role (+1: the project
    # the create form redirected to)
    n = q1(rd, "select count(*) from frontier where role='admin' and kind='url' "
               "and url like '%/projects/%' and url not like '%/new' "
               "and url not like '%/private'")
    assert n <= FAST["max_states_per_route_pattern"] + 1
    assert stats.queued_approvals == q1(rd, "select count(*) from approvals where decision='pending'")
    assert rd.approvals_path.exists()
    # out-of-scope redirect target recorded as an external link, never requested
    assert q1(rd, "select count(*) from external_links where url like 'https://external.invalid%'") >= 1
    # crawler-created projects (ids > 25) get their own held delete, for marker-scoped approval
    created = set()
    for a in q(rd, "select action_path from approvals where category='delete'"):
        for step in json.loads(a["action_path"]):
            m = re.search(r"/projects/(\d+)$", step.get("url", ""))
            if m and int(m.group(1)) > 25:
                created.add(int(m.group(1)))
    assert created
    # link provenance: a page reached from several pages has an edge from each of them
    multi = q(rd, "select to_state, count(distinct from_state) n from transitions "
                  "where action_type='navigate' group by to_state having n >= 2")
    assert multi


async def test_spa_crawl_states_and_list_sampling(browser, spa_url, make_cfg, run_dir, tmp_path):
    roles = [{"name": "admin", "login": {
        "type": "form", "url": f"{spa_url}/", "username_env": "APPMAP_ADMIN_USER",
        "password_env": "APPMAP_ADMIN_PASS", "success_selector": "text=Signed in as"}}]
    cfg = make_cfg(spa_url, roles=roles, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=5, browser=browser)
    assert stats.status == "done"
    detail = q(rd, "select * from states where route_pattern='/items/{id}'")
    assert 1 <= len(detail) <= FAST["max_states_per_route_pattern"]
    # card clicks (div with a click listener only) are sampled, not all 100 items
    clicks = q(rd, "select t.* from transitions t join states s on s.id=t.to_state "
                   "where t.action_type='click' and s.route_pattern='/items/{id}'")
    assert 1 <= len(clicks) <= FAST["max_states_per_route_pattern"]
    assert all(t["locator_set_id"] for t in clicks)
    assert q1(rd, "select count(*) from locators where locator_set_id=?", clicks[0]["locator_set_id"]) >= 1
    sse = q(rd, "select kind from endpoints where path_pattern='/api/events'")
    assert sse and sse[0]["kind"] == "sse"
    # logout and delete buttons are held, never clicked
    cats = {r["category"] for r in q(rd, "select category from approvals")}
    assert {"logout", "delete"} <= cats
    assert q1(rd, "select count(*) from exchanges where req_body like '%DeleteItem%'") == 0
    # the inferred (non-<form>) item form was submitted
    assert q1(rd, "select count(*) from exchanges where method='POST' and url like '%/api/items'") >= 1


async def test_budget_checkpoint_and_resume(browser, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    s1 = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=0.02, browser=browser)
    assert s1.status == "in_progress"
    pending = q1(rd, "select count(*) from frontier where status='pending'")
    done_before = q1(rd, "select count(*) from frontier where status!='pending'")
    assert pending > 0 and done_before >= 1
    s2 = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=5, browser=browser)
    assert s2.status == "done"
    assert q1(rd, "select count(*) from frontier where status='pending'") == 0
    assert q1(rd, "select count(*) from states") == q1(rd, "select count(distinct id) from states")
    # resumed, not restarted: the seed page was visited once across both runs
    assert q1(rd, "select count(*) from frontier where role='admin' and url=?", ssr_url + "/") == 1
    assert s2.states >= s1.states
    # links discovered before the checkpoint still produced their edges after resume
    assert q1(rd, "select count(*) from frontier_links l where l.from_state != '' "
                  "and l.to_state is null and exists (select 1 from frontier_links v "
                  "where v.role=l.role and v.url=l.url and v.from_state='')") == 0
    assert q1(rd, "select count(*) from transitions where action_type='navigate'") >= 3


async def test_session_expiry_mid_crawl(browser, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)

    async def expire_after_first_state():
        while True:
            await asyncio.sleep(0.01)
            try:
                if q1(rd, "select count(*) from states") >= 1:
                    break
            except Exception:  # schema not created yet
                continue
        await asyncio.to_thread(urllib.request.urlopen, urllib.request.Request(
            ssr_url + "/__expire", method="POST", data=b""))

    expirer = asyncio.ensure_future(expire_after_first_state())
    stats = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=5, browser=browser)
    await expirer
    assert stats.status == "done"
    types = [json.loads(line)["type"] for line in rd.events_path.read_text().splitlines()]
    assert "session_lost" in types
    assert types.count("login") >= 2  # initial login + re-login after expiry
    admin = q(rd, "select * from states where role='admin' and route_pattern='/admin'")
    assert admin and "Admin" in (admin[0]["title"] or "")
    assert q1(rd, "select count(*) from exchanges where role='admin' and status=200 "
                  "and url like '%/admin'") >= 1


async def test_confirm_dialog_dismissed_and_queued(browser, stub_server, make_cfg, run_dir,
                                                   tmp_path):
    html = ("<html><head><title>Dialogs</title></head><body>"
            "<button onclick=\"if (confirm('Sure?')) fetch('/api/thing', {method: 'POST'})\">"
            "Proceed</button>"
            "<button onclick=\"alert('hello'); document.title='alerted'\">Greet</button>"
            "</body></html>")
    srv = stub_server({"/": lambda r: __import__("werkzeug").Response(html, mimetype="text/html"),
                       "/api/thing": lambda r: "ok"})
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    assert stats.status == "done"
    assert ("POST", "/api/thing") not in srv.requests
    held = q(rd, "select * from approvals where label='Proceed'")
    assert held and held[0]["category"] == "delete"
    assert "confirm" in held[0]["reasons"]
    assert q1(rd, "select count(*) from frontier where status='queued_approval'") >= 1
    events = [json.loads(line) for line in rd.events_path.read_text().splitlines()]
    dialogs = {e["data"]["dialog_type"] for e in events if e["type"] == "dialog"}
    assert {"confirm", "alert"} <= dialogs


def _anon_cfg(make_cfg, url, **kw):
    return make_cfg(url, roles=[{"name": "anonymous", "login": {"type": "none"}}], **kw)


async def test_js_delete_request_guarded(browser, stub_server, make_cfg, run_dir, tmp_path):
    """R17: a click whose JS issues DELETE (icon button, no telling label) is aborted and held;
    a secondary button of a confirm-protected form is classified with that form."""
    html = ("<html><head><title>Items</title></head><body><ul><li>Item 7 "
            "<button onclick=\"fetch('/api/items/7',{method:'DELETE'})\">&#x2715;</button></li></ul>"
            "<form method='post' action='/items/7/x' onsubmit=\"return confirm('Sure?')\">"
            "<button type='submit'>Ok</button><button type='submit' name='alt'>Go</button>"
            "</form></body></html>")
    srv = stub_server({"/": lambda r: werkzeug.Response(html, mimetype="text/html"),
                       "/api/items/7": lambda r: "ok", "/items/7/x": lambda r: "ok"})
    cfg = _anon_cfg(make_cfg, srv.url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    assert stats.status == "done"
    assert ("DELETE", "/api/items/7") not in srv.requests
    assert ("POST", "/items/7/x") not in srv.requests
    held = q(rd, "select * from approvals where label='\u2715'")
    assert held and held[0]["category"] == "delete"
    pred = json.loads(held[0]["predicted_request"])
    assert pred["method"] == "DELETE" and pred["url"].endswith("/api/items/7")
    go = q(rd, "select * from approvals where label='Go'")
    assert go and go[0]["category"] == "delete"
    assert q1(rd, "select count(*) from frontier where status='queued_approval'") >= 2


async def test_write_click_not_replayed_for_children(browser, stub_server, make_cfg, run_dir,
                                                     tmp_path):
    """A click that POSTs is never re-clicked as a restore prefix for its children."""
    html = ("<html><head><title>Fav</title></head><body>"
            "<button onclick=\"fetch('/api/star',{method:'POST'});"
            "var d=document.createElement('section');d.innerHTML="
            "'<button onclick=\\'this.textContent=1\\'>One</button>"
            "<button onclick=\\'this.textContent=2\\'>Two</button>';"
            "document.body.appendChild(d)\">Star</button></body></html>")
    srv = stub_server({"/": lambda r: werkzeug.Response(html, mimetype="text/html"),
                       "/api/star": lambda r: "ok"})
    cfg = _anon_cfg(make_cfg, srv.url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    assert (await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2,
                            browser=browser)).status == "done"
    assert sum(1 for m, p in srv.requests if m == "POST" and p == "/api/star") == 1
    star = q(rd, "select action_path from frontier where action_path like '%Star%'")
    assert json.loads(star[0]["action_path"])[-1]["mutating"] is True


async def test_production_js_post_click_held(browser, stub_server, make_cfg, run_dir, tmp_path):
    html = ("<html><head><title>Notes</title></head><body>"
            "<button onclick=\"fetch('/api/notes',{method:'POST'})\">Add</button></body></html>")
    srv = stub_server({"/": lambda r: werkzeug.Response(html, mimetype="text/html"),
                       "/api/notes": lambda r: "ok"})
    cfg = _anon_cfg(make_cfg, srv.url, profile="production",
                    limits={"quiet_window_ms": 150, "jitter_seconds": 0.5})
    rd = run_dir(tmp_path, cfg)
    assert (await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2,
                            browser=browser)).status == "done"
    assert ("POST", "/api/notes") not in srv.requests
    assert q(rd, "select category from approvals where label='Add'")[0]["category"] == "data_create"


async def test_approved_only_without_approvals_crawls_nothing(browser, ssr_url, make_cfg,
                                                              run_dir, tmp_path):
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=1, approved_only=True,
                            browser=browser)
    assert stats.status == "done"
    assert stats.approvals == {"executed": 0, "failed": 0, "skipped": 0}
    assert q1(rd, "select count(*) from exchanges") == 0


def test_manual_login_roles_only_converts_interactive(make_cfg, ssr_url):
    from appmap.cli import manual_login_roles
    cfg = make_cfg(ssr_url, roles=[
        {"name": "a", "login": {"type": "form", "url": ssr_url + "/login",
                                "username_env": "U", "password_env": "P"}},
        {"name": "b", "login": {"type": "none"}},
        {"name": "c", "login": {"type": "storage_state", "storage_state_path": "s.json"}}])
    out = {r.name: r.login.type for r in manual_login_roles(cfg.roles, explicit=False)}
    assert out == {"a": "manual", "b": "none", "c": "storage_state"}
    one = manual_login_roles([cfg.roles[2]], explicit=True)
    assert one[0].login.type == "manual"


def test_cli_next_quotes_paths_with_spaces(ssr_url, make_cfg, tmp_path, capsys):
    cfg = make_cfg(ssr_url)
    out = tmp_path / "my runs"
    assert main(["init", "--config", str(cfg.source_path), "--out", str(out)]) == 0
    st = _status(capsys)
    run = next(out.iterdir())
    assert st["next"] == f"appmap auth --run {shlex.quote(str(run))}"
    assert shlex.split(st["next"])[-1] == str(run)


def _status(capsys) -> dict:
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("APPMAP_STATUS ")
    return json.loads(last.removeprefix("APPMAP_STATUS "))


def test_cli_init_auth_crawl(ssr_url, make_cfg, tmp_path, capsys):
    cfg = make_cfg(ssr_url, limits=FAST, roles=[
        {"name": "alice", "login": {"type": "form", "url": f"{ssr_url}/login",
                                    "username_env": "APPMAP_ALICE_USER",
                                    "password_env": "APPMAP_ALICE_PASS"}}])
    out = tmp_path / "runs"
    assert main(["init", "--config", str(cfg.source_path), "--out", str(out)]) == 0
    st = _status(capsys)
    assert st["ok"] and st["command"] == "init"
    run = next(out.iterdir())
    assert st["next"] == f"appmap auth --run {shlex.quote(str(run))}"
    meta = json.loads((run / "run.json").read_text())
    assert meta["config_path"] == str(cfg.source_path.resolve())

    assert main(["auth", "--run", str(run)]) == 0
    st = _status(capsys)
    assert st["ok"] and st["status"] == "authenticated" and st["counts"]["roles_ok"] == 1

    assert main(["crawl", "--run", str(run), "--budget-minutes", "0.02"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["status"] == "in_progress"
    assert st["next"] == f"appmap crawl --run {shlex.quote(str(run))}"
    assert main(["crawl", "--run", str(run), "--role", "alice"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["status"] == "done" and st["counts"]["states"] > 0
    assert st["pending_approvals"] >= 1  # logout + project delete
    log = (run / "crawl.log").read_text()
    assert "crawl" in log and "alice-pw-Fx2" not in log
    assert json.loads((run / "run.json").read_text())["status"] == "done"
    # R18: one HAR per role accumulates every invocation (auth's login traffic survives)
    har = json.loads((run / "traffic" / "alice.har").read_text())
    reqs = [(e["request"]["method"], e["request"]["url"]) for e in har["log"]["entries"]]
    assert any(m == "POST" and u.endswith("/login") for m, u in reqs)
    assert any(m == "GET" and u.endswith("/account/password") for m, u in reqs)
    assert not list((run / "traffic").glob("*.part.har"))
    assert "alice-pw-Fx2" not in (run / "traffic" / "alice.har").read_text()
    # --approved-only with nothing approved executes nothing
    assert main(["crawl", "--run", str(run), "--approved-only"]) == 0
    st = _status(capsys)
    assert st["ok"] and st["status"] == "done"
    assert st["counts"]["executed"] == 0 and st["counts"]["failed"] == 0


def test_cli_parser_flags():
    from appmap.cli import build_parser
    p = build_parser()
    a = p.parse_args(["crawl", "--run", "R"])
    assert a.budget_minutes == 8 and a.approved_only is False and a.role is None
    a = p.parse_args(["auth", "--run", "R", "--role", "admin", "--manual-login"])
    assert a.manual_login and a.role == "admin"


def test_cli_missing_run_dir_reports_error(tmp_path, capsys):
    assert main(["crawl", "--run", str(tmp_path / "nope")]) == 1
    st = _status(capsys)
    assert not st["ok"] and st["errors"]


async def test_failed_relogin_stops_role_and_keeps_frontier(browser, ssr_url, make_cfg,
                                                            run_dir, tmp_path, monkeypatch):
    """C1: a re-login that fails (password changed mid-crawl) is not retried on every
    remaining item: the role stops, its items stay pending, status is auth_failed."""
    from appmap.browser.session import MAX_RELOGINS
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)

    async def break_creds():
        while True:
            await asyncio.sleep(0.01)
            try:
                if q1(rd, "select count(*) from states") >= 1:
                    break
            except Exception:
                continue
        monkeypatch.setenv("APPMAP_ADMIN_PASS", "wrong")
        await asyncio.to_thread(urllib.request.urlopen, urllib.request.Request(
            ssr_url + "/__expire", method="POST", data=b""))

    t = asyncio.ensure_future(break_creds())
    stats = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=3, browser=browser)
    await t
    assert stats.status == "auth_failed"
    assert stats.auth_failed == ["admin"]
    assert any("admin" in w and "appmap auth" in w for w in stats.warnings)
    assert q1(rd, "select count(*) from frontier where role='admin' and status='pending'") >= 1
    assert q1(rd, "select count(*) from frontier where status='failed'") == 0
    posts = q1(rd, "select count(*) from exchanges where method='POST' and url like '%/login'")
    assert posts <= 1 + MAX_RELOGINS  # initial login + capped re-logins
    logins = [json.loads(l) for l in rd.events_path.read_text().splitlines()
              if json.loads(l)["type"] == "login"]
    assert sum(1 for e in logins if not e.get("data", e).get("ok")) == 1


async def test_manual_role_never_prompts_in_crawl(browser, ssr_url, make_cfg, run_dir,
                                                  tmp_path, monkeypatch):
    """A manual role without a saved session fails closed (no input() behind a headless
    browser): auth_failed with a clear warning."""
    def no_input(*a, **k):
        raise AssertionError("crawl must never prompt for a manual login")
    monkeypatch.setattr("builtins.input", no_input)
    cfg = make_cfg(ssr_url, limits=FAST, roles=[
        {"name": "sso", "login": {"type": "manual", "url": f"{ssr_url}/login"}}])
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["sso"], budget_minutes=1, browser=browser)
    assert stats.status == "auth_failed" and stats.auth_failed == ["sso"]
    assert any("--manual-login" in w for w in stats.warnings)


def test_cli_crawl_auth_failed_next(monkeypatch, ssr_url, make_cfg, tmp_path, capsys):
    import appmap.crawl as crawl_mod
    cfg = make_cfg(ssr_url, roles=[
        {"name": "sso", "login": {"type": "manual", "url": f"{ssr_url}/login"}}])
    out = tmp_path / "runs"
    assert main(["init", "--config", str(cfg.source_path), "--out", str(out)]) == 0
    _status(capsys)
    run = next(out.iterdir())

    async def fake(*a, **k):
        return CrawlStats(0, 0, 0, 0, 0, "auth_failed", warnings=["role sso: ..."],
                          auth_failed=["sso"])
    monkeypatch.setattr(crawl_mod, "run_crawl", fake)
    assert main(["crawl", "--run", str(run)]) == 1
    st = _status(capsys)
    assert not st["ok"] and st["status"] == "auth_failed"
    assert st["next"] == (f"appmap auth --run {shlex.quote(str(run))} --role sso "
                          "--manual-login")


def _events(rd):
    return [json.loads(line) for line in rd.events_path.read_text().splitlines()]


async def test_forbidden_401_is_not_session_loss(browser, ssr_url, make_cfg, run_dir, tmp_path):
    """Field fix 2: a logged-in user opening a resource they do not own gets 401 (as VTM
    does). The session is confirmed alive with a known-authenticated probe, so the 401 is
    recorded as an access-denied page, not a lost session: no re-login, no auth_failed."""
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["alice"], budget_minutes=5, browser=browser)
    assert stats.status == "done", stats.warnings
    assert stats.auth_failed == []
    n401 = q1(rd, "select count(*) from exchanges where role='alice' and status=401 "
                  "and url like '%/private'")
    assert n401 >= 2
    assert q1(rd, "select count(*) from frontier where role='alice' and url like '%/private' "
                  "and status='done'") >= 2
    events = _events(rd)
    assert sum(1 for e in events if e["type"] == "login") == 1  # the initial login only
    assert any("landing page" in w for w in stats.warnings)  # liveness via landing: said
    assert not [e for e in events if e["type"] == "session_lost"]
    denied = [e for e in events if e["type"] == "observation"
              and e["data"].get("kind") == "access_denied"]
    assert len(denied) >= 2 and all(e["data"].get("status") == 401 for e in denied)
    assert q1(rd, "select count(*) from observations where kind='access_denied' "
                  "and role='alice'") >= 2


async def test_forbidden_401_then_genuine_expiry_still_relogins(browser, ssr_url, make_cfg,
                                                                run_dir, tmp_path):
    """Genuine expiry (/__expire) after 401 pages still re-logs in and the crawl goes on."""
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)

    async def expire_after_private():
        while True:
            await asyncio.sleep(0.01)
            try:
                if q1(rd, "select count(*) from exchanges where status=401") >= 1:
                    break
            except Exception:
                continue
        await asyncio.to_thread(urllib.request.urlopen, urllib.request.Request(
            ssr_url + "/__expire", method="POST", data=b""))

    t = asyncio.ensure_future(expire_after_private())
    stats = await run_crawl(cfg, rd, roles=["alice"], budget_minutes=5, browser=browser)
    await t
    assert stats.status == "done", stats.warnings
    events = _events(rd)
    assert [e for e in events if e["type"] == "session_lost"]
    assert sum(1 for e in events if e["type"] == "login" and e["data"].get("ok")) >= 2


async def test_password_type_field_values_never_stored(browser, ssr_url, make_cfg, run_dir,
                                                       tmp_path):
    """Field fix 2: an <input type=password> whose name does not look like a password
    (VTM's ``ssn``; here ``pin``) is redacted everywhere: the value the crawler typed is
    learned as a literal secret before submitting. A type=text ``ssn`` is customer data
    and kept."""
    cfg = make_cfg(ssr_url, limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=5, browser=browser)
    assert stats.status == "done", stats.warnings
    pins = json.loads(urllib.request.urlopen(ssr_url + "/__pins").read())
    assert pins, "the crawler never submitted the PIN form"
    from urllib.parse import quote, quote_plus
    needles = set()
    for pin in pins:
        needles |= {pin, quote(pin, safe=""), quote_plus(pin), json.dumps(pin)[1:-1]}
    needles = {n.encode() for n in needles if n}
    for f in rd.root.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert not any(n in data for n in needles), f
    posts = q(rd, "select req_body, req_body_path from exchanges where method='POST' "
                  "and url like '%/account/pin'")
    assert posts
    body = posts[0]["req_body"] or (rd.root / posts[0]["req_body_path"]).read_text()
    assert "pin=%3Credacted%3Apassword%3E" in body or "pin=<redacted:password>" in body
    assert re.search(r"ssn=appmap", body)  # type=text ssn: customer data, kept
