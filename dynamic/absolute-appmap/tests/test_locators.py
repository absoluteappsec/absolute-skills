from appmap.browser.locators import LOCATOR_JS, rank_locators, resolve


def test_locator_ranking_prefers_testid():
    ranked = rank_locators({"testid": "save", "role": "button", "name": "Save", "label": "",
                            "text": "Save", "css": "#save", "xpath": "/html/body/button"})
    assert [r["strategy"] for r in ranked] == ["testid", "role_name", "text", "css", "xpath"]
    assert [r["rank"] for r in ranked] == [0, 1, 2, 3, 4]
    assert ranked[0]["value"] == "save"


async def test_locator_js_extracts_testid_and_role(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content('<button data-cy="go" id="b">Go now</button>')
    raw = await page.evaluate("(el) => (" + LOCATOR_JS + ")(el)", await page.query_selector("#b"))
    await ctx.close()
    assert raw["testid"] == "go" and raw["role"] == "button" and raw["name"] == "Go now"
    assert raw["css"] and raw["xpath"]


async def test_resolve_falls_back_to_css(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content('<button>Dup</button><button id="x">Dup</button>')
    locs = rank_locators({"testid": "", "role": "button", "name": "Dup", "label": "",
                          "text": "Dup", "css": "#x", "xpath": ""})
    loc = await resolve(page, locs)
    assert loc is not None and await loc.get_attribute("id") == "x"
    assert await resolve(page, [{"strategy": "css", "value": "#nope", "rank": 0}]) is None
    await ctx.close()
