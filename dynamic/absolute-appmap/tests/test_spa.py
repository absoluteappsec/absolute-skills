import json

from appmap.crawl import run_crawl
from appmap.spa.detect import detect_spa
from appmap.spa.routes import (HISTORY_PATCH_JS, extract_routes,
                               parse_next_build_manifest)
from appmap.store import Store

FAST = {"max_requests_per_second": 200, "quiet_window_ms": 150,
        "max_states_per_route_pattern": 3}


def _roles(spa_url):
    return [{"name": "admin", "login": {
        "type": "form", "url": f"{spa_url}/", "username_env": "APPMAP_ADMIN_USER",
        "password_env": "APPMAP_ADMIN_PASS", "success_selector": "text=Signed in as"}}]


async def _loaded(browser, url):
    page = await browser.new_page()
    await page.goto(url)
    await page.wait_for_selector("h2")
    return page


async def _no_fetch(url):
    return None


async def test_detect_vue_router(browser, spa_url):
    page = await _loaded(browser, spa_url)
    try:
        assert await detect_spa(page) == {"framework": "vue", "has_router": True}
    finally:
        await page.close()


async def test_detect_no_spa(browser):
    page = await browser.new_page()
    try:
        await page.set_content("<h1>static</h1>")
        assert await detect_spa(page) == {"framework": "unknown", "has_router": False}
    finally:
        await page.close()


async def test_extract_vue_routes(browser, spa_url):
    page = await _loaded(browser, spa_url)
    try:
        routes = await extract_routes(page, spa_url, _no_fetch)
    finally:
        await page.close()
    assert {r.pattern for r in routes} == {"/", "/items", "/items/{id}", "/settings", "/admin"}
    assert {r.source for r in routes} == {"spa_router"}


def test_next_build_manifest_parsing():
    text = ('self.__BUILD_MANIFEST = {"__rewrites":{"beforeFiles":[],"afterFiles":[]},'
            '"/":["static/chunks/pages/index-1.js"],"/_app":["static/chunks/pages/_app-2.js"],'
            '"/blog/[slug]":["static/chunks/pages/blog/[slug]-3.js"],'
            '"/docs/[...parts]":["x.js"],"/_error":["e.js"],'
            'sortedPages:["/","/_app","/_error","/blog/[slug]","/about"]};'
            'self.__BUILD_MANIFEST_CB && self.__BUILD_MANIFEST_CB()')
    assert parse_next_build_manifest(text) == ["/", "/blog/{slug}", "/docs/{parts}", "/about"]


async def test_history_patch_records_client_navigation(browser, spa_url):
    ctx = await browser.new_context()
    try:
        await ctx.add_init_script(HISTORY_PATCH_JS)
        page = await ctx.new_page()
        await page.goto(spa_url)
        await page.evaluate("history.pushState({}, '', '/items'); location.hash = '#x';")
        await page.wait_for_timeout(100)
        nav = await page.evaluate("window.__appmap_nav")
        types = [n["type"] for n in nav]
        assert "pushState" in types and types[-1] == "hashchange"
        assert nav[types.index("pushState")]["url"].endswith("/items")
    finally:
        await ctx.close()


async def test_unlinked_settings_route_crawled(browser, spa_url, make_cfg, run_dir, tmp_path,
                                               monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(spa_url, roles=_roles(spa_url), limits=FAST)
    rd = run_dir(tmp_path, cfg)
    stats = await run_crawl(cfg, rd, roles=["admin"], budget_minutes=5, browser=browser)
    assert stats.status == "done"
    store = Store(rd.db_path)
    try:
        q = lambda sql, *a: [dict(r) for r in store.conn.execute(sql, a)]
        assert q("select 1 from states where route_pattern='/settings'")
        spa = {r["pattern"] for r in q("select pattern from routes where kind='spa_route'")}
        assert {"/settings", "/items/{id}", "/admin"} <= spa
        assert q("select 1 from sources s join routes r on r.id=s.item_id "
                 "where s.item_kind='route' and s.source='spa_router' and r.pattern='/settings'")
        assert q("select 1 from frontier where url like '%/settings' and source='spa_router'")
    finally:
        store.close()
