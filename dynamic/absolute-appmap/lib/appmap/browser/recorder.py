"""Gated traffic recording for a Playwright browser context."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable
from urllib.parse import urljoin, urlsplit

from playwright.async_api import BrowserContext, Page, Request, Route, WebSocket

from ..events import EventLog
from ..gate import RequestGate
from ..normalize import normalize_path
from ..recording import record_exchange
from ..redact import Redactor
from ..rundir import RunDir
from ..store import Store
from ..throttle import CircuitOpen

# Static subresources of in-scope hosts skip the rate limiter (scope still applies).
STATIC_TYPES = frozenset({"image", "stylesheet", "script", "font", "media"})
# Long-lived streams are never buffered; redirects are not a concern for them.
_PASSTHROUGH_TYPES = frozenset({"eventsource", "websocket"})
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_CSRF = re.compile(r"csrf|xsrf", re.I)
_FRAME_LIMIT = 4096


def _canonical_header(name: str) -> str:
    return "-".join(p.capitalize() for p in name.split("-"))


class TrafficRecorder:
    def __init__(self, store: Store, events: EventLog, redactor: Redactor, rd: RunDir,
                 gate: RequestGate, known_patterns: list[str]):
        self.store, self.events, self.redactor, self.rd = store, events, redactor, rd
        self.gate, self.known_patterns = gate, known_patterns
        # Per-role state shared with RoleSession (same dict objects).
        self.auth_headers: dict[str, dict[str, str]] = {}
        self.origin_by_role: dict[str, str] = {}   # "login" while a login flow runs
        self.last_status: dict[str, int | None] = {}
        self._pending: set[asyncio.Task] = set()

    # -- wiring ------------------------------------------------------------
    async def attach(self, context: BrowserContext, role: str) -> None:
        self.auth_headers.setdefault(role, {})
        await context.route("**/*", lambda route, request: self._route(route, request, role))
        if hasattr(context, "route_web_socket"):
            await context.route_web_socket("**/*", lambda ws: self._gate_websocket(ws, role))
        context.on("request", lambda request: self._learn_auth(request, role))
        context.on("requestfinished",
                   lambda request: self._spawn(self._record(request, role)))
        context.on("page", lambda page: self._watch_page(page, role))
        for page in context.pages:
            self._watch_page(page, role)

    def _spawn(self, coro: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def drain(self) -> None:
        """Wait for in-flight exchange recording to finish."""
        while self._pending:
            await asyncio.gather(*list(self._pending), return_exceptions=True)

    # -- request gate ------------------------------------------------------
    @staticmethod
    def _found_on(request: Request) -> str:
        try:
            ref = request.headers.get("referer")
            if ref:
                return ref
            url = request.frame.url
            return "" if url.startswith("about:") else url
        except Exception:
            return ""

    async def _route(self, route: Route, request: Request, role: str) -> None:
        url = request.url
        if request.resource_type in STATIC_TYPES and self.gate.scope.allows(url):
            await self._fetch_checking_redirect(route, request, role)
            return
        try:
            admitted = await self.gate.admit(request.method, url, role=role,
                                             found_on=self._found_on(request))
        except CircuitOpen as exc:
            self.events.emit("error", role=role, message=str(exc), url=url)
            admitted = False
        if not admitted:
            await route.abort()
        elif request.resource_type in _PASSTHROUGH_TYPES:
            await route.continue_()
        else:
            await self._fetch_checking_redirect(route, request, role)

    async def _fetch_checking_redirect(self, route: Route, request: Request,
                                       role: str) -> None:
        """Chromium follows redirects without re-running route handlers, so fetch with
        redirects disabled and gate the ``Location`` before the browser can follow it."""
        try:
            response = await route.fetch(max_redirects=0, timeout=0)
        except Exception:
            await route.abort()
            return
        location = response.headers.get("location")
        if response.status in _REDIRECTS and location:
            target = urljoin(request.url, location)
            try:
                allowed = await self.gate.admit(request.method, target, role=role,
                                                found_on=request.url)
            except CircuitOpen as exc:
                self.events.emit("error", role=role, message=str(exc), url=target)
                allowed = False
            if not allowed:
                await route.abort()
                return
        await route.fulfill(response=response)

    async def _gate_websocket(self, ws, role: str) -> None:
        try:
            # ScopeGuard speaks http(s); a websocket is an upgraded GET to the same origin.
            http_url = re.sub(r"^ws", "http", ws.url, flags=re.I)
            ok = await self.gate.admit("GET", http_url, role=role, found_on=None)
        except CircuitOpen as exc:
            self.events.emit("error", role=role, message=str(exc), url=ws.url)
            ok = False
        if ok:
            ws.connect_to_server()
        else:
            await ws.close()

    # -- learning + recording ---------------------------------------------
    def _learn_auth(self, request: Request, role: str) -> None:
        learned = self.auth_headers.setdefault(role, {})
        for name, value in request.headers.items():
            if name == "authorization" or _CSRF.search(name):
                learned[_canonical_header(name)] = value

    async def _record(self, request: Request, role: str) -> None:
        try:
            response = await request.response()
            if response is None:
                return
            try:
                body = await response.body()
            except Exception:  # redirects and aborted bodies have none
                body = None
            timing = request.timing
            duration = max(0.0, float(timing.get("responseEnd", 0) or 0))
            status = response.status
            self.gate.observe(status)
            if request.resource_type not in STATIC_TYPES:
                self.last_status[role] = status
            record_exchange(
                self.store, self.events, self.redactor, self.rd, role=role,
                origin=self.origin_by_role.get(role, "ui"), method=request.method,
                url=request.url, req_headers=await request.all_headers(),
                req_body=request.post_data_buffer, status=status,
                resp_headers=await response.all_headers(), resp_body=body,
                duration_ms=duration, caused_by=None, known_patterns=self.known_patterns)
        except Exception as exc:  # recording must never break the crawl
            self.events.emit("error", role=role, message=f"record failed: {exc}",
                             url=request.url)

    # -- websockets --------------------------------------------------------
    def _watch_page(self, page: Page, role: str) -> None:
        page.on("websocket", lambda ws: self._on_websocket(ws, role))

    def _on_websocket(self, ws: WebSocket, role: str) -> None:
        parts = urlsplit(ws.url)
        self.store.upsert_endpoint("GET", parts.netloc.lower(),
                                   normalize_path(parts.path or "/", self.known_patterns),
                                   "websocket")
        url = self.redactor.url(ws.url)
        self.events.emit("websocket", role=role, url=url, direction="open")

        def frame(direction: str):
            def handler(payload) -> None:
                if isinstance(payload, (bytes, bytearray)):
                    payload = bytes(payload).decode("utf-8", errors="replace")
                self.events.emit("websocket", role=role, url=url, direction=direction,
                                 payload=str(payload)[:_FRAME_LIMIT])
            return handler

        ws.on("framesent", frame("sent"))
        ws.on("framereceived", frame("received"))
        ws.on("close", lambda _ws: self.events.emit(
            "websocket", role=role, url=url, direction="close"))

    # -- HAR ---------------------------------------------------------------
    def finalize_har(self, role: str) -> Path | None:
        """Rewrite ``traffic/<role>.har`` through the redactor (close the context first)."""
        path = Path(self.rd.traffic_dir) / f"{role}.har"
        if not path.exists():
            return None
        har = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(self.redactor.json(har), indent=1), encoding="utf-8")
        return path
