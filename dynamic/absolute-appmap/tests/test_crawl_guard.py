"""Network-level destructive guard (ruling R17): every page, popup and worker of a role
context, during and after actions. Ported from the review probe (test_variants)."""
import json

import pytest
import werkzeug

from appmap.crawl import run_crawl
from appmap.store import Store

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150,
        "max_states_per_route_pattern": 3}


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def H(body, title="Items"):
    return lambda r: werkzeug.Response(
        f"<html><head><title>{title}</title></head><body>" + body + "</body></html>",
        mimetype="text/html")


CASES = {
    "popup": {"/": H("<button onclick=\"window.open('/pop')\">Open</button>"),
              "/pop": H("<script>fetch('/api/items/7',{method:'DELETE'})</script>")},
    "timer": {"/": H("<button onclick=\"setTimeout(()=>fetch('/api/items/7',"
                     "{method:'DELETE'}),400)\">x</button>")},
    "timer_long": {"/": H("<button onclick=\"setTimeout(()=>fetch('/api/items/7',"
                          "{method:'DELETE'}),1500)\">x</button><a href='/b'>b</a>"
                          "<a href='/c'>c</a>"),
                   "/b": H("<p>b</p>"), "/c": H("<p>c</p>")},
    "div": {"/": H("<div style='cursor:pointer' onclick=\"fetch('/api/items/7',"
                   "{method:'DELETE'})\">x</div>")},
    "worker": {"/": H("<button onclick=\"new Worker('/w.js')\">Go</button>"),
               "/w.js": lambda r: werkzeug.Response(
                   "fetch('/api/items/7',{method:'DELETE'})",
                   mimetype="application/javascript")},
    "page_load": {"/": H("<script>fetch('/api/items/7',{method:'DELETE'})</script><p>hi</p>")},
}


def _approvals(rd):
    store = Store(rd.db_path)
    try:
        return [dict(r) for r in store.conn.execute("SELECT * FROM approvals")]
    finally:
        store.close()


async def _crawl(case_routes, browser, stub_server, make_cfg, run_dir, tmp_path,
                 profile="staging", limits=FAST):
    srv = stub_server(case_routes)
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   limits=limits, profile=profile)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["anonymous"], budget_minutes=2, browser=browser)
    return srv, rd, stats


@pytest.mark.parametrize("case", list(CASES))
async def test_destructive_request_never_sent(case, browser, stub_server, make_cfg, run_dir,
                                              tmp_path):
    routes = dict(CASES[case])
    routes["/api/items/7"] = lambda r: "ok"
    srv, rd, stats = await _crawl(routes, browser, stub_server, make_cfg, run_dir, tmp_path)
    assert stats.status == "done"
    assert ("DELETE", "/api/items/7") not in srv.requests
    if case.startswith("timer"):
        return  # the page moves on before the timer fires; nothing reached the network
    held = [a for a in _approvals(rd)
            if (json.loads(a["predicted_request"] or "{}").get("method") == "DELETE")]
    assert held and all(a["category"] == "delete" for a in held)
    for a in held:
        path = json.loads(a["action_path"])
        assert path and path[0]["type"] == "goto"
    if case == "page_load":
        assert held[0]["label"].endswith("(page load)")
    elif case != "worker":
        assert any(json.loads(a["action_path"])[-1]["type"] == "click" for a in held)


PROD = {
    "beacon": H("<button onclick=\"navigator.sendBeacon('/api/items', 'x')\">Save</button>"),
    "fetchpost": H("<button onclick=\"fetch('/api/items',{method:'POST',body:'x'})\">Add</button>"),
}


@pytest.mark.parametrize("case", list(PROD))
async def test_production_js_writes_held(case, browser, stub_server, make_cfg, run_dir,
                                         tmp_path):
    srv, rd, stats = await _crawl({"/": PROD[case], "/api/items": lambda r: "ok"}, browser,
                                  stub_server, make_cfg, run_dir, tmp_path,
                                  profile="production", limits=FAST)
    assert stats.status == "done"
    assert ("POST", "/api/items") not in srv.requests
    assert any(a["category"] == "data_create" for a in _approvals(rd))


# -- second review round: popups/frames reached without a crawler click, re-login window --
POP = H("<script>fetch('/api/items/7',{method:'DELETE'})</script><p>p</p>")
OPENERS = {
    "blank_link": {"/": H("<a href='/pop' target='_blank'>Open details</a>"), "/pop": POP},
    "load_popup": {"/": H("<script>window.open('/pop')</script><p>hi</p>"), "/pop": POP},
    "iframe": {"/": H("<iframe src='/pop'></iframe><p>hi</p>"), "/pop": POP},
    "form_target_blank": {"/": H("<form action='/pop' target='_blank'><input name=q>"
                                 "<button>Search</button></form>"), "/pop": POP},
}


@pytest.mark.parametrize("case", list(OPENERS))
async def test_destructive_request_from_opened_page_held(case, browser, stub_server, make_cfg,
                                                         run_dir, tmp_path):
    routes = dict(OPENERS[case])
    routes["/api/items/7"] = lambda r: "ok"
    srv, rd, _ = await _crawl(routes, browser, stub_server, make_cfg, run_dir, tmp_path)
    assert ("DELETE", "/api/items/7") not in srv.requests
    assert any(json.loads(a["predicted_request"] or "{}").get("method") == "DELETE"
               for a in _approvals(rd))


def _login_routes(login_extra: str = ""):
    login_page = H("<form method=post action='/login'><input name=username>"
                   "<input type=password name=password><button type=submit>Sign in</button>"
                   "</form>" + login_extra, "Sign in")

    def login(r):
        if r.method == "POST":
            resp = werkzeug.Response("", status=302, headers={"Location": "/"})
            resp.set_cookie("sid", "1")
            return resp
        return login_page(r)
    return login


def _alice(make_cfg, url, profile="staging"):
    return make_cfg(url, profile=profile, limits=FAST, roles=[{"name": "alice", "login": {
        "type": "form", "url": f"{url}/login", "username_env": "APPMAP_ALICE_USER",
        "password_env": "APPMAP_ALICE_PASS"}}])


async def test_relogin_window(browser, stub_server, make_cfg, run_dir, tmp_path):
    """A destructive timer armed by a crawl action must not fire while re-login runs."""
    srv = stub_server({
        "/login": _login_routes(),
        "/": H("<button onclick=\"fetch('/api/me');setTimeout(()=>fetch('/api/items/7',"
               "{method:'DELETE'}),700)\">Refresh</button>"),
        "/api/me": lambda r: werkzeug.Response("no", status=401),
        "/api/items/7": lambda r: "ok",
    })
    cfg = _alice(make_cfg, srv.url)
    rd = run_dir(tmp_path, cfg)
    await run_crawl(cfg, rd, roles=["alice"], budget_minutes=2, browser=browser)
    assert ("DELETE", "/api/items/7") not in srv.requests
    types = [json.loads(line)["type"] for line in rd.events_path.read_text().splitlines()]
    assert "session_lost" in types and types.count("login") >= 2


async def test_login_page_destructive_request_still_held(browser, stub_server, make_cfg,
                                                         run_dir, tmp_path):
    """The login exemption covers only the login submit, not a DELETE the login page sends;
    in production the login POST itself (data_create) still goes through."""
    srv = stub_server({
        "/login": _login_routes("<script>fetch('/api/items/7',{method:'DELETE'})</script>"),
        "/": H("<p>home</p>"),
        "/api/items/7": lambda r: "ok",
    })
    cfg = _alice(make_cfg, srv.url, profile="production")
    rd = run_dir(tmp_path, cfg)
    # the initial login runs before any crawl page exists, under the crawler's guard
    from appmap.browser.session import login, open_role_session
    from appmap.crawl import Crawler, build_runtime, make_datagen
    rt = build_runtime(cfg, rd)
    sess = await open_role_session(browser, cfg, rd, cfg.roles[0], rt.recorder)
    crawler = Crawler(cfg, rd, rt.store, rt.events, rt.gate, rt.queue, rt.recorder,
                      {"alice": sess}, make_datagen(cfg, rd))
    page = await crawler._open_page(sess)  # installs the context guard
    assert await login(sess, cfg, rt.events)
    await rt.recorder.drain()
    await page.close()
    await sess.context.close()
    rt.store.close()
    assert ("POST", "/login") in srv.requests
    assert ("DELETE", "/api/items/7") not in srv.requests


REDIRECT_CASES = {
    "form": "<form method='post' action='/save'><input name='n' value='x'>"
            "<button type='submit'>Save</button></form>",
    "fetch": "<button onclick=\"fetch('/save',{method:'POST',body:'a=1'})\">Save</button>",
}


@pytest.mark.parametrize("case", list(REDIRECT_CASES))
@pytest.mark.parametrize("code", [307, 308])
async def test_unsafe_307_redirect_hop_classified(case, code, browser, stub_server, make_cfg,
                                                  run_dir, tmp_path):
    """I1: Chromium re-sends a POST on 307/308; the hop's target is classified and a
    destructive one is aborted and queued, never sent."""
    routes = {"/": H(REDIRECT_CASES[case]),
              "/save": lambda r: werkzeug.Response(
                  status=code, headers={"Location": "/items/7/delete"}),
              "/items/7/delete": lambda r: "deleted"}
    srv, rd, _stats = await _crawl(routes, browser, stub_server, make_cfg, run_dir, tmp_path)
    assert ("POST", "/save") in srv.requests
    assert ("POST", "/items/7/delete") not in srv.requests
    held = [a for a in _approvals(rd)
            if "/items/7/delete" in (a["predicted_request"] or "")]
    assert held and held[0]["category"] == "delete"


# -- field fix 3: account-editing forms are held, never submitted -----------------------
_USER_CHANGE = (
    "<form method='post' action='/admin/auth/user/1/change/'>"
    "<input name='username' value='admin'><input type='email' name='email' value='a@x.io'>"
    "<input type='checkbox' name='is_staff' checked>"
    "<select name='groups' multiple><option value='1'>editors</option></select>"
    "<input type='submit' value='Save'>"
    "<input type='submit' name='_continue' value='Save and continue editing'></form>")
_BULK = ("<form method='post' action='/admin/auth/user/'>"
         "<select name='action'><option value=''>---</option>"
         "<option value='make_active'>Activate</option></select>"
         "<input type='checkbox' name='_selected_action' value='1'>"
         "<button type='submit' name='index' value='0'>Go</button></form>")
_DISPLAY = ("<form method='post' action='/profile'><input name='display_name'>"
            "<input type='submit' value='Save'></form>")


async def test_account_edit_forms_held_never_submitted(browser, stub_server, make_cfg,
                                                       run_dir, tmp_path):
    routes = {
        "/": H("<a href='/admin/auth/user/1/change/'>admin</a> <a href='/admin/auth/user/'>"
               "users</a> <a href='/profile'>profile</a>", title="Home"),
        "/admin/auth/user/1/change/": H(_USER_CHANGE, title="admin | Change user"),
        "/admin/auth/user/": H(_BULK, title="Select user to change"),
        "/profile": H(_DISPLAY, title="Profile"),
    }
    srv, rd, stats = await _crawl(routes, browser, stub_server, make_cfg, run_dir, tmp_path)
    assert stats.status == "done"
    posts = [p for m, p in srv.requests if m == "POST"]
    assert "/admin/auth/user/1/change/" not in posts
    assert "/admin/auth/user/" not in posts
    assert "/profile" in posts  # a plain display-name form is not held
    held = {(json.loads(a["predicted_request"] or "{}").get("url") or "").split("/", 3)[-1]:
            a["category"] for a in _approvals(rd)}
    assert held.get("admin/auth/user/1/change/") == "account_change"
    assert held.get("admin/auth/user/") == "account_change"
