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
