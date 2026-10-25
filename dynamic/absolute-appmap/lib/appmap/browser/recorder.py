"""Gated traffic recording for a Playwright browser context."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Request, Route, WebSocket

from ..events import EventLog
from ..gate import RequestGate
from ..normalize import normalize_path
from ..recording import record_exchange, redact_bytes, redact_text
from ..redact import Redactor
from ..rundir import RunDir
from ..store import Store
from ..throttle import CircuitOpen

# Static subresources of in-scope hosts skip the rate limiter (scope still applies).
STATIC_TYPES = frozenset({"image", "stylesheet", "script", "font", "media"})
_CDP_STATIC_TYPES = frozenset({"Image", "Stylesheet", "Script", "Font", "Media"})
_CSRF = re.compile(r"csrf|xsrf", re.I)
_FRAME_LIMIT = 4096


def har_part_path(rd: RunDir, role: str) -> Path:
    """Where a role's browser context records the current session's (clear) HAR."""
    return Path(rd.traffic_dir) / f"{role}.part.har"


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
        self.login_pages: set[Page] = set()        # pages a login flow is running on
        self._pending: set[asyncio.Task] = set()
        self._gate_tasks: dict[Page, asyncio.Task] = {}
        self._orphans: dict[str, list[tuple[float, str]]] = {}  # per role/context
        self._admit_tokens: dict[tuple[str, str], list[float]] = {}
        self._roles: dict[BrowserContext, str] = {}
        self._safe: set[Page] = set()        # armed before any request could start
        self._quarantine: set[Page] = set()  # unarmed pages whose requests are refused

    # -- wiring ------------------------------------------------------------
    async def attach(self, context: BrowserContext, role: str) -> None:
        self.auth_headers.setdefault(role, {})
        self._roles[context] = role
        await context.route("**/*", lambda route, request: self._route(route, request, role))
        await context.route_web_socket("**/*", lambda ws: self._gate_websocket(ws, role))
        context.on("request", lambda request: self._learn_auth(request, role))
        context.on("requestfinished",
                   lambda request: self._spawn(self._record(request, role)))
        context.on("page", lambda page: self._watch_page(page, role))
        for page in context.pages:
            self._watch_page(page, role)

    async def _cdp_allows(self, ev: dict, role: str) -> bool:
        """Every paused request gets scope + deny checks. Chromium restarts cross-origin
        redirected fetch/XHR as a fresh request (no redirectedRequestId, no route call), so
        the limiter/breaker apply to anything the route handler did not already admit."""
        req = ev["request"]
        url, method = req["url"], req["method"]
        if urlsplit(url).scheme not in ("http", "https"):
            return True  # data:, blob:, about: never reach the network
        found_on = req.get("headers", {}).get("Referer", "")
        if not self.gate.scope.allows(url):
            await self.gate.admit(method, url, role=role, found_on=found_on)  # records link
            return False
        redirected = bool(ev.get("redirectedRequestId"))
        if ev.get("resourceType") in _CDP_STATIC_TYPES:
            return True
        if not redirected and not self._claim(method, url):
            return True
        return await self.gate.admit(method, url, role=role, found_on=found_on)

    def _claim(self, method: str, url: str) -> bool:
        """True if the caller must admit this request; False if the other gate (route
        handler or CDP, whichever runs first) already did. Never awaits, so it is atomic."""
        key, now = (method, url), time.monotonic()
        tokens = [t for t in self._admit_tokens.get(key, []) if now - t < 5.0]
        if tokens:
            tokens.pop(0)
            self._admit_tokens[key] = tokens
            return False
        self._admit_tokens[key] = tokens + [now]
        return True

    # -- redirect hops (CDP) -------------------------------------------------
    # Chromium follows redirects internally without re-running Playwright route handlers,
    # so hops 2+ would bypass scope/deny/limiter/breaker. A CDP Fetch session per page sees
    # every redirected request; first hops are gated by the route handler above.
    async def _enable_redirect_gate(self, page: Page, role: str) -> None:
        session = await page.context.new_cdp_session(page)

        async def on_paused(ev: dict) -> None:
            rid = ev["requestId"]
            req = ev["request"]
            url = req["url"]
            try:
                ok = await self._cdp_allows(ev, role)
            except CircuitOpen as exc:
                self.events.emit("error", role=role, message=str(exc), url=url)
                ok = False
            except Exception as exc:  # fail closed
                self.events.emit("error", role=role, message=f"request gate failed: {exc}")
                ok = False
            try:
                if ok:
                    await session.send("Fetch.continueRequest", {"requestId": rid})
                else:
                    await session.send("Fetch.failRequest",
                                       {"requestId": rid, "errorReason": "BlockedByClient"})
            except Exception:
                pass  # page closed while paused

        session.on("Fetch.requestPaused", lambda ev: self._spawn(on_paused(ev)))
        await session.send("Fetch.enable", {"patterns": [{"urlPattern": "*",
                                                          "requestStage": "Request"}]})

    async def new_page(self, context: BrowserContext) -> Page:
        """Open a page whose redirect gate is armed before it can navigate.

        Use this instead of ``context.new_page()``; pages opened by the site itself
        (popups) are armed asynchronously and may miss their very first request.
        """
        page = await context.new_page()
        task = self._watch_page(page, self._roles[context])  # usually made by the "page" event
        await task
        self._safe.add(page)
        return page

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
        # Fetch interception only applies to requests that start after Fetch.enable, so a
        # request already in flight on a page that was not armed up front (popup, or a page
        # from plain context.new_page()) could follow a redirect chain out of scope. Fail
        # closed: quarantine the page, abort, arm it, then replay main-frame GET navigations
        # (the replay starts after arming, so its hops are gated).
        page = self._page_of(request)
        if page is None:
            # No page to arm yet (new window's first request): refuse it, and replay it on
            # the popup once its page appears and is armed (see _adopt_orphan).
            if request.is_navigation_request() and request.method == "GET":
                self._orphans.setdefault(role, []).append((time.monotonic(), url))
            await route.abort()
            return
        if page not in self._safe:
            task = self._watch_page(page, role)
            self._quarantine.add(page)
            await route.abort()
            if request.is_navigation_request() and request.method == "GET" \
                    and request.frame == page.main_frame:
                self._spawn(self._replay_navigation(page, task, url, role))
            return
        if request.resource_type in STATIC_TYPES and self.gate.scope.allows(url):
            await route.continue_()
            return
        try:
            admitted = (not self._claim(request.method, url)) or await self.gate.admit(
                request.method, url, role=role, found_on=self._found_on(request))
        except CircuitOpen as exc:
            self.events.emit("error", role=role, message=str(exc), url=url)
            admitted = False
        if admitted:
            await route.continue_()
        else:
            await route.abort()

    @staticmethod
    def _page_of(request: Request) -> Page | None:
        try:
            return request.frame.page
        except Exception:  # popup's first request: issued before its frame exists
            return None

    async def _adopt_orphan(self, page: Page, armed: asyncio.Task, role: str) -> None:
        try:
            if await page.opener() is None:
                return
        except Exception:
            return
        while self._orphans.get(role):
            ts, url = self._orphans[role].pop(0)
            if time.monotonic() - ts < 5.0:
                self._quarantine.add(page)
                await self._replay_navigation(page, armed, url, role)
                return

    async def _replay_navigation(self, page: Page, armed: asyncio.Task, url: str,
                                 role: str) -> None:
        try:
            await armed
            self._quarantine.discard(page)
            self._safe.add(page)
            await page.goto(url)
        except Exception as exc:
            self.events.emit("error", role=role, url=url,
                             message=f"navigation replay after arming failed: {exc}")

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
        if not self.gate.scope.allows(request.url):
            return
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
    def _watch_page(self, page: Page, role: str) -> asyncio.Task:
        task = self._gate_tasks.get(page)
        if task is not None:
            return task
        task = asyncio.ensure_future(self._arm(page, role))
        self._gate_tasks[page] = task
        self._pending.add(task)

        def done(t: asyncio.Task) -> None:
            self._pending.discard(t)
            if not t.cancelled():
                t.exception()  # retrieved: failure is reported via the error event

        task.add_done_callback(done)
        def closed(_p: Page) -> None:
            self._gate_tasks.pop(page, None)
            self._safe.discard(page)
            self._quarantine.discard(page)

        page.on("close", closed)
        if self._orphans.get(role):
            self._spawn(self._adopt_orphan(page, task, role))
        page.on("websocket", lambda ws: self._on_websocket(ws, role))
        return task

    async def _arm(self, page: Page, role: str) -> None:
        try:
            await self._enable_redirect_gate(page, role)
        except Exception as exc:
            self.events.emit("error", role=role,
                             message=f"redirect gate could not be armed: {exc}")
            raise

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
        """Redact this session's HAR into ``traffic/<role>.har`` (close the context first).

        Contexts record to ``traffic/<role>.part.har``; its redacted entries and pages are
        appended to the existing ``<role>.har`` so every invocation's traffic is kept.
        Without a part file, ``<role>.har`` itself is redacted in place.
        """
        path = Path(self.rd.traffic_dir) / f"{role}.har"
        part = har_part_path(self.rd, role)
        if part.exists():
            try:
                har = self._redacted_har(part)
            except ValueError as exc:  # truncated by a crash: unusable and unredactable
                self.events.emit("error", role=role, message=f"discarding unreadable HAR: {exc}")
                part.unlink()
                return path if path.exists() else None
            if path.exists():
                base = json.loads(path.read_text(encoding="utf-8"))
                log, new = base.setdefault("log", {}), har.get("log", {})
                log.setdefault("pages", []).extend(new.get("pages") or [])
                log.setdefault("entries", []).extend(new.get("entries") or [])
                har = base
            self._write_har(path, har)
            part.unlink()
            return path
        if not path.exists():
            return None
        self._write_har(path, self._redacted_har(path))
        return path

    def _redacted_har(self, path: Path) -> dict:
        har = json.loads(path.read_text(encoding="utf-8"))
        for entry in har.get("log", {}).get("entries", []):
            self._redact_bodies(entry)
        return self.redactor.json(har)

    @staticmethod
    def _write_har(path: Path, har: dict) -> None:
        tmp = path.with_suffix(".har.tmp")
        tmp.write_text(json.dumps(har, indent=1), encoding="utf-8")
        os.replace(tmp, path)

    def _redact_body(self, text: str, mime: str, encoding: str | None) -> str:
        """Redact a HAR body the way recording.py redacts stored bodies."""
        is_b64 = encoding == "base64"
        if is_b64:
            try:
                raw = base64.b64decode(text, validate=True)
            except ValueError:
                return "<redacted:undecodable>"
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return base64.b64encode(redact_bytes(self.redactor, raw)).decode()
        if "x-www-form-urlencoded" in (mime or ""):
            text = self.redactor.url("x?" + text)[2:]
        else:
            text = redact_text(self.redactor, text)
        return base64.b64encode(text.encode()).decode() if is_b64 else text

    def _redact_bodies(self, entry: dict) -> None:
        content = entry.get("response", {}).get("content") or {}
        if isinstance(content.get("text"), str):
            content["text"] = self._redact_body(content["text"], content.get("mimeType", ""),
                                                content.get("encoding"))
        post = entry.get("request", {}).get("postData") or {}
        if isinstance(post.get("text"), str):
            post["text"] = self._redact_body(post["text"], post.get("mimeType", ""), None)
