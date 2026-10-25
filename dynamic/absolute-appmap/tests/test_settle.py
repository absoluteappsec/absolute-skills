from appmap.browser.settle import install, wait_settled


async def test_settled_on_static_page(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content("<p>hi</p>")
    assert await wait_settled(page, 100, timeout_ms=3000) is True
    await ctx.close()


async def test_not_settled_while_mutating(browser):
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await page.set_content("<p id=p></p><script>setInterval(()=>p.textContent=Math.random(),50)</script>")
    assert await wait_settled(page, 300, timeout_ms=1200) is False
    await ctx.close()


async def test_waits_for_inflight_request(browser, stub_server):
    import asyncio, time
    from werkzeug.wrappers import Response

    def slow(req):
        time.sleep(0.8)
        return Response("ok")
    srv = stub_server({"/slow": slow, "/": lambda r: Response(
        "<script>fetch('/slow')</script>", mimetype="text/html")})
    ctx = await browser.new_context()
    page = await ctx.new_page()
    await install(page)
    t0 = asyncio.get_event_loop().time()
    await page.goto(srv.url + "/")
    assert await wait_settled(page, 100, timeout_ms=5000) is True
    assert asyncio.get_event_loop().time() - t0 >= 0.7
    await ctx.close()
