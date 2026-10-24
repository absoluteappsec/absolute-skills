"""Request pacing and the error-rate circuit breaker."""
from __future__ import annotations

import asyncio
import random
import time
from collections import deque


class CircuitOpen(RuntimeError):
    """Raised when too many recent responses were 429/5xx."""


class RateLimiter:
    def __init__(self, rps: float, jitter: float = 0.0):
        self._interval = 1.0 / rps
        self._jitter = jitter
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = max(0.0, self._next - now)
            if self._jitter:
                wait += random.uniform(0, self._jitter)
            if wait:
                await asyncio.sleep(wait)
            self._next = time.monotonic() + self._interval


class CircuitBreaker:
    def __init__(self, window: int, threshold: float):
        self._events: deque[bool] = deque(maxlen=window)
        self._window = window
        self._threshold = threshold

    def record(self, status: int) -> None:
        self._events.append(status == 429 or status >= 500)

    @property
    def tripped(self) -> bool:
        if len(self._events) < self._window:
            return False
        return sum(self._events) / self._window >= self._threshold
