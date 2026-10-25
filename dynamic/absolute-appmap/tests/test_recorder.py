import asyncio
import json

from werkzeug.wrappers import Response

from test_session import Harness, rows

from appmap.browser.session import login


async def test_browser_external_redirect_aborted(browser, ssr_url, make_cfg, run_dir,
                                                 tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    seen: list[str] = []
    sess.context.on("request", lambda r: seen.append(r.url))
    page = await h.recorder.new_page(sess.context)
    try:
        await page.goto(f"{ssr_url}/go-external")
    except Exception:
        pass  # the aborted redirect surfaces as a navigation error
    await h.recorder.drain()
    links = rows(h, "SELECT url FROM external_links")
    assert [link["url"] for link in links] == ["https://external.invalid/"]
    assert not any(r["url"].startswith("https://external.invalid")
                   for r in rows(h, "SELECT url FROM exchanges"))
    assert any(u.startswith(ssr_url) for u in seen)
    assert not any("external.invalid" in u for u in seen)
    await sess.context.close()


async def test_har_redacted(browser, ssr_url, make_cfg, run_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    rd = run_dir(tmp_path, cfg)
    h = Harness(cfg, rd)
    sess = await h.open(browser, "admin")
    assert await login(sess, cfg, h.events)
    cookies = await sess.context.cookies()
    sid = next(c["value"] for c in cookies if c["name"] == "sid")
    page = await h.recorder.new_page(sess.context)
    await page.goto(f"{ssr_url}/projects")
    await h.recorder.drain()
    await sess.context.close()
    har_path = h.recorder.finalize_har("admin")
    text = har_path.read_text()
    json.loads(text)
    assert har_path == rd.traffic_dir / "admin.har"
    assert "admin-pw-Fx1" not in text
    assert sid not in text
    assert "<redacted:cookie>" in text
    # exchanges and events are clean too
    assert sid not in rd.events_path.read_text()
    assert "admin-pw-Fx1" not in rd.events_path.read_text()


async def test_static_subresources_skip_limiter(browser, stub_server, make_cfg, run_dir,
                                                tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    srv = stub_server({
        "/": lambda r: Response("<img src='/x.png'><script src='/app.js'></script>",
                                mimetype="text/html"),
        "/x.png": lambda r: Response(b"\x89PNG", mimetype="image/png"),
        "/app.js": lambda r: Response("fetch('/api/data')", mimetype="text/javascript"),
        "/api/data": lambda r: Response("{}", mimetype="application/json"),
    })
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    h = Harness(cfg, run_dir(tmp_path, cfg))
    calls = []
    real = h.gate.admit

    async def spy(method, url, **kw):
        calls.append(url)
        return await real(method, url, **kw)

    h.gate.admit = spy
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(srv.url + "/")
    await page.wait_for_load_state("networkidle")
    await h.recorder.drain()
    paths_hit = [p for _, p in srv.requests]
    assert "/x.png" in paths_hit and "/app.js" in paths_hit  # assets really loaded
    assert sorted(calls) == [srv.url + "/", srv.url + "/api/data"]  # but never admitted
    await sess.context.close()


async def test_multi_hop_redirect_to_external_blocked(browser, stub_server, make_cfg,
                                                      run_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ext = stub_server({"/x": lambda r: Response("external", mimetype="text/html")})
    ext_url = ext.url.replace("127.0.0.1", "localhost") + "/x"
    srv = stub_server({
        "/a": lambda r: Response(status=302, headers={"Location": "/b"}),
        "/b": lambda r: Response(status=302, headers={"Location": ext_url}),
    })
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    try:
        await page.goto(srv.url + "/a")
    except Exception:
        pass
    await h.recorder.drain()
    assert ("GET", "/b") in srv.requests  # in-scope hop 2 still followed
    assert ext.requests == []
    assert [r["url"] for r in rows(h, "SELECT url FROM external_links")] == [ext_url]
    await sess.context.close()


async def test_redirect_hop_onto_deny_pattern_blocked(browser, stub_server, make_cfg,
                                                      run_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    srv = stub_server({
        "/a": lambda r: Response(status=302, headers={"Location": "/b"}),
        "/b": lambda r: Response(status=302, headers={"Location": "/denied/x"}),
        "/denied/x": lambda r: Response("secret", mimetype="text/html"),
    })
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   scope={"deny_patterns": ["/denied*"]})
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    try:
        await page.goto(srv.url + "/a")
    except Exception:
        pass
    await h.recorder.drain()
    assert ("GET", "/b") in srv.requests
    assert ("GET", "/denied/x") not in srv.requests
    await sess.context.close()


async def test_har_opaque_tokens_in_bodies_redacted(browser, stub_server, make_cfg, run_dir,
                                                    tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    srv = stub_server({
        "/api/login": lambda r: Response(
            json.dumps({"token": "opaque-tok-12345", "user": "bob"}),
            mimetype="application/json"),
        "/": lambda r: Response("<p>hi</p>", mimetype="text/html"),
    })
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    rd = run_dir(tmp_path, cfg)
    h = Harness(cfg, rd)
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(srv.url + "/")
    await page.evaluate("""() => fetch('/api/login', {method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded'},
        body: 'user=bob&token=opaque-form-999'}).then(r => r.text())""")
    await h.recorder.drain()
    await sess.context.close()
    text = h.recorder.finalize_har("anonymous").read_text()
    assert "opaque-tok-12345" not in text and "opaque-form-999" not in text
    assert "bob" in text  # customer data is kept
    har = json.loads(text)
    bodies = [e["response"]["content"].get("text", "") for e in har["log"]["entries"]]
    assert any("<redacted:secret>" in b for b in bodies)


async def _echo_ws_server():
    """Minimal RFC 6455 server: handshake, echo one text frame, close."""
    import base64
    import hashlib

    async def handle(reader, writer):
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        key = next(line.split(":", 1)[1].strip() for line in head.split("\r\n")
                   if line.lower().startswith("sec-websocket-key"))
        accept = base64.b64encode(hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        await writer.drain()
        b0, b1 = await reader.readexactly(2)
        n = b1 & 0x7F
        mask = await reader.readexactly(4)
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(await reader.readexactly(n)))
        reply = b"echo:" + data
        writer.write(bytes([0x81, len(reply)]) + reply)
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


async def test_websocket_frames_recorded(browser, ssr_url, make_cfg, run_dir, tmp_path,
                                         monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    cfg = make_cfg(ssr_url)
    h = Harness(cfg, run_dir(tmp_path, cfg))
    server = await _echo_ws_server()
    port = server.sockets[0].getsockname()[1]
    # Chromium's local-network-access checks block loopback websockets; relax for this test.
    ws_browser = await browser.browser_type.launch(args=[
        "--disable-features=LocalNetworkAccessChecks,LocalNetworkAccessChecksWebSockets,"
        "BlockInsecurePrivateNetworkRequests,PrivateNetworkAccessChecks"])
    sess = await h.open(ws_browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(f"{ssr_url}/about")
    result = await asyncio.wait_for(page.evaluate("""(u) => new Promise(res => {
        const s = new WebSocket(u);
        s.onopen = () => s.send('hello');
        s.onmessage = () => res();
        s.onerror = () => res('error');
    })""", f"ws://127.0.0.1:{port}/ws"), 10)
    assert result != "error"
    ev = [e for e in h.events.iter() if e.type == "websocket"]
    dirs = [e.data["direction"] for e in ev]
    assert "open" in dirs and "sent" in dirs and "received" in dirs
    assert any(e.data.get("payload") == "echo:hello" for e in ev)
    assert rows(h, "SELECT kind FROM endpoints WHERE kind='websocket'")
    await sess.context.close()
    await ws_browser.close()
    server.close()


def _chain_servers(stub_server, first_html=None):
    ext = stub_server({"/x": lambda r: Response("external", mimetype="text/html")})
    ext_url = ext.url.replace("127.0.0.1", "localhost") + "/x"
    routes = {
        "/a": lambda r: Response(status=302, headers={"Location": "/b"}),
        "/b": lambda r: Response(status=302, headers={"Location": ext_url}),
    }
    if first_html:
        routes["/p"] = lambda r: Response(first_html, mimetype="text/html")
    return ext, ext_url, stub_server(routes)


async def test_popup_multi_hop_redirect_blocked(browser, stub_server, make_cfg, run_dir,
                                                tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ext, ext_url, srv = _chain_servers(
        stub_server, "<a id=l target=_blank href='/a'>go</a>")
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(srv.url + "/p")
    async with sess.context.expect_page() as popup_info:
        await page.click("#l")
    popup = await popup_info.value
    try:
        await popup.wait_for_load_state()
    except Exception:
        pass
    await asyncio.sleep(0.5)
    await h.recorder.drain()
    assert ("GET", "/b") in srv.requests
    assert ext.requests == []
    assert [r["url"] for r in rows(h, "SELECT url FROM external_links")] == [ext_url]
    await sess.context.close()


async def test_plain_new_page_goto_multi_hop_blocked(browser, stub_server, make_cfg,
                                                     run_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ext, _, srv = _chain_servers(stub_server)
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    page = await sess.context.new_page()  # not recorder.new_page: gate armed by the route
    try:
        await page.goto(srv.url + "/a")
    except Exception:
        pass
    await h.recorder.drain()
    assert ("GET", "/b") in srv.requests
    assert ext.requests == []
    await sess.context.close()


async def test_finalize_har_survives_malformed_base64(make_cfg, run_dir, tmp_path, ssr_url):
    cfg = make_cfg(ssr_url)
    rd = run_dir(tmp_path, cfg)
    h = Harness(cfg, rd)
    har = {"log": {"entries": [
        {"request": {"method": "GET", "url": "http://x/", "headers": []},
         "response": {"status": 200, "headers": [], "content": {
             "mimeType": "application/json", "encoding": "base64", "text": "!!notbase64!!"}}},
        {"request": {"method": "GET", "url": "http://x/2", "headers": []},
         "response": {"status": 200, "headers": [], "content": {
             "mimeType": "text/plain", "text": "pw admin-pw-Fx1"}}}]}}
    path = rd.traffic_dir / "admin.har"
    path.write_text(json.dumps(har))
    h.recorder.finalize_har("admin")
    out = json.loads(path.read_text())
    texts = [e["response"]["content"]["text"] for e in out["log"]["entries"]]
    assert texts[0] == "<redacted:undecodable>"
    assert "admin-pw-Fx1" not in texts[1]
    assert not list(rd.traffic_dir.glob("*.tmp"))


async def _armed_chain_page(browser, stub_server, make_cfg, run_dir, tmp_path, extra=None):
    ext, ext_url, srv = _chain_servers(stub_server)
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    return ext, ext_url, srv, h, sess


async def test_fetch_multi_hop_cross_origin_redirect_blocked(browser, stub_server, make_cfg,
                                                             run_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ext, ext_url, srv, h, sess = await _armed_chain_page(
        browser, stub_server, make_cfg, run_dir, tmp_path)
    page = await h.recorder.new_page(sess.context)
    await page.route(srv.url + "/blank", lambda r: r.fulfill(body="x", content_type="text/html"))
    await page.goto(srv.url + "/blank")
    res = await page.evaluate("(u) => fetch(u).then(r => r.status, () => 'blocked')", srv.url + "/a")
    assert res == "blocked"
    xhr = await page.evaluate("""(u) => new Promise(res => {
        const x = new XMLHttpRequest(); x.open('GET', u);
        x.onload = () => res('ok'); x.onerror = () => res('blocked'); x.send(); })""",
                              srv.url + "/a")
    assert xhr == "blocked"
    await h.recorder.drain()
    assert ext.requests == []
    assert [r["url"] for r in rows(h, "SELECT url FROM external_links")] == [ext_url]
    await sess.context.close()


async def test_opener_driven_popup_fetch_blocked(browser, stub_server, make_cfg, run_dir,
                                                 tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ext, ext_url, srv = _chain_servers(stub_server)
    srv2 = stub_server({
        "/a": lambda r: Response(status=302, headers={"Location": "/b"}),
        "/b": lambda r: Response(status=302, headers={"Location": ext_url}),
        "/q": lambda r: Response(
            "<script>let w=window.open('');let n=0;"
            "let t=setInterval(()=>{try{w.fetch(location.origin+'/a')}catch(e){};"
            "if(++n>300)clearInterval(t)},0);"
            "setTimeout(()=>{w.location='/a'},50)</script>", mimetype="text/html")})
    cfg = make_cfg(srv2.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(srv2.url + "/q")
    await page.wait_for_timeout(1500)
    await h.recorder.drain()
    assert ext.requests == []
    await sess.context.close()


async def test_unsafe_redirect_hop_refused_without_classifier(browser, stub_server, make_cfg,
                                                              run_dir, tmp_path, monkeypatch):
    """I1: outside the crawler (no redirect_guard), a 307 re-sending a POST is refused."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    srv = stub_server({
        "/": lambda r: Response("<form method='post' action='/save'><button>Go</button></form>",
                                mimetype="text/html"),
        "/save": lambda r: Response(status=307, headers={"Location": "/items/7/delete"}),
        "/items/7/delete": lambda r: Response("deleted", mimetype="text/html"),
    })
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(srv.url + "/")
    try:
        await page.click("button")
        await page.wait_for_timeout(500)
    except Exception:
        pass
    await h.recorder.drain()
    assert ("POST", "/save") in srv.requests
    assert ("POST", "/items/7/delete") not in srv.requests
    await sess.context.close()


async def test_login_and_static_allow_hosts_in_browser(browser, stub_server, make_cfg, run_dir,
                                                       tmp_path, monkeypatch):
    """I2: a CDN script loads (static_allow_hosts) and an IdP page opens from a login page
    (login_allow_hosts); neither host is reachable otherwise or recorded as an endpoint."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    ext = stub_server({
        "/lib.js": lambda r: Response("document.title = 'cdn-loaded'",
                                      mimetype="application/javascript"),
        "/sso": lambda r: Response("<title>IdP</title>", mimetype="text/html")})
    ext_base = f"http://localhost:{ext.port}"
    app = stub_server({"/": lambda r: Response(
        f"<title>app</title><script src='{ext_base}/lib.js'></script>", mimetype="text/html")})
    cfg = make_cfg(app.url, roles=[{"name": "anonymous", "login": {"type": "none"}}],
                   scope={"static_allow_hosts": ["localhost"],
                          "login_allow_hosts": ["localhost"]})
    h = Harness(cfg, run_dir(tmp_path, cfg))
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(app.url + "/")
    await page.wait_for_function("document.title === 'cdn-loaded'", timeout=5000)
    try:  # a crawl page may not navigate to the IdP / CDN host
        await page.goto(ext_base + "/sso")
    except Exception:
        pass
    assert ("GET", "/sso") not in ext.requests
    login_page = await h.recorder.new_page(sess.context)
    h.recorder.login_pages.add(login_page)
    await login_page.goto(ext_base + "/sso")
    assert await login_page.title() == "IdP"
    await h.recorder.drain()
    assert ("GET", "/lib.js") in ext.requests and ("GET", "/sso") in ext.requests
    assert not [r for r in rows(h, "SELECT url FROM exchanges") if "localhost" in r["url"]]
    await sess.context.close()


_DJANGO_REGISTER = """<!doctype html><html><body>
<form method="post" action="/taskManager/register/" enctype="multipart/form-data">
<input type="hidden" name="csrfmiddlewaretoken" value="Zk9QcXJzdFV2d3h5ejAxMjM0NTY3ODk">
<input name="username"><input type="password" name="password">
<input name="ssn"><input type="file" name="avatar">
<button type="submit">Register</button></form></body></html>"""


async def test_multipart_register_redacted_end_to_end(browser, stub_server, make_cfg, run_dir,
                                                      tmp_path, monkeypatch):
    """A Django-style multipart register form: password and csrfmiddlewaretoken are
    redacted in the stored request body and the HAR; ssn (customer data) and the
    uploaded file bytes are kept."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    got = {}

    def register(r):
        if r.method == "POST":
            got["form"] = dict(r.form)
            got["file"] = r.files["avatar"].read()
            return Response(status=302, headers={"Location": "/taskManager/login/"})
        return Response(_DJANGO_REGISTER, mimetype="text/html")
    srv = stub_server({
        "/taskManager/register/": register,
        "/taskManager/login/": lambda r: Response("<p>login</p>", mimetype="text/html"),
    })
    cfg = make_cfg(srv.url, roles=[{"name": "anonymous", "login": {"type": "none"}}])
    rd = run_dir(tmp_path, cfg)
    h = Harness(cfg, rd)
    sess = await h.open(browser, "anonymous")
    page = await h.recorder.new_page(sess.context)
    await page.goto(srv.url + "/taskManager/register/")
    await page.fill("input[name=username]", "alice")
    await page.fill("input[name=password]", "Hunter2-pass-Zq9")
    await page.fill("input[name=ssn]", "123-45-6789")
    await page.set_input_files("input[name=avatar]", files=[{
        "name": "a.txt", "mimeType": "text/plain", "buffer": b"avatar-file-contents"}])
    await page.click("button[type=submit]")
    await page.wait_for_url("**/taskManager/login/")
    await h.recorder.drain()
    await sess.context.close()
    assert got["form"]["password"] == "Hunter2-pass-Zq9"  # the app itself got the real value

    ex = next(r for r in rows(h, "SELECT * FROM exchanges WHERE method='POST'"))
    stored = (ex["req_body"] or "").encode() if ex["req_body"] else \
        (rd.root / ex["req_body_path"]).read_bytes()
    for secret in (b"Hunter2-pass-Zq9", b"Zk9QcXJzdFV2d3h5ejAxMjM0NTY3ODk"):
        assert secret not in stored
    assert b"<redacted:password>" in stored and b"<redacted:csrf>" in stored
    assert b"123-45-6789" in stored and b"avatar-file-contents" in stored

    text = h.recorder.finalize_har("anonymous").read_text()
    assert "Hunter2-pass-Zq9" not in text
    assert "Zk9QcXJzdFV2d3h5ejAxMjM0NTY3ODk" not in text
    assert "123-45-6789" in text
    har = json.loads(text)
    post = next(e["request"]["postData"] for e in har["log"]["entries"]
                if e["request"]["method"] == "POST")
    assert "<redacted:password>" in post.get("text", "") or any(
        p.get("value") == "<redacted:password>" for p in post.get("params") or [])
    blob = rd.events_path.read_text() + json.dumps(rows(h, "SELECT * FROM params"))
    assert "Hunter2-pass-Zq9" not in blob and "Zk9QcXJzdFV2d3h5ejAxMjM0NTY3ODk" not in blob


async def test_finalize_har_multipart_post_data_redacted(make_cfg, run_dir, tmp_path, ssr_url):
    cfg = make_cfg(ssr_url)
    rd = run_dir(tmp_path, cfg)
    h = Harness(cfg, rd)
    body = ("--BND\r\nContent-Disposition: form-data; name=\"csrfmiddlewaretoken\"\r\n\r\n"
            "tokTOKtok12345678\r\n--BND\r\nContent-Disposition: form-data; name=\"password\""
            "\r\n\r\nPw-Secret-777\r\n--BND\r\nContent-Disposition: form-data; name=\"ssn\""
            "\r\n\r\n123-45-6789\r\n--BND--\r\n")
    har = {"log": {"entries": [
        {"request": {"method": "POST", "url": "http://x/register/", "headers": [],
                     "postData": {"mimeType": "multipart/form-data; boundary=BND", "text": body,
                                  "params": [{"name": "csrfmiddlewaretoken",
                                              "value": "tokTOKtok12345678"},
                                             {"name": "password", "value": "Pw-Secret-777"},
                                             {"name": "ssn", "value": "123-45-6789"}]}},
         "response": {"status": 302, "headers": [], "content": {"mimeType": "text/html"}}}]}}
    path = rd.traffic_dir / "anon.har"
    path.write_text(json.dumps(har))
    h.recorder.finalize_har("anon")
    post = json.loads(path.read_text())["log"]["entries"][0]["request"]["postData"]
    assert "tokTOKtok12345678" not in json.dumps(post) and "Pw-Secret-777" not in json.dumps(post)
    assert "<redacted:csrf>" in post["text"] and "<redacted:password>" in post["text"]
    assert "123-45-6789" in post["text"]
    assert post["text"].startswith("--BND\r\n") and post["text"].endswith("--BND--\r\n")
    assert {p["name"]: p["value"] for p in post["params"]} == {
        "csrfmiddlewaretoken": "<redacted:csrf>", "password": "<redacted:password>",
        "ssn": "123-45-6789"}
