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


def H(body):
    return lambda r: werkzeug.Response(
        "<html><head><title>Items</title></head><body>" + body + "</body></html>",
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
