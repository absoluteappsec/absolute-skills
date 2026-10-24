import pytest

from appmap.config import Scope
from appmap.gate import RequestGate
from appmap.scope import ScopeGuard
from appmap.throttle import CircuitBreaker, CircuitOpen, RateLimiter


class FakeStore:
    def __init__(self):
        self.links = []

    def upsert_external_link(self, url, found_on="", role=None):
        self.links.append((url, found_on, role))
        return 1


def make(window=2):
    st = FakeStore()
    g = RequestGate(ScopeGuard(Scope(allow_hosts=["a.test"])),
                    RateLimiter(1000, 0), CircuitBreaker(window, 0.5), st)
    return g, st


async def test_admit_in_and_out_of_scope():
    g, st = make()
    assert await g.admit("GET", "http://a.test/x", role="r", found_on=None)
    assert not await g.admit("GET", "http://b.test/x", role="r", found_on=None)
    assert st.links == [("http://b.test/x", "", "r")]


async def test_admit_raises_when_tripped():
    g, _ = make()
    g.observe(503)
    g.observe(500)
    with pytest.raises(CircuitOpen):
        await g.admit("GET", "http://a.test/x", role="r", found_on=None)


async def test_out_of_scope_never_touches_limiter_even_when_tripped():
    g, st = make()
    g.observe(503)
    g.observe(500)

    async def boom():
        raise AssertionError("limiter called")
    g.limiter.acquire = boom
    assert await g.admit("GET", "http://b.test/x", role="r", found_on="p") is False
    assert st.links == [("http://b.test/x", "p", "r")]
