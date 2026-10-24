"""Gated direct HTTP client over a Playwright APIRequestContext."""
from __future__ import annotations

import json as _json
import time
from typing import Any, Callable
from urllib.parse import urlencode, urljoin, urlsplit

from playwright.async_api import APIRequestContext, APIResponse

from .events import EventLog
from .gate import RequestGate

MAX_HOPS = 10
_REDIRECTS = {301, 302, 303, 307, 308}
_CREDENTIAL_HEADERS = {"authorization", "cookie", "proxy-authorization"}
NETWORK_ERROR_STATUS = 599  # what the circuit breaker sees for a failed fetch


def _with_params(url: str, params: dict | None) -> str:
    if not params:
        return url
    return url + ("&" if "?" in url else "?") + urlencode(params, doseq=True)


def _body_bytes(data: Any, json: Any) -> bytes | None:
    if json is not None:
        return _json.dumps(json).encode()
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, str):
        return data.encode()
    return _json.dumps(data).encode()


class DirectClient:
    """Every hop (initial request and each redirect) passes through the gate.

    ``rec`` records one exchange; it is called with the keyword arguments of
    ``record_exchange`` except store/events/redactor/rd/known_patterns (bind those
    with ``functools.partial``).
    """

    def __init__(self, gate: RequestGate, rec: Callable[..., int],
                 events: EventLog | None = None):
        self.gate, self.rec, self.events = gate, rec, events

    async def request(self, api: APIRequestContext, method: str, url: str, *, role: str,
                      origin: str, caused_by: str | None = None,
                      headers: dict | None = None, data: Any = None, json: Any = None,
                      params: dict | None = None,
                      extra_auth: dict | None = None) -> tuple[int, APIResponse] | None:
        """Return ``(status, response)`` of the last response received, or None when the
        gate refuses the initial URL. A redirect to an out-of-scope URL is not followed:
        the 3xx response is returned and the target lands in ``external_links``.
        """
        url = _with_params(url, params)
        origin_netloc = urlsplit(url).netloc
        body = _body_bytes(data, json)
        found_on = None
        result: tuple[int, APIResponse] | None = None
        for _ in range(MAX_HOPS + 1):
            if not await self.gate.admit(method, url, role=role, found_on=found_on):
                return result
            hdrs = dict(headers or {})
            same_origin = urlsplit(url).netloc == origin_netloc
            if not same_origin:
                hdrs = {k: v for k, v in hdrs.items() if k.lower() not in _CREDENTIAL_HEADERS}
            if body is None:
                hdrs = {k: v for k, v in hdrs.items() if k.lower() != "content-type"}
            if extra_auth and same_origin:
                hdrs.update(extra_auth)
            if json is not None and body is not None:
                hdrs.setdefault("Content-Type", "application/json")
            started = time.monotonic()
            try:
                resp = await api.fetch(url, method=method, headers=hdrs or None, data=body,
                                       max_redirects=0)
            except Exception as exc:  # timeout, refused, DNS, ...
                self.gate.observe(NETWORK_ERROR_STATUS)
                if self.events is not None:
                    self.events.emit("error", role=role, caused_by=caused_by, method=method,
                                     url=url, error=f"{type(exc).__name__}: {exc}")
                return None
            duration_ms = (time.monotonic() - started) * 1000
            resp_body = await resp.body()
            self.gate.observe(resp.status)
            self.rec(role=role, origin=origin, method=method, url=url, req_headers=hdrs,
                     req_body=body, status=resp.status, resp_headers=dict(resp.headers),
                     resp_body=resp_body, duration_ms=duration_ms, caused_by=caused_by)
            result = (resp.status, resp)
            location = resp.headers.get("location")
            if resp.status not in _REDIRECTS or not location:
                return result
            found_on = url
            url = urljoin(url, location)
            if resp.status in (301, 302, 303) and method.upper() not in ("GET", "HEAD"):
                method, body = "GET", None
                json = None
        return result
