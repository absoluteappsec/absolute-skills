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
