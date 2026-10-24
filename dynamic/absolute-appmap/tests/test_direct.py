import pytest
from playwright.async_api import async_playwright
from werkzeug.wrappers import Response

from appmap.config import Scope
from appmap.direct import DirectClient
from appmap.gate import RequestGate
from appmap.scope import ScopeGuard
from appmap.store import Store
from appmap.throttle import CircuitBreaker, RateLimiter


@pytest.fixture
async def api():
    async with async_playwright() as p:
        ctx = await p.request.new_context()
        yield ctx
        await ctx.dispose()


def make(tmp_path):
    store = Store(tmp_path / "t.sqlite")
    store.init_schema()
    gate = RequestGate(ScopeGuard(Scope(allow_hosts=["127.0.0.1"])),
                       RateLimiter(1000, 0), CircuitBreaker(10, 0.9), store)
    calls = []

    def rec(**kw):
        calls.append(kw)
        return len(calls)
    return store, gate, DirectClient(gate, rec), calls


async def test_redirect_to_external_host_not_followed(api, tmp_path, stub_server):
    srv = stub_server({"/go": lambda r: Response(status=302, headers={"Location": "https://evil.example/x"})})
    store, gate, client, calls = make(tmp_path)
    status, resp = await client.request(api, "GET", srv.url + "/go", role="admin", origin="replay")
    assert status == 302
    assert [r[1] for r in srv.requests] == ["/go"]
    links = [r["url"] for r in store.conn.execute("SELECT url FROM external_links")]
    assert links == ["https://evil.example/x"]
    assert len(calls) == 1


async def test_in_scope_redirect_followed_through_gate(api, tmp_path, stub_server):
    srv = stub_server({
        "/a": lambda r: Response(status=302, headers={"Location": "/b"}),
        "/b": lambda r: Response("done", headers={"Content-Type": "text/plain"})})
    _, _, client, calls = make(tmp_path)
    status, resp = await client.request(api, "GET", srv.url + "/a", role="r", origin="ui")
    assert status == 200 and await resp.text() == "done"
    assert [c["status"] for c in calls] == [302, 200]
    assert calls[0]["caused_by"] is None


async def test_out_of_scope_returns_none(api, tmp_path, stub_server):
    _, _, client, calls = make(tmp_path)
    assert await client.request(api, "GET", "http://evil.example/", role="r", origin="ui") is None
    assert calls == []


async def test_redirect_loop_capped(api, tmp_path, stub_server):
    srv = stub_server({"/loop": lambda r: Response(status=302, headers={"Location": "/loop"})})
    _, _, client, calls = make(tmp_path)
    status, _ = await client.request(api, "GET", srv.url + "/loop", role="r", origin="ui")
    assert status == 302 and len(srv.requests) == 11


async def test_post_json_params_and_extra_auth(api, tmp_path, stub_server):
    seen = {}

    def h(r):
        seen.update(auth=r.headers.get("X-Auth"), body=r.get_json(), q=r.args.get("a"))
        return Response("ok")
    srv = stub_server({"/p": h})
    _, _, client, calls = make(tmp_path)
    status, _ = await client.request(api, "POST", srv.url + "/p", role="r", origin="replay",
                                     json={"k": 1}, params={"a": "b"}, extra_auth={"X-Auth": "t"})
    assert status == 200 and seen == {"auth": "t", "body": {"k": 1}, "q": "b"}
    assert calls[0]["req_body"] == b'{"k": 1}' and calls[0]["method"] == "POST"


async def test_credentials_dropped_cross_netloc_and_content_type_on_get(api, tmp_path, stub_server):
    other = stub_server({"/t": lambda r: Response("ok")})
    seen = {}

    def start(r):
        return Response(status=303, headers={"Location": f"{other.url}/t"})
    srv = stub_server({"/s": start})
    store = Store(tmp_path / "t.sqlite")
    store.init_schema()
    # both stub servers are on 127.0.0.1 but different ports, so netlocs differ
    gate = RequestGate(ScopeGuard(Scope(allow_hosts=["127.0.0.1"])),
                       RateLimiter(1000, 0), CircuitBreaker(10, 0.9), store)
    calls = []
    client = DirectClient(gate, lambda **kw: calls.append(kw) or len(calls))
    await client.request(api, "POST", srv.url + "/s", role="r", origin="replay", json={"a": 1},
                         headers={"Authorization": "Bearer x", "Cookie": "a=b",
                                  "Proxy-Authorization": "p", "X-Keep": "1"},
                         extra_auth={"X-Auth": "t"})
    first, second = calls
    assert first["req_headers"]["Authorization"] == "Bearer x"
    h = {k.lower() for k in second["req_headers"]}
    assert h == {"x-keep"} and second["method"] == "GET" and second["req_body"] is None


async def test_network_error_observed_and_returns_none(api, tmp_path):
    from appmap.events import EventLog
    from appmap.redact import Redactor
    store, gate, _, _ = make(tmp_path)
    seen = []
    gate.observe = seen.append
    ev = EventLog(tmp_path / "e.jsonl", Redactor([], []))
    client = DirectClient(gate, lambda **kw: 1, ev)
    assert await client.request(api, "GET", "http://127.0.0.1:1/", role="r", origin="ui") is None
    assert seen == [599]
    assert [e.type for e in ev.iter()] == ["error"]
