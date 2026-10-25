import json
import urllib.request

from appmap.browser.forms import discover_forms, fill_and_submit
from appmap.browser.settle import wait_settled
from appmap.datagen import DataGen


async def _ssr_page(browser, ssr_url):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(ssr_url + "/login")
    await page.fill("input[name=username]", "admin")
    await page.fill("input[name=password]", "admin-pw-Fx1")
    await page.click("button[type=submit]")
    await page.wait_for_url("**/projects")
    await page.goto(ssr_url + "/projects/new")
    return ctx, page


async def _spa_page(browser, spa_url):
    req = urllib.request.Request(
        spa_url + "/api/login", method="POST",
        data=json.dumps({"username": "admin", "password": "admin-pw-Fx1"}).encode(),
        headers={"Content-Type": "application/json"})
    data = json.loads(urllib.request.urlopen(req, timeout=5).read())
    ctx = await browser.new_context()
    await ctx.add_init_script(
        f"localStorage.setItem('token', {json.dumps(data['token'])});"
        f"localStorage.setItem('role', {json.dumps(data['role'])});"
        "localStorage.setItem('username', 'admin');")
    return ctx, await ctx.new_page()


async def test_ssr_discovery_fields(browser, ssr_url):
    ctx, page = await _ssr_page(browser, ssr_url)
    try:
        forms = [f for f in await discover_forms(page) if f.action.endswith("/projects/new")]
        assert len(forms) == 1
        form = forms[0]
        assert not form.inferred and form.method == "POST" and not form.has_confirm
        by = {f.name: f for f in form.fields}
        assert set(by) == {"name", "visibility", "due"}
        assert by["name"].required and by["name"].pattern == "[a-z0-9-]{3,40}"
        assert by["visibility"].widget == "select" and by["visibility"].options == ["public", "private"]
        assert by["due"].widget == "date" and by["name"].locators and form.submit_locators
    finally:
        await ctx.close()


async def test_ssr_new_project_submits(browser, ssr_url, _ssr_server):
    ctx, page = await _ssr_page(browser, ssr_url)
    try:
        form = next(f for f in await discover_forms(page) if f.action.endswith("/projects/new"))
        res = await fill_and_submit(page, form, DataGen("appmap-t1", {}), max_retries=2, settle_ms=200)
        assert res.success and res.attempts == 1 and res.validation_messages == {}
        assert res.to_url.endswith("/projects/26") and res.exchange_ids == []
        assert ("POST", "/projects/new") in _ssr_server.requests
        assert "appmap-t1" in await page.content()
    finally:
        await ctx.close()


async def test_validation_retry_recorded(browser, ssr_url):
    ctx, page = await _ssr_page(browser, ssr_url)
    try:
        form = next(f for f in await discover_forms(page) if f.action.endswith("/projects/new"))
        marker = "appmap-20261024-120000"
        res = await fill_and_submit(page, form, DataGen(marker, {}), max_retries=2, settle_ms=200,
                                    exchange_ids_since=lambda: [7, 8])
        assert res.success and res.attempts == 2
        assert res.validation_messages == {"name": ["Name must be at most 12 characters"]}
        assert res.exchange_ids == [7, 8]
        assert marker[:12] in await page.content()
        # I7: both attempts' values are returned (the second one truncated by the hint)
        assert len(res.values_tried["name"]) == 2 and res.values_tried["name"][1] == marker[:12]
    finally:
        await ctx.close()


async def test_failure_without_usable_hint_stops(browser, ssr_url):
    ctx, page = await _ssr_page(browser, ssr_url)
    try:
        form = next(f for f in await discover_forms(page) if f.action.endswith("/projects/new"))
        gen = DataGen("appmap-t", {"fields": {"^name$": "x" * 20}})  # fixture ignores hints
        res = await fill_and_submit(page, form, gen, max_retries=3, settle_ms=200)
        assert not res.success and res.attempts == 2  # one retry; identical hint then stops
        assert "name" in res.validation_messages
    finally:
        await ctx.close()


async def test_spa_inferred_form_with_combobox(browser, spa_url):
    ctx, page = await _spa_page(browser, spa_url)
    posts = []
    page.on("request", lambda r: posts.append(r) if r.method == "POST" and r.url.endswith("/api/items") else None)
    try:
        await page.goto(spa_url + "/items")
        await page.wait_for_selector(".card")
        assert await wait_settled(page, 300)
        forms = [f for f in await discover_forms(page) if f.inferred]
        assert len(forms) == 1
        form = forms[0]
        assert form.method is None and form.action is None
        assert {f.widget for f in form.fields} == {"input", "combobox"}
        assert next(f for f in form.fields if f.widget == "input").name == "Title"
        res = await fill_and_submit(page, form, DataGen("appmap-spa1", {}), max_retries=1, settle_ms=300)
        assert res.success and res.attempts == 1, res
        assert len(posts) == 1
        body = json.loads(posts[0].post_data)
        assert "appmap-spa1" in body["title"] and body["category"] == "general"
    finally:
        await ctx.close()


async def test_misc_widgets_and_method_override(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content(
        '<form method="post" action="/x"><input type="hidden" name="_method" value="put">'
        '<input name="email" type="email"><textarea name="bio"></textarea>'
        '<input type="checkbox" name="agree"><input type="radio" name="r" value="a"><input type="radio" name="r" value="b">'
        '<input type="file" name="doc" accept="image/*"><div contenteditable id="rt"></div>'
        '<button type="submit" onclick="return confirm(\'sure?\')">Go</button></form>')
    form = (await discover_forms(page))[0]
    await ctx.close()
    assert form.method == "PUT" and form.has_confirm
    w = {f.name: f.widget for f in form.fields}
    assert w == {"email": "input", "bio": "textarea", "agree": "checkbox", "r": "radio",
                 "doc": "file", "rt": "richtext"}
    assert next(f for f in form.fields if f.name == "r").options == ["a", "b"]


async def test_fill_all_widget_kinds(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content(
        '<form onsubmit="return false"><input name="email" type="email"><input type="checkbox" name="agree">'
        '<input type="radio" name="r" value="a"><input type="radio" name="r" value="b">'
        '<input type="file" name="doc" accept="image/*"><div contenteditable id="rt"></div>'
        '<select name="s"><option value="">--</option><option value="one">1</option></select>'
        '<button type="submit">Save</button></form>')
    form = (await discover_forms(page))[0]
    gen = DataGen("appmap-w", {})
    from appmap.browser.forms import _fill_field
    for f in form.fields:
        assert await _fill_field(page, f, gen.value_for(f, 1)), f.name
    state = await page.evaluate(
        "() => ({e: document.querySelector('[name=email]').value, c: document.querySelector('[name=agree]').checked,"
        " r: document.querySelector('[name=r]:checked').value, f: document.querySelector('[name=doc]').files[0].name,"
        " rt: document.getElementById('rt').innerText, s: document.querySelector('select').value})")
    await ctx.close()
    assert state == {"e": "appmap-w@example.test", "c": True, "r": "a", "f": "sample.png",
                     "rt": "appmap-w", "s": "one"}


_RESET_FORM = """<form id=f onsubmit="event.preventDefault();
  fetch('/api', {method: 'POST', body: new FormData(this)}).then(() => { this.reset(); %s });">
  <input name=title required><button type=submit>Save</button></form><div id=out></div>"""


def _api_server(stub_server, body: str):
    from werkzeug.wrappers import Response
    return stub_server({"/": lambda r: Response(body, mimetype="text/html"),
                        "/api": lambda r: Response("{}", status=201, mimetype="application/json")})


async def test_form_reset_after_success_is_not_failure(browser, stub_server):
    srv = _api_server(stub_server, _RESET_FORM % "")
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(srv.url + "/")
    form = (await discover_forms(page))[0]
    res = await fill_and_submit(page, form, DataGen("appmap-r", {}), max_retries=2, settle_ms=200)
    await ctx.close()
    assert res.success and res.attempts == 1
    assert srv.requests.count(("POST", "/api")) == 1


async def test_success_toast_with_role_alert_is_success(browser, stub_server):
    toast = "document.getElementById('out').innerHTML = '<div role=alert>Created!</div>';"
    srv = _api_server(stub_server, _RESET_FORM % toast)
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(srv.url + "/")
    form = (await discover_forms(page))[0]
    res = await fill_and_submit(page, form, DataGen("appmap-r", {}), max_retries=2, settle_ms=200)
    await ctx.close()
    assert res.success and res.attempts == 1 and res.validation_messages == {}


async def test_alert_error_text_still_fails(browser, stub_server):
    from werkzeug.wrappers import Response
    body = ('<form onsubmit="event.preventDefault(); fetch(\'/api\', {method: \'POST\'}).then(() =>'
            ' document.getElementById(\'out\').innerHTML = \'<div role=alert>Title is required</div>\');">'
            '<input name=title><button type=submit>Save</button></form><div id=out></div>')
    srv = stub_server({"/": lambda r: Response(body, mimetype="text/html"),
                       "/api": lambda r: Response("{}", status=200, mimetype="application/json")})
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(srv.url + "/")
    form = (await discover_forms(page))[0]
    res = await fill_and_submit(page, form, DataGen("appmap-r", {}), max_retries=0, settle_ms=200)
    await ctx.close()
    assert not res.success and res.validation_messages


async def test_navigation_on_submit_does_not_hang(browser, stub_server):
    import time
    from werkzeug.wrappers import Response
    srv = stub_server({
        "/": lambda r: Response("<form action=/next method=get><input name=q><button>Search</button></form>",
                                mimetype="text/html"),
        "/next": lambda r: Response("<h1>done</h1>", mimetype="text/html")})
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(srv.url + "/")
    form = (await discover_forms(page))[0]
    t0 = time.monotonic()
    res = await fill_and_submit(page, form, DataGen("appmap-n", {}), max_retries=1, settle_ms=200)
    await ctx.close()
    assert time.monotonic() - t0 < 10
    assert res.success and res.to_url.endswith("/next?q=appmap-n")


async def test_range_and_color_use_fill(browser):
    from appmap.browser.forms import _fill_field
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content('<form><input type=range name=r min=0 max=10><input type=color name=c>'
                           '<button>Save</button></form>')
    form = (await discover_forms(page))[0]
    gen = DataGen("appmap-c", {})
    for f in form.fields:
        assert await _fill_field(page, f, gen.value_for(f, 1)), f.name
    vals = await page.evaluate("[document.querySelector('[name=r]').value, document.querySelector('[name=c]').value]")
    await ctx.close()
    assert vals == ["5", "#336699"]


async def test_values_tried_redacts_passwords(browser, stub_server):
    srv = _api_server(stub_server, "<form onsubmit=\"event.preventDefault();fetch('/api',"
                      "{method:'POST'})\"><input name=title><input type=password name=pin>"
                      "<button type=submit>Save</button></form>")
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.goto(srv.url + "/")
    form = (await discover_forms(page))[0]
    res = await fill_and_submit(page, form, DataGen("appmap-r", {}), max_retries=0, settle_ms=200)
    await ctx.close()
    assert res.values_tried["pin"] == ["<redacted:password>"]
    assert res.values_tried["title"] and "<redacted" not in str(res.values_tried["title"])
