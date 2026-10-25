from appmap.browser.fingerprint import SKELETON_JS, dom_fingerprint, state_id


async def _fp(browser, html):
    ctx = await browser.new_context()
    try:
        page = await ctx.new_page()
        await page.set_content(html)
        return dom_fingerprint(await page.evaluate(SKELETON_JS))
    finally:
        await ctx.close()


async def test_fingerprint_ignores_text_and_counts(browser):
    a = await _fp(browser, "<main><h1>Hello</h1><ul><li>a</li><li>b</li></ul></main>")
    b = await _fp(browser, "<main><h1>Other words</h1><ul><li>x</li><li>y</li><li>z</li><li>w</li></ul></main>")
    assert a == b and len(a) == 16


async def test_fingerprint_differs_on_structure(browser):
    a = await _fp(browser, "<main><h1>Hello</h1><ul><li>a</li></ul></main>")
    b = await _fp(browser, "<main><h1>Hello</h1><ol><li>a</li></ol><button>x</button></main>")
    assert a != b


def test_state_id_stable_and_role_sensitive():
    s = state_id("/items/:id", "abc", "admin")
    assert s.startswith("st-") and len(s) == 15
    assert s == state_id("/items/:id", "abc", "admin")
    assert s != state_id("/items/:id", "abc", "alice")
