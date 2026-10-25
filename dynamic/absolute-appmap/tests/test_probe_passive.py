import functools
import time

import pytest
from playwright.async_api import async_playwright

from appmap.crawl import build_runtime
from appmap.direct import DirectClient
from appmap.frontier import Frontier
from appmap.probe.passive import parse_robots, parse_sitemap, probe_passive
from appmap.probe.schemas import probe_schemas
from appmap.recording import record_exchange


def test_parse_robots():
    text = """# comment
User-agent: *
Disallow: /secret-report
Disallow: /private/*.pdf$
Allow: /public/
Disallow:
Sitemap: https://example.com/sitemap.xml
disallow: /Admin   # trailing
"""
    got = parse_robots(text)
    assert "/secret-report" in got and "/public/" in got and "/Admin" in got
    assert "https://example.com/sitemap.xml" in got
    assert "/private/" in got  # wildcard cut at the first '*'
    assert "" not in got and len(got) == len(set(got))


def test_parse_sitemap_index():
    index = """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc> https://e.com/s1.xml </loc></sitemap>
      <sitemap><loc>https://e.com/s2.xml</loc></sitemap></sitemapindex>"""
    assert parse_sitemap(index) == ([], ["https://e.com/s1.xml", "https://e.com/s2.xml"])
    urlset = """<urlset><url><loc>https://e.com/a?x=1&amp;y=2</loc></url>
      <url><loc><![CDATA[https://e.com/b]]></loc></url></urlset>"""
    assert parse_sitemap(urlset) == (["https://e.com/a?x=1&y=2", "https://e.com/b"], [])
    assert parse_sitemap("not xml") == ([], [])


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


async def test_probe_ssr_finds_unlinked(api, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _client(cfg, rd)
    frontier = Frontier(rt.store)
    await probe_passive(client, api, cfg, rt.store, frontier, "anonymous")
    await probe_schemas(client, api, cfg, rt.store, "anonymous")
    routes = dict(_rows(rt.store, "SELECT r.pattern, s.source FROM routes r JOIN sources s "
                                  "ON s.item_kind='route' AND s.item_id=r.id"))
    assert routes["/secret-report"] == "robots"
    assert routes["/about"] == "sitemap"
    eps = _rows(rt.store, "SELECT e.method, e.path_pattern, s.source FROM endpoints e JOIN "
                          "sources s ON s.item_kind='endpoint' AND s.item_id=e.id")
    assert ("GET", "/api/internal/stats", "openapi") in eps
    # discovered URLs are queued for every configured role
    for role in ("admin", "alice", "anonymous"):
        urls = {r[0] for r in _rows(rt.store, f"SELECT url FROM frontier WHERE role='{role}'")}
        assert ssr_url + "/secret-report" in urls and ssr_url + "/about" in urls
    # passive exchanges carry origin passive + their source
    origins = _rows(rt.store, "SELECT DISTINCT origin FROM exchanges")
    assert ("passive",) in origins
    # idempotent
    before = _rows(rt.store, "SELECT count(*) FROM frontier")
    await probe_passive(client, api, cfg, rt.store, frontier, "anonymous")
    assert _rows(rt.store, "SELECT count(*) FROM frontier") == before
    rt.store.close()


async def test_probe_respects_deadline(api, ssr_url, make_cfg, run_dir, tmp_path):
    cfg = make_cfg(ssr_url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _client(cfg, rd)
    counts = await probe_passive(client, api, cfg, rt.store, Frontier(rt.store), "anonymous",
                                 deadline=time.monotonic() - 1)
    assert counts["requests"] == 0 and counts["timed_out"] == 1
    rt.store.close()


def test_cli_probe_passive(ssr_url, make_cfg, tmp_path, capsys):
    import json
    import sqlite3

    from appmap.cli import main

    cfg = make_cfg(ssr_url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    out = tmp_path / "my runs"
    assert main(["init", "--config", str(cfg.source_path), "--out", str(out)]) == 0
    run = next(out.iterdir())
    capsys.readouterr()
    assert main(["probe", "--run", str(run), "--phase", "passive"]) == 0
    st = json.loads(capsys.readouterr().out.strip().splitlines()[-1].removeprefix("APPMAP_STATUS "))
    assert st["ok"] and st["status"] == "done" and st["command"] == "probe"
    assert st["counts"]["routes"] >= 4 and st["counts"]["openapi_ops"] == 3
    assert st["next"].startswith("appmap crawl --run '")
    db = sqlite3.connect(next(run.glob("*.sqlite")))
    assert db.execute("select count(*) from frontier where url like '%/secret-report'"
                      ).fetchone()[0] == 1
    assert main(["probe", "--run", str(run), "--phase", "passive", "--budget-minutes", "0"]) == 0
    st = json.loads(capsys.readouterr().out.strip().splitlines()[-1].removeprefix("APPMAP_STATUS "))
    assert st["status"] == "in_progress" and "--phase passive" in st["next"]


def test_guessed_404s_leave_no_endpoints(ssr_url, make_cfg, tmp_path, capsys):
    import sqlite3

    from appmap.cli import main
    cfg = make_cfg(ssr_url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    assert main(["init", "--config", str(cfg.source_path), "--out", str(tmp_path / "r")]) == 0
    run = next((tmp_path / "r").iterdir())
    assert main(["probe", "--run", str(run), "--phase", "passive"]) == 0
    db = sqlite3.connect(next(run.glob("*.sqlite")))
    paths = {r[0] for r in db.execute("select path_pattern from endpoints")}
    assert not paths & {"/swagger.json", "/v3/api-docs", "/api-docs", "/manifest.json",
                        "/.well-known/security.txt", "/graphql", "/openapi.json"}
    assert db.execute("select count(*) from exchanges where status=404").fetchone()[0] >= 5


def test_probe_with_saved_cookie_session_never_persists_cookie(ssr_url, make_cfg, tmp_path,
                                                               monkeypatch, capsys):
    import json

    from appmap.cli import main
    secret = "SESSIONCOOKIEVALUE-9f3a1c77d2"
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url, roles=[{"name": "alice", "login": {
        "type": "form", "url": f"{ssr_url}/login", "username_env": "APPMAP_ALICE_USER",
        "password_env": "APPMAP_ALICE_PASS"}}])
    assert main(["init", "--config", str(cfg.source_path), "--out", str(tmp_path / "r")]) == 0
    run = next((tmp_path / "r").iterdir())
    state = tmp_path / "cache" / "absolute-appmap" / "sessions" / run.name / "alice.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"cookies": [{
        "name": "session", "value": secret, "domain": "127.0.0.1", "path": "/",
        "expires": -1, "httpOnly": True, "secure": False, "sameSite": "Lax"}], "origins": []}))
    assert main(["probe", "--run", str(run), "--phase", "passive"]) == 0
    seen = [p for p in run.rglob("*") if p.is_file() and secret.encode() in p.read_bytes()]
    assert seen == []
    import sqlite3
    db = sqlite3.connect(next(run.glob("*.sqlite")))
    assert db.execute("select count(*) from exchanges where role='alice' and origin='passive'"
                      ).fetchone()[0] > 0


async def test_catch_all_spa_guesses_not_attributed(api, stub_server, make_cfg, run_dir,
                                                    tmp_path):
    """I5: an SPA that answers every path with index.html must not turn guessed probe URLs
    (robots, sitemap, well-known, manifest, OpenAPI paths) into endpoints."""
    from werkzeug.wrappers import Request, Response

    @Request.application
    def catch_all(request):
        return Response("<!doctype html><title>SPA</title><div id=app></div>",
                        mimetype="text/html")
    srv = stub_server(catch_all)
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    rd = run_dir(tmp_path, cfg)
    rt, client = _client(cfg, rd)
    await probe_passive(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
    await probe_schemas(client, api, cfg, rt.store, "anonymous")
    assert _rows(rt.store, "SELECT count(*) FROM exchanges")[0][0] > 5  # they were fetched
    assert _rows(rt.store, "SELECT method, path_pattern FROM endpoints") == []
    rt.store.close()


_DJANGO_404 = """<!DOCTYPE html><html lang="en"><head>
  <title>Page not found at /robots.txt</title></head><body>
<div id="summary"><h1>Page not found <span>(404)</span></h1></div>
<div id="info">
    <p>Using the URLconf defined in <code>taskManager.urls</code>,
    Django tried these URL patterns, in this order:</p>
    <ol>
        <li>
            <code>admin/</code>
        </li>
        <li>
            <code>taskManager/</code>
            <code>
              [name='index']
            </code>
        </li>
        <li>
            <code>taskManager/</code>
            <code>forgot_password/ [name='forgot_password']</code>
        </li>
        <li>
            <code>taskManager/</code>
            <code>&lt;int:project_id&gt;/edit_project/ [name='edit_project']</code>
        </li>
        <li>
            <code>^legacy/</code>
            <code>^report/(?P&lt;report_id&gt;\\d+)/$ [name='report']</code>
        </li>
        <li>
            <code>^static/(?P&lt;path&gt;.*)$</code>
        </li>
    </ol>
    <p>The current path, <code>robots.txt</code>, didn't match any of these.</p>
</div>
<div id="explanation"><p>You're seeing this error because you have <code>DEBUG = True</code>
  in your Django settings file.</p></div></body></html>"""


def test_parse_django_urlconf():
    from appmap.probe.passive import parse_django_urlconf
    assert parse_django_urlconf(_DJANGO_404) == [
        "/admin/", "/taskManager/", "/taskManager/forgot_password/",
        "/taskManager/{project_id}/edit_project/", "/legacy/report/{report_id}/",
        "/static/{path}"]
    assert parse_django_urlconf("<p>Not Found</p>") == []


async def test_django_debug_404_routes_queued(api, stub_server, make_cfg, run_dir, tmp_path):
    from werkzeug.wrappers import Response
    srv = stub_server({"/robots.txt": lambda r: Response(_DJANGO_404, status=404,
                                                         mimetype="text/html")})
    cfg = make_cfg(srv.url)
    rd = run_dir(tmp_path, cfg)
    rt, client = _client(cfg, rd)
    counts = await probe_passive(client, api, cfg, rt.store, Frontier(rt.store), "anonymous")
    routes = dict(_rows(rt.store, "SELECT r.pattern, s.source FROM routes r JOIN sources s "
                                  "ON s.item_kind='route' AND s.item_id=r.id"))
    for p in ("/admin", "/taskManager/forgot_password",  # route patterns are normalized
              "/taskManager/{project_id}/edit_project", "/legacy/report/{report_id}"):
        assert routes[p] == "well_known"
    assert counts["routes"] >= 5
    for role in [r.name for r in cfg.roles]:
        urls = {r[0] for r in _rows(rt.store, f"SELECT url FROM frontier WHERE role='{role}'")}
        assert srv.url + "/taskManager/forgot_password/" in urls
        assert srv.url + "/admin/" in urls
        assert not any("{" in u or "project_id" in u for u in urls)  # only concrete paths
    rt.store.close()


_DJANGO_OLD_404 = """<html><body><div id="info">
  <p>Using the URLconf defined in <code>mysite.urls</code>,
  Django tried these URL patterns, in this order:</p>
  <ol>
      <li>^admin/ </li>
      <li>^taskManager/ ^$ [name='index']</li>
      <li>^taskManager/ ^forgot_password/$ [name='forgot_password']</li>
      <li>^$ [name='home']</li>
      <li>   </li>
      <li>[name='nameless']</li>
  </ol>
  <p>The current URL, <code>robots.txt</code>, didn't match any of these.</p></div>
</body></html>"""


def test_parse_django_urlconf_old_format_without_code():
    from appmap.probe.passive import parse_django_urlconf
    assert parse_django_urlconf(_DJANGO_OLD_404) == [
        "/admin/", "/taskManager/", "/taskManager/forgot_password/", "/"]


def test_parse_django_urlconf_skips_empty_items():
    from appmap.probe.passive import parse_django_urlconf
    page = ("<p>Using the URLconf defined in <code>x</code></p><ol><li><code></code></li>"
            "<li><code>[name='i']</code></li><li><code>a/</code></li></ol>")
    assert parse_django_urlconf(page) == ["/a/"]
