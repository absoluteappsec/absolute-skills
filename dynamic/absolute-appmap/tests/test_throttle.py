import time

import pytest

from appmap.throttle import CircuitBreaker, CircuitOpen, RateLimiter


async def test_rate_limiter_spacing():
    rl = RateLimiter(rps=10, jitter=0)
    t = time.monotonic()
    for _ in range(5):
        await rl.acquire()
    assert time.monotonic() - t >= 0.4


def test_breaker_trips_at_threshold():
    b = CircuitBreaker(window=10, threshold=0.2)
    for s in (503, 503):
        b.record(s)
    assert not b.tripped  # window not yet full
    for _ in range(8):
        b.record(200)
    assert b.tripped


def test_breaker_not_tripped_below_threshold():
    b = CircuitBreaker(window=10, threshold=0.2)
    b.record(429)
    for _ in range(9):
        b.record(200)
    assert not b.tripped


def test_circuit_open_is_runtime_error():
    assert issubclass(CircuitOpen, RuntimeError)
    with pytest.raises(CircuitOpen):
        raise CircuitOpen("x")
