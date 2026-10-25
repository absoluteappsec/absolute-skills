"""Single admission point for every outbound request."""
from __future__ import annotations

from .scope import ScopeGuard
from .store import Store
from .throttle import CircuitBreaker, CircuitOpen, RateLimiter


class RequestGate:
    def __init__(self, scope: ScopeGuard, limiter: RateLimiter,
                 breaker: CircuitBreaker, store: Store):
        self.scope, self.limiter, self.breaker, self.store = scope, limiter, breaker, store

    async def admit(self, method: str, url: str, *, role: str,
                    found_on: str | None, login: bool = False, static: bool = False) -> bool:
        """``login``/``static``: see ``ScopeGuard.allows``."""
        if not self.scope.allows(url, login=login, static=static):
            self.store.upsert_external_link(url, found_on or "", role)
            return False
        if self.breaker.tripped:
            raise CircuitOpen(f"circuit breaker tripped; refusing {method} {url}")
        await self.limiter.acquire()
        return True

    def observe(self, status: int) -> None:
        self.breaker.record(status)
