import functools
import json
import time

import pytest
from werkzeug.wrappers import Response
from playwright.async_api import async_playwright

from appmap.crawl import build_runtime
from appmap.direct import DirectClient
from appmap.frontier import Frontier
from appmap.probe.bundles import mine_bundle, probe_bundles
from appmap.recording import record_exchange


def test_mine_fetch_and_axios():
    js = '''
    fetch("/api/users"); fetch(`/api/items/${id}`, {method: "DELETE"});
    axios.post("/orders/create", body); axios.get('/v2/ping');
    xhr.open("PUT", "/things/1"); const u = "/api/items/" + id;
    const x = "/not/an/api"; fetch("https://cdn.evil.example/lib.js");
    var bad = "/api/" ; var s = "/api/a b";
    //# sourceMappingURL=app.js.map
    '''
    f = mine_bundle(js)
    assert {"/api/users", "/api/items/{id}", "/orders/create", "/v2/ping", "/things/1"} <= f.api_paths
    assert "/not/an/api" not in f.api_paths and "/api/" not in f.api_paths
    assert f.methods["/orders/create"] == {"POST"} and f.methods["/api/items/{id}"] == {"DELETE"}
    assert f.methods["/things/1"] == {"PUT"}
    assert "https://cdn.evil.example/lib.js" in f.absolute_urls
    assert f.source_maps == {"app.js.map"}


def test_mine_route_objects():
    js = ('const r=[{path:"/items/:id",component:A},{path:"/admin"},{path:"*"},{path:"rel"}];'
          '<Route path="/settings" element={x}/>')
    assert mine_bundle(js).routes == {"/items/{id}", "/admin", "/settings"}


def test_mine_gql_docs():
    js = 'const q = `query GetUser($id: ID!) { user(id: $id) { id } } mutation AddItem{a}`; "query string"'
    assert mine_bundle(js).graphql_ops == {("query", "GetUser"), ("mutation", "AddItem")}


def test_mine_is_fast_on_hostile_input():
    start = time.monotonic()
    mine_bundle('"' * 200000 + "fetch(" * 50000 + "/api/" * 50000 + "`" * 100000)
    mine_bundle("a" * 5_000_000)
    assert time.monotonic() - start < 10


@pytest.fixture
async def api():
    async with async_playwright() as p:
        ctx = await p.request.new_context()
        yield ctx
        await ctx.dispose()


def _client(cfg, rd):
    rt = build_runtime(cfg, rd)
    rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                            known_patterns=[])
    return rt, DirectClient(rt.gate, rec, rt.events)


def _rows(store, sql):
    return [tuple(r) for r in store.conn.execute(sql)]


async def test_spa_internal_flags_found(api, spa_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(spa_url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _client(cfg, rd)
    assert _rows(rt.store, "SELECT count(*) FROM exchanges WHERE url LIKE '%/api/internal/flags'") == [(0,)]
    await probe_bundles(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
    eps = _rows(rt.store, "SELECT e.method, e.path_pattern, s.source FROM endpoints e JOIN "
                          "sources s ON s.item_kind='endpoint' AND s.item_id=e.id")
    assert ("GET", "/api/internal/flags", "bundle") in eps
    assert ("GET", "/api/items/{id}", "bundle") in eps
    assert any(r[1] == "/graphql" and r[0] == "POST" for r in eps)
    routes = {r[0] for r in _rows(rt.store, "SELECT pattern FROM routes WHERE kind='spa_route'")}
    assert {"/items", "/items/{id}", "/admin"} <= routes
    for role in ("admin", "alice", "anonymous"):
        urls = {r[0] for r in _rows(rt.store, f"SELECT url FROM frontier WHERE role='{role}'")}
        assert spa_url + "/admin" in urls and not any("{" in u for u in urls)
    assert _rows(rt.store, "SELECT count(*) FROM exchanges WHERE url LIKE '%/api/internal/flags'") == [(0,)]
    # second run re-uses stored bodies: no further requests
    again = await probe_bundles(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
    assert again["requests"] == 0  # pages, scripts and failed map guesses are all stored
    rt.store.close()


async def test_source_map_and_external(api, stub_server, make_cfg, run_dir, tmp_path):
    maps = json.dumps({"version": 3, "sourcesContent": ['fetch("/api/hidden/secret");']})
    js = 'fetch("/api/visible"); var u="https://tracker.example.net/x";\n//# sourceMappingURL=app.js.map'

    def page(r):
        return Response('<script src="/app.js"></script>', content_type="text/html")

    routes = {"/": page,
              "/app.js": lambda r: Response(js, content_type="application/javascript"),
              "/app.js.map": lambda r: Response(maps, content_type="application/json")}
    url = stub_server(routes).url
    cfg = make_cfg(url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _client(cfg, rd)
    await probe_bundles(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
    eps = _rows(rt.store, "SELECT e.path_pattern, s.source FROM endpoints e JOIN sources s ON "
                          "s.item_kind='endpoint' AND s.item_id=e.id")
    assert ("/api/visible", "bundle") in eps and ("/api/hidden/secret", "source_map") in eps
    assert _rows(rt.store, "SELECT url FROM external_links") == [("https://tracker.example.net/x",)]
    rt.store.close()


def test_cli_probe_passive_mines_bundles(spa_url, make_cfg, tmp_path, capsys):
    import sqlite3

    from appmap.cli import main
    cfg = make_cfg(spa_url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    assert main(["init", "--config", str(cfg.source_path), "--out", str(tmp_path / "r")]) == 0
    run = next((tmp_path / "r").iterdir())
    assert main(["probe", "--run", str(run), "--phase", "passive"]) == 0
    db = sqlite3.connect(next(run.glob("*.sqlite")))
    assert db.execute("select count(*) from endpoints where path_pattern='/api/internal/flags'"
                      ).fetchone()[0] == 1


def test_fix_template_prefix_and_static_and_open():
    f = mine_bundle('fetch(`${API}/users`); fetch(`${BASE_URL}/api/orders/${id}`);'
                    'window.open("/logout"); x.open("POST", "/submit");'
                    'fetch("/img/logo.png"); fetch("/static/css/app.css"); fetch("/data/x.json")')
    assert f.api_paths == {"/users", "/api/orders/{id}", "/submit", "/data/x.json"}
    assert f.methods["/submit"] == {"POST"}


def test_fix_bare_prefix_and_i18n():
    f = mine_bundle('a="/v1/"; b="/api/auth.login.title"; c="/api/"; d="/v2/users"; '
                    'e="/api/auth/login.title"')
    assert f.api_paths == {"/v2/users", "/api/auth/login.title"}


def test_fix_filesystem_routes():
    f = mine_bundle('[{path:"/usr/lib/x"},{path:"/etc"},{path:"/readme.md"},{path:"/home"},'
                    '{path:"/dash"}]')
    assert f.routes == {"/dash"}


def test_fix_in_scope_absolute_call_kept(api_free=None):
    f = mine_bundle('axios.post("https://app.test/orders/${id}", b); fetch("https://x.io/logo.png")')
    assert f.call_urls == {"https://app.test/orders/{id}"}
    assert f.methods["https://app.test/orders/{id}"] == {"POST"}


async def test_fix_probe_absolute_call_and_map_sources(api, stub_server, make_cfg, run_dir,
                                                       tmp_path):
    holder = {}
    maps = json.dumps({"version": 3, "sources": ["webpack/boot.js", "src/a.js", "node_modules/x.js"],
                       "sourcesContent": ['fetch("/api/webpackish");', 'fetch("/api/mine");',
                                          'fetch("/api/vendor");']})
    js = ('fetch(`${location.origin}/api/x`); axios.put(ABS + "/n"); '
          'fetch("%s/abs/thing", {method: "PATCH"});\\n//# sourceMappingURL=a.js.map')
    routes = {"/": lambda r: Response('<script src="/a.js"></script>', content_type="text/html"),
              "/a.js": lambda r: Response(js % holder["url"], content_type="application/javascript"),
              "/a.js.map": lambda r: Response(maps, content_type="application/json")}
    srv = stub_server(routes)
    holder["url"] = srv.url
    cfg = make_cfg(srv.url)
    rt, client = _client(cfg, run_dir(tmp_path, cfg))
    await probe_bundles(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
    eps = set(_rows(rt.store, "SELECT method, path_pattern FROM endpoints"))
    assert ("PATCH", "/abs/thing") in eps and ("GET", "/api/mine") in eps
    assert ("GET", "/api/webpackish") not in eps and ("GET", "/api/vendor") not in eps
    rt.store.close()
