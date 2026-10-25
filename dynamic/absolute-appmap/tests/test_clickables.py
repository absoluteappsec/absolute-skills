import json
import urllib.request

from appmap.browser.clickables import enumerate_clickables, expand_reveals
from appmap.browser.settle import wait_settled


async def _login_page(browser, spa_url):
    req = urllib.request.Request(
        spa_url + "/api/login", method="POST",
        data=json.dumps({"username": "admin", "password": "admin-pw-Fx1"}).encode(),
        headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=5).read())
    ctx = await browser.new_context()
    await ctx.add_init_script(
        f"localStorage.setItem('token', {json.dumps(data['token'])});"
        f"localStorage.setItem('role', {json.dumps(data['role'])});"
        f"localStorage.setItem('username', 'admin');")
    return ctx, await ctx.new_page()


async def test_spa_div_click_found(browser, spa_url):
    ctx, page = await _login_page(browser, spa_url)
    try:
        await page.goto(spa_url + "/items")
        await page.wait_for_selector(".card")
        assert await wait_settled(page, 300)
        cards = await page.locator(".card").all_inner_texts()
        found = await enumerate_clickables(page)
        hits = [c for c in found if c.reason == "listener" and c.label == cards[0].strip()]
        assert hits, [(c.tag, c.reason, c.label) for c in found]
        assert hits[0].locators and hits[0].href is None
    finally:
        await ctx.close()


async def test_enumerate_reasons_and_dedup(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content(
        '<a href="/x">Link</a><div role="tab">T</div><span onclick="1">O</span>'
        '<p style="cursor:pointer">P</p>')
    found = {c.label: c for c in await enumerate_clickables(page)}
    await ctx.close()
    assert found["Link"].href == "/x" and found["Link"].reason == "anchor"
    assert found["T"].reason == "role" and found["O"].reason == "onclick"
    assert found["P"].reason == "cursor"


async def test_expand_reveals_skips_destructive(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content(
        '<button aria-expanded="false" onclick="this.setAttribute(\'aria-expanded\',\'true\')">More</button>'
        '<button aria-expanded="false" id="d" onclick="window.hit=1">Delete account</button>'
        '<details><summary>Sum</summary>hidden</details>')
    n = await expand_reveals(page)
    hit = await page.evaluate("window.hit")
    is_open = await page.evaluate("document.querySelector('details').open")
    await ctx.close()
    assert n == 2 and hit is None and is_open


async def test_same_label_rows_not_merged(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content(
        "<table>" + "".join(f"<tr><td>r{i}</td><td><button id=b{i}>Edit</button></td></tr>"
                            for i in range(3)) + "</table>"
        '<a href="/a">Same</a><a href="/b">Same</a>')
    from appmap.browser.locators import resolve
    found = await enumerate_clickables(page)
    edits = [c for c in found if c.label == "Edit"]
    assert len(edits) == 3
    ids = set()
    for c in edits:
        loc = await resolve(page, c.locators)
        assert loc is not None
        ids.add(await loc.get_attribute("id"))
    assert ids == {"b0", "b1", "b2"}
    assert len([c for c in found if c.label == "Same"]) == 2
    await ctx.close()


async def test_native_button_without_cursor_or_listener(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content('<form><button type=submit>Go</button><select><option>a</option></select></form>')
    tags = {c.tag for c in await enumerate_clickables(page)}
    await ctx.close()
    assert {"button", "select"} <= tags


async def test_cap_warns_and_still_finds_button(browser, caplog):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content("<div></div>" * 2100 + "<button>Late</button>")
    with caplog.at_level("WARNING"):
        found = await enumerate_clickables(page)
    await ctx.close()
    assert any(c.label == "Late" for c in found)
    assert "cap" in caplog.text


async def test_expand_reveals_guard_uses_href_and_title(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content(
        '<a href="/logout" aria-expanded="false" onclick="window.a=1;return false"><svg></svg></a>'
        '<button aria-expanded="false" title="Delete" onclick="window.b=1"><svg></svg></button>')
    n = await expand_reveals(page)
    hit = await page.evaluate("[window.a, window.b]")
    await ctx.close()
    assert n == 0 and hit == [None, None]
