"""State-graph crawler: per-role, frontier-driven browser exploration.

Each frontier item is restored from a known entry point (``goto`` + replayed clicks),
fingerprinted into a state, and expanded into child items: in-scope anchors become ``url``
items, other clickables and forms become ``action`` items. Every action is classified
before it is performed; held actions go to the approval queue instead. All progress lives
in SQLite, so a later ``crawl`` resumes where a budget-limited one stopped.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from urllib.parse import urljoin, urlsplit, urlunsplit

import yaml
from playwright.async_api import Browser, Dialog, Page, async_playwright

from .approvals import ApprovalQueue, PendingAction
from .browser.clickables import Clickable, enumerate_clickables, expand_reveals
from .browser.fingerprint import SKELETON_JS, dom_fingerprint, state_id
from .browser.forms import FormInfo, discover_forms, fill_and_submit
from .browser.locators import resolve
from .browser.recorder import TrafficRecorder
from .browser.session import (MAX_RELOGINS, RoleSession, auth_probe_urls, is_session_lost,
                              login, open_role_session, state_path)
from .browser.settle import install, wait_settled
from .config import Role, RunConfig
from .datagen import DataGen
from .destructive import ActionDescriptor, Flag, classify
from .events import EventLog
from .frontier import Frontier, FrontierItem
from .gate import RequestGate
from .normalize import normalize_path
from .redact import Redactor
from .rundir import RunDir
from .scope import ScopeGuard
from .spa.detect import detect_spa
from .spa.routes import (DRAIN_NAV_JS, HISTORY_PATCH_JS, concrete_url, extract_routes,
                         observed_ids)
from .store import Store
from .throttle import CircuitBreaker, CircuitOpen, RateLimiter

log = logging.getLogger("appmap.crawl")

CrawlStatus = Literal["done", "in_progress", "paused_circuit", "limit_reached", "auth_failed"]
# url items first (cheap, map pages), then clicks, then form submissions
PRIORITY = {"url": 2, "click": 1, "submit": 0}
NAV_TIMEOUT_MS = 15000
ACTION_TIMEOUT_MS = 5000
_NTH = re.compile(r":nth-of-type\(\d+\)")
_SKIP_CLICK_TAGS = {"select", "textarea", "option", "label"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_MAX_VALUES_TRIED = 20  # per field, most recent kept

# Every <form>: xpath (same algorithm as LOCATOR_JS), effective method, action, confirm hint.
_FORMS_JS = """() => [...document.forms].map(f => {
  const parts = [];
  for (let n = f; n && n.nodeType === 1; n = n.parentElement) {
    const sibs = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [n];
    parts.unshift(n.tagName.toLowerCase() + '[' + (sibs.indexOf(n) + 1) + ']');
  }
  const o = f.querySelector('input[type=hidden][name=_method]');
  let action;
  try { action = new URL(f.getAttribute('action') ?? location.href, document.baseURI).href; }
  catch (e) { action = location.href; }
  return {xpath: '/' + parts.join('/'),
          method: ((o && o.value) || f.getAttribute('method') || 'GET').toUpperCase(), action,
          confirm: /confirm\\s*\\(/.test(f.getAttribute('onsubmit') || '')
            || f.hasAttribute('data-confirm') || f.hasAttribute('data-turbo-confirm')};
})"""

# Serialized DOM with password input values removed (attribute values only exist in markup).
_DOM_JS = """() => {
  const clone = document.documentElement.cloneNode(true);
  for (const el of clone.querySelectorAll('input[type=password i]')) el.removeAttribute('value');
  return '<!doctype html>\\n' + clone.outerHTML;
}"""


@dataclass
class CrawlStats:
    states: int
    transitions: int
    forms: int
    exchanges: int
    queued_approvals: int
    status: CrawlStatus
    warnings: list[str] = field(default_factory=list)
    # --approved-only: approved items executed / failed / skipped in this invocation
    approvals: dict[str, int] = field(default_factory=dict)
    # roles whose login or re-login failed: stopped, their pending items kept
    auth_failed: list[str] = field(default_factory=list)


@dataclass
class _State:
    id: str
    url: str
    pattern: str
    title: str
    new: bool = False


class _Lost(Exception):
    """The role's session was lost while processing an item."""


class _Fail(Exception):
    """A step of an item could not be performed (unresolvable locator, navigation error)."""


class _NotRunnable(Exception):
    """An approved item was executed, started or revoked elsewhere since it was listed."""


class _AuthFailed(Exception):
    """The role could not be re-authenticated: stop it, leave the item pending."""


def canon_url(url: str) -> str:
    """Frontier form of a URL: no fragment, non-empty path."""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def _xpath(locators: list[dict]) -> str | None:
    return next((l["value"] for l in locators if l.get("strategy") == "xpath"), None)


def _signature(c: Clickable) -> tuple:
    """Structural group of a clickable: list items/cards share one (position stripped)."""
    css = next((l["value"] for l in c.locators if l.get("strategy") == "css"), "")
    return (c.tag, c.role, _NTH.sub("", css))


class Crawler:
    def __init__(self, cfg: RunConfig, rd: RunDir, store: Store, events: EventLog,
                 gate: RequestGate, queue: ApprovalQueue, recorder: TrafficRecorder,
                 sessions: dict[str, RoleSession], gen: DataGen):
        self.cfg, self.rd, self.store, self.events = cfg, rd, store, events
        self.gate, self.queue, self.recorder, self.sessions, self.gen = (
            gate, queue, recorder, sessions, gen)
        self.redactor: Redactor = recorder.redactor
        self.frontier = Frontier(store)
        self.limits = cfg.limits
        self.cap = cfg.limits.max_states_per_route_pattern
        self.known = [r["pattern"] for r in store.conn.execute(
            "SELECT pattern FROM routes WHERE kind='spa_route'")]
        self._url_patterns: dict[str, Counter] = {}
        self._installed: set = set()
        self._spa_routes: dict[str, list] = {}   # role -> router routes (once detected)
        self._spa_base: dict[str, str] = {}      # role -> URL the routes were read from
        self._spa_skipped: dict[tuple[str, str], str] = {}  # (role, pattern) -> warning
        self._confirm_seen = False
        # While an approved action's final step runs (and only then): which held requests
        # the guard lets through, and how confirm() dialogs are answered.
        self._approval: dict | None = None
        self._confirm_mode = "dismiss"  # "accept" | "defer" (fill_and_submit accepts)
        # Network guard attribution: the action (or page load) requests are charged to.
        # ``_guard`` is set while an item runs; ``_last_guard[role]`` keeps the most recent
        # one so requests fired later (timers, popups, workers) are still attributed.
        self._guard: dict | None = None
        self._last_guard: dict[str, dict] = {}
        # Re-login bookkeeping (C1): consecutive re-logins per role (reset when an item
        # completes) and roles stopped because authentication failed (role -> reason).
        self._relogins: dict[str, int] = {}
        self.auth_failed: dict[str, str] = {}
        # True while the controls of a record this crawl just created are queued: held
        # actions are tagged created_by_run (usable as an approval rule match key).
        self._creating = False

    # -- driver --------------------------------------------------------------
    async def run(self, roles: list[str], *, budget_minutes: float,
                  approved_only: bool = False) -> CrawlStats:
        if approved_only:
            return await self._run_approved(roles, budget_minutes)
        start = time.monotonic()
        prior = float(self.store.get_meta("crawl_elapsed_s") or 0)
        budget_s = budget_minutes * 60
        limit_s = self.limits.max_runtime_minutes * 60
        status: CrawlStatus = "done"
        log.info("crawl started: roles=%s budget=%.2fmin pending=%d", roles, budget_minutes,
                 self.frontier.pending_count())
        try:
            for role in roles:
                if role in self.auth_failed:
                    continue
                status = await self._crawl_role(role, start, prior, budget_s, limit_s)
                if status == "auth_failed":  # that role stops; the others still run
                    status = "done"
                    continue
                if status != "done":
                    break
        finally:
            self.store.set_meta("crawl_elapsed_s", f"{prior + time.monotonic() - start:.3f}")
        if status == "done" and any(self.frontier.pending_count(r) for r in roles):
            status = "in_progress"
        status = self._auth_status(status)
        stats = self.stats(status)
        self._auth_report(stats)
        stats.warnings += list(self._spa_skipped.values())
        log.info("crawl stopped: status=%s states=%d transitions=%d pending_approvals=%d",
                 status, stats.states, stats.transitions, stats.queued_approvals)
        return stats

    def _auth_status(self, status: CrawlStatus) -> CrawlStatus:
        """A role stopped for authentication outranks done/in_progress: the operator must
        re-run ``appmap auth`` before the crawl can finish."""
        return "auth_failed" if self.auth_failed and status in ("done", "in_progress") \
            else status

    def _auth_report(self, stats: CrawlStats) -> None:
        stats.auth_failed = sorted(self.auth_failed)
        for role in stats.auth_failed:
            manual = self.sessions[role].role.login.type == "manual" if role in self.sessions \
                else False
            stats.warnings.append(
                f"role {role}: {self.auth_failed[role]}; role stopped, its pending items "
                f"are kept. Run 'appmap auth --role {role}"
                f"{' --manual-login' if manual else ''}' (the operator logs in for manual "
                "roles), then crawl again")

    def _stop_role(self, role: str, why: str) -> None:
        if role in self.auth_failed:
            return
        self.auth_failed[role] = why
        self.events.emit("error", role=role, message=f"authentication failed: {why}; "
                                                      "role stopped")
        log.warning("[%s] %s; role stopped", role, why)

    async def _crawl_role(self, role: str, start: float, prior: float, budget_s: float,
                          limit_s: float) -> CrawlStatus:
        sess = self.sessions[role]
        for url in self.cfg.base_urls:
            self.frontier.push(role, "url", url=self._safe_url(url), depth=0,
                               priority=PRIORITY["url"], source="ui")
        if not self.frontier.pending_count(role):
            return "done"
        page: Page | None = None
        try:
            while True:
                elapsed = time.monotonic() - start
                if prior + elapsed >= limit_s:
                    return "limit_reached"
                if elapsed >= budget_s:
                    return "in_progress"
                if self.gate.breaker.tripped:
                    return "paused_circuit"
                item = self.frontier.pop(role)
                if item is None:
                    # Drained: IDs seen since seeding may fill more parameterized routes
                    # (and a router may only have appeared after login).
                    if page is not None and not page.is_closed() \
                            and await self._seed_spa(page, role):
                        continue
                    return "done"
                if page is None or page.is_closed():
                    page = await self._open_page(sess)
                try:
                    outcome = await self._process(sess, page, item)
                except CircuitOpen:
                    return "paused_circuit"
                except _AuthFailed:
                    log.warning("[%s] authentication failed; item %d left pending", role,
                                item.id)
                    return "auth_failed"
                if self.gate.breaker.tripped:
                    log.warning("circuit breaker tripped; item %d left pending", item.id)
                    return "paused_circuit"
                self.frontier.mark(item.id, outcome)
                if outcome == "done":
                    self._relogins[role] = 0  # the session works again
                log.info("[%s] item %d %s %s -> %s", role, item.id, item.kind,
                         item.url or self._describe(item.action_path[-1:]), outcome)
                if role not in self._spa_routes and not page.is_closed():
                    await self._seed_spa(page, role)
        finally:
            if page is not None and not page.is_closed():
                await page.close()
            await self.recorder.drain()

    def stats(self, status: CrawlStatus) -> CrawlStats:
        c = lambda sql: int(self.store.conn.execute(sql).fetchone()[0])
        return CrawlStats(
            states=c("SELECT count(*) FROM states"),
            transitions=c("SELECT count(*) FROM transitions"),
            forms=c("SELECT count(*) FROM forms"),
            exchanges=c("SELECT count(*) FROM exchanges"),
            queued_approvals=c("SELECT count(*) FROM approvals WHERE decision='pending'"),
            status=status)

    async def _open_page(self, sess: RoleSession) -> Page:
        role = sess.role.name
        if sess.context not in self._installed:
            self._installed.add(sess.context)
            # Registered after the recorder's route, so it runs first for every request of
            # the role context: all pages, popups and workers, during and between actions.
            await sess.context.route(
                "**/*", lambda route, request: self._guard_route(route, request, role))
            self.recorder.redirect_guard = self._guard_redirect
            if self.cfg.spa != "off":
                await sess.context.add_init_script(HISTORY_PATCH_JS)
            page = await self.recorder.new_page(sess.context)
            await install(page)
        else:
            page = await self.recorder.new_page(sess.context)
        page.set_default_timeout(ACTION_TIMEOUT_MS)
        page.set_default_navigation_timeout(NAV_TIMEOUT_MS)

        async def on_dialog(dialog: Dialog) -> None:
            await self._on_dialog(dialog, role, page)

        page.on("dialog", on_dialog)
        return page

    async def _guard_route(self, route, request, role: str) -> None:
        """Network-level destructive guard (ruling R17). Every non-GET request of the role
        context is classified as a ``request`` attributed to the current (or most recent)
        action or page load; held ones are aborted and queued for approval."""
        page = self._page_of(request)
        if request.method in _SAFE_METHODS \
                or self._approval_allows(request.method, request.url, page, role):
            await route.fallback()
            return
        try:
            origin = request.frame.url
        except Exception:  # worker / not-yet-attached popup
            origin = request.headers.get("referer") or request.url
        if self._hold_write(request.method, request.url, role, page, origin):
            await route.abort("blockedbyclient")
        else:
            await route.fallback()

    async def _guard_redirect(self, method: str, url: str, role: str, page) -> bool:
        """Recorder hook for a 307/308 hop re-sending a write (I1): the target is classified
        like any other request. An approved action's window does not cover a redirect
        target unless it is the predicted request itself."""
        ap = self._approval
        if ap is not None and ap["role"] == role and ap["match"] is not None \
                and self._approval_allows(method, url, page, role):
            return True
        try:
            origin = page.url if page is not None else url
        except Exception:
            origin = url
        return not self._hold_write(method, url, role, page, origin)

    def _hold_write(self, method: str, url: str, role: str, page, origin: str) -> bool:
        """Classify a non-GET request charged to the current (or most recent) action or
        page load. Held: recorded as a guard hit, queued for approval, returns True (the
        caller aborts it)."""
        g = self._guard or self._last_guard.get(role)
        if g is None:  # nothing processed yet: charge it to the page that sent it
            g = {"kind": "load", "label": "", "context": "", "state": None, "item": None,
                 "path": [{"type": "goto", "url": self._safe_url(origin)}], "hits": []}
        try:
            flag = classify(ActionDescriptor("request", method, url, g["label"],
                                             g["context"], False, role, "ui"), self.cfg.profile)
        except Exception:  # fail closed
            flag = Flag(True, "delete", ["request could not be classified"])
        if not flag.held or (flag.category == "data_create"
                             and page is not None and page in self.recorder.login_pages):
            # A login flow's own submit (data_create only in production) must go through;
            # destructive requests from the login page, and anything from other pages or
            # workers while it runs, stay guarded.
            return False
        safe = self.redactor.url(url)
        g["hits"].append((flag, method, safe))
        self.events.emit("error", role=role, url=safe,
                         message=f"held {method} request aborted ({flag.category}); "
                                 "queued for approval")
        label = g["label"] if g["kind"] == "action" else f"{method} {safe} (page load)"
        self._enqueue(role, g["state"], label, flag, g["path"], {"method": method, "url": safe})
        if g is not self._guard and g["item"] is not None:
            self.frontier.mark(g["item"], "queued_approval")  # fired after its item finished
        return True

    @staticmethod
    def _page_of(request):
        try:
            return request.frame.page
        except Exception:  # worker requests have no frame
            return None

    def _arm_guard(self, role: str, kind: str, path: list[dict], *, label: str = "",
                   context: str = "", state: str | None = None,
                   item: int | None = None) -> list:
        """Attribute subsequent non-GET requests to ``path`` (an action or a page load);
        returns the list the guard appends aborted (flag, method, url) hits to."""
        self._guard = {"kind": kind, "label": label, "context": context, "state": state,
                       "item": item, "path": path, "hits": []}
        self._last_guard[role] = self._guard
        return self._guard["hits"]

    async def _on_dialog(self, dialog: Dialog, role: str, page: Page) -> None:
        self.events.emit("dialog", role=role, dialog_type=dialog.type,
                         message=(dialog.message or "")[:500], url=self.redactor.url(page.url))
        try:
            if dialog.type == "confirm" and self._confirm_mode == "defer":
                pass  # an approved confirm-protected form: fill_and_submit accepts it
            elif dialog.type == "confirm" and self._confirm_mode != "accept":
                self._confirm_seen = True
                await dialog.dismiss()
            else:  # alert, prompt, beforeunload: record and accept
                await dialog.accept()
        except Exception:
            pass  # already handled / page closed

    # -- approved actions (--approved-only) ----------------------------------------
    async def _run_approved(self, roles: list[str], budget_minutes: float) -> CrawlStats:
        """Execute each approved, not yet executed item once. Only the approved action's
        own request(s) pass the destructive guard; everything else stays guarded."""
        start = time.monotonic()
        budget_s = budget_minutes * 60
        res = {"executed": 0, "failed": 0, "skipped": 0}
        warnings: list[str] = []
        status: CrawlStatus = "done"
        for aid, why in self._recover_interrupted():
            res["failed"] += 1
            warnings.append(f"approval {aid}: {why}")
        items = self.queue.approved_unexecuted()
        idle = sorted({a.role for a in items if a.role not in roles and a.role in self.sessions})
        if idle:
            warnings.append(f"approved items of role(s) {', '.join(idle)} not executed: "
                            "role not logged in")
        log.info("approved-only: %d approved item(s) to execute",
                 sum(1 for a in items if a.role in roles))
        try:
            for a in items:
                if a.role not in roles or a.role in self.auth_failed:
                    continue  # left approved and unexecuted for a later run
                if time.monotonic() - start >= budget_s:
                    status = "in_progress"
                    break
                if self.gate.breaker.tripped:
                    status = "paused_circuit"
                    break
                if a.category == "logout":
                    # Executing it would end the role's session: never run, even if approved.
                    self.queue.set_decision(a.id, "skip", decided_by="appmap")
                    msg = (f"approval {a.id} ({a.label}): logout actions are never executed "
                           "(they end the session); marked skip")
                    warnings.append(msg)
                    log.warning(msg)
                    res["skipped"] += 1
                    continue
                try:
                    outcome, reason = await self._execute_approval(a)
                except CircuitOpen:
                    status = "paused_circuit"
                    break
                except _AuthFailed:  # nothing was let through; the item stays approved
                    continue
                except _NotRunnable as exc:
                    res["skipped"] += 1
                    warnings.append(f"approval {a.id} ({a.label}): {exc}")
                    continue
                res[outcome] += 1
                if reason:
                    warnings.append(f"approval {a.id} ({a.label}): {reason}")
                    log.warning("[%s] approval %s %s: %s", a.role, a.id, outcome, reason)
                else:
                    log.info("[%s] approval %s executed: %s", a.role, a.id, a.label)
        finally:
            await self.recorder.drain()
        status = self._auth_status(status)
        stats = self.stats(status)
        stats.approvals = res
        stats.warnings += warnings
        self._auth_report(stats)
        log.info("approved-only stopped: status=%s %s", status, res)
        return stats

    def _recover_interrupted(self) -> list[tuple[str, str]]:
        """Items whose final action was let through by a run that never finished: the
        request may have been sent, so they count as executed and are never re-run."""
        out = []
        for aid, started in self.queue.in_flight().items():
            row = self.store.conn.execute("SELECT role, label FROM approvals WHERE id=?",
                                          (aid,)).fetchone()
            reason = "interrupted while executing; outcome unknown (not re-run)"
            ev = self.events.emit("approval_executed", role=row["role"] if row else None,
                                  caused_by=started, approval_id=aid,
                                  label=row["label"] if row else "",
                                  outcome="unknown (interrupted)", error=reason)
            self.queue.mark_executed(aid, ev.id)
            self.queue.mark_failed(aid, reason)
            out.append((aid, reason))
        return out

    def _approval_failed(self, a: PendingAction, reason: str) -> tuple[str, str]:
        """An approved item that could not be executed (nothing destructive was sent)."""
        self.queue.mark_failed(a.id, reason)
        self.events.emit("error", role=a.role, approval_id=a.id,
                         message=f"approved action not executed: {reason}")
        return "failed", reason

    def _start_approval(self, a: PendingAction, mode: str, label: str):
        """Persist that the approved action is about to be let through (before it is):
        an interrupted run then never executes it a second time. The row is re-read
        first: another process (or an edit of approvals.yaml) may have run, started or
        revoked it since the list was read."""
        row = self.store.conn.execute("SELECT decision, executed_event FROM approvals "
                                      "WHERE id=?", (a.id,)).fetchone()
        if row is None or row["decision"] != "approve":
            raise _NotRunnable("no longer approved; not executed")
        if row["executed_event"] is not None or a.id in self.queue.in_flight():
            raise _NotRunnable("already executed or in flight; not executed again")
        ev = self.events.emit("approval_started", role=a.role, approval_id=a.id,
                              category=a.category, label=label, mode=mode)
        self.queue.mark_started(a.id, ev.id)
        return ev

    def _finish_approval(self, a: PendingAction, role: str, caused_by: str, mode: str,
                         label: str, mark: int, error: str | None, interrupted: bool,
                         **data) -> None:
        ids = self._ids_since(mark, role)
        if interrupted:
            outcome = "unknown (interrupted)"
            error = error or "interrupted while executing; outcome unknown"
        else:
            outcome = "error" if error else "done"
        done = self.events.emit("approval_executed", role=role, caused_by=caused_by,
                                approval_id=a.id, category=a.category, label=label, mode=mode,
                                exchange_ids=ids, outcome=outcome, error=error, **data)
        self.queue.mark_executed(a.id, done.id)  # at most once, whatever happened
        self._settle_frontier(role, a.action_path)
        if error:
            self.queue.mark_failed(a.id, f"approved action: {error}")

    async def _execute_approval(self, a: PendingAction) -> tuple[str, str | None]:
        sess = self.sessions.get(a.role)
        if sess is None:
            return self._approval_failed(a, f"role {a.role!r} has no session")
        path = a.action_path or []
        last = path[-1] if path else {}
        pred = a.predicted_request or {}
        if last.get("type") in ("click", "submit"):
            mode = "ui"
        elif last.get("type") == "request" or (not path and pred.get("url")):
            mode = "direct"  # replay / schema / cross-role requests
        elif last.get("type") == "goto":
            # a held link, or a request the page load fired: the browser re-sends the real one
            mode = "goto"
        else:
            return self._approval_failed(a, "approval has no action or request to perform")
        if mode == "direct":
            return await self._execute_direct(sess, a)
        page = await self._open_page(sess)
        try:
            return await self._execute_ui(sess, page, a, mode)
        finally:
            self._guard = None
            if not page.is_closed():
                await page.close()
            await self.recorder.drain()

    async def _execute_direct(self, sess: RoleSession, a: PendingAction) -> tuple[str, str | None]:
        import functools

        from .direct import DirectClient
        from .recording import record_exchange

        role = sess.role.name
        last = a.action_path[-1] if a.action_path else {}
        step = last if last.get("type") == "request" else {}
        pred = a.predicted_request or {}
        method = str(step.get("method") or pred.get("method") or "").upper()
        url = step.get("url") or pred.get("url")
        if not method or not url:
            return self._approval_failed(a, "approval has no request to send")
        body = step.get("body")
        if "<redacted:" in url or "<redacted:" in (body or ""):
            return self._approval_failed(a, "the recorded request holds redacted credentials; "
                                            "not sent")
        if not self.gate.scope.allows(url):
            return self._approval_failed(a, "request URL is out of scope; not sent")
        if self.gate.breaker.tripped:
            raise CircuitOpen(f"circuit breaker tripped; approval {a.id} not sent")
        rec = functools.partial(record_exchange, self.store, self.events, self.redactor,
                                self.rd, known_patterns=self.recorder.known_patterns)
        client = DirectClient(self.gate, rec, self.events)
        ctype = step.get("content_type")
        mark = self._exchange_mark()
        started = self._start_approval(a, "direct", a.label)
        got = None
        error = None
        finished = False
        try:
            try:
                # An approved write is sent once: its redirect is recorded, not followed.
                got = await client.request(
                    sess.context.request, method, url, role=role, origin="approved",
                    headers={"Content-Type": ctype} if body is not None and ctype else None,
                    data=body.encode("utf-8") if body is not None else None,
                    extra_auth=dict(sess.auth_headers) or None,
                    follow_redirects=method in ("GET", "HEAD"))
            except CircuitOpen:
                if not self._ids_since(mark, role):  # refused before anything was sent
                    self.queue.unmark_started(a.id)
                    started = None
                    raise
                error = "circuit breaker tripped while following redirects"
            if got is None and error is None:
                error = "request failed on the network (see events)"
            finished = True
        finally:
            if started is not None:
                self._finish_approval(a, role, started.id, "direct", a.label, mark, error,
                                      not finished, method=method,
                                      url=self.redactor.url(url),
                                      status=got[0] if got else None)
        if error:
            return "failed", error
        status, resp = got
        location = resp.headers.get("location")
        if 300 <= status < 400 and location:  # new surface the action pointed to
            target = urljoin(url, location)
            if urlsplit(target).scheme in ("http", "https") and self.gate.scope.allows(target):
                self.frontier.push(role, "url", url=self._safe_url(target), depth=1,
                                   priority=PRIORITY["url"], source="ui")
        return "executed", None

    @staticmethod
    def _restore_blocker(prefix: list[dict]) -> dict | None:
        """The first restore step that would issue writes (or cannot be replayed)."""
        for s in prefix:
            if s.get("type") == "goto":
                continue
            if s.get("type") == "click" and not s.get("mutating"):
                continue
            return s
        return None

    async def _restore_for_approval(self, sess: RoleSession, page: Page, a: PendingAction,
                                    prefix: list[dict]) -> str | None:
        """Replay ``prefix`` with the guard fully armed (no held request passes, confirm()
        is dismissed). Returns a failure reason, or None when restored."""
        role = sess.role.name
        bad = self._restore_blocker(prefix)
        if bad is not None:
            return (f"restoring its action path needs a mutating step "
                    f"({self._describe([bad]).strip()}); not executed")
        if not prefix or prefix[0].get("type") != "goto":
            return "action path does not start at an entry URL"
        for attempt in (1, 2):
            hits = self._arm_guard(role, "load", prefix)
            try:
                await self._replay(page, prefix)
                await self.recorder.drain()
                if hits:
                    return ("restoring its action path issued a request that needs approval "
                            "(aborted); not executed")
                await self._check_lost(sess, page)
                return None
            except _Lost:
                if attempt == 2:
                    return "session lost while restoring"
                if not await self._relogin(sess, page):
                    raise _AuthFailed()
            except _Fail as exc:
                return f"restore failed: {exc}"
        return "session lost while restoring"

    async def _execute_ui(self, sess: RoleSession, page: Page, a: PendingAction,
                          mode: str) -> tuple[str, str | None]:
        role, path = sess.role.name, a.action_path
        step = path[-1]
        src = None
        if mode == "ui":
            why = await self._restore_for_approval(sess, page, a, path[:-1])
            if why:
                return self._approval_failed(a, why)
            src = await self._record_state(page, role, visit=False)
            await self._drain_nav(page)
        before = canon_url(page.url) if page.url.startswith("http") else ""
        form = loc = None
        try:  # locate the control before anything is let through
            if step["type"] == "click":
                loc = await self._resolve(page, step["locators"])
            elif step["type"] == "submit":
                form = await self._find_form(page, step)
                if form is None:
                    raise _Fail(f"form {step.get('form_label')!r} not found after restore")
        except _Fail as exc:
            return self._approval_failed(a, str(exc))
        pred = a.predicted_request or {}
        pm = str(pred.get("method") or "").upper()
        match = ((pm, self._norm_url(pred["url"]))
                 if pm and pm not in _SAFE_METHODS and pred.get("url") else None)
        label = self._step_label(step) or a.label
        mark = self._exchange_mark()
        hits = self._arm_guard(role, "action", path, label=label,
                               context=step.get("context", ""), state=src.id if src else None)
        started = self._start_approval(a, mode, label)
        # R21: a predicted write passes by method + URL; without one, only held requests of
        # the acted-on page during this action window pass (popups/workers stay guarded).
        self._approval = {"role": role, "match": match, "page": page, "allowed": []}
        self._confirm_mode = "defer" if form is not None and form.has_confirm else "accept"
        self.recorder.origin_by_role[role] = "approved"
        result = None
        error = None
        allowed: list = []
        ev = None
        finished = False
        try:
            try:
                ev = self.events.emit("navigate" if mode == "goto" else step["type"],
                                      role=role, caused_by=started.id, approval_id=a.id,
                                      label=label,
                                      url=self.redactor.url(step.get("url") or page.url))
                if mode == "goto":
                    await self._goto(page, step["url"])
                elif step["type"] == "click":
                    await loc.click(timeout=ACTION_TIMEOUT_MS)
                    await asyncio.sleep(0.05)
                    await wait_settled(page, self.limits.quiet_window_ms)
                else:
                    self._learn_password_fields(page.url, form)
                    result = await fill_and_submit(
                        page, form, self.gen, max_retries=0,
                        settle_ms=self.limits.quiet_window_ms,
                        exchange_ids_since=lambda: self._ids_since(mark, role),
                        learn_secret=self.redactor.learn_secret)
            except CircuitOpen:
                error = "circuit breaker tripped during the action"
            except Exception as exc:
                error = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            finally:
                try:
                    await self.recorder.drain()
                finally:
                    allowed = (self._approval or {}).get("allowed", [])
                    self._approval = None
                    self._confirm_mode = "dismiss"
                    self.recorder.origin_by_role.pop(role, None)
            finished = True
        finally:
            self._finish_approval(a, role, (ev or started).id, mode, label, mark, error,
                                  not finished,
                                  allowed_requests=[f"{m} {u}" for m, u in allowed],
                                  blocked_requests=[f"{m} {u}" for _f, m, u in hits])
        if error:
            return "failed", f"approved action: {error}"
        await self._after_approved(sess, page, step, src, ev.id, label,
                                   self._ids_since(mark, role), before, form, result)
        return "executed", None

    async def _after_approved(self, sess: RoleSession, page: Page, step: dict,
                              src: _State | None, ev_id: str, label: str, ids: list[int],
                              before: str, form: FormInfo | None, result) -> None:
        """Record the transition and queue the surface the approved action revealed."""
        role = sess.role.name
        try:
            if page.is_closed():
                return
            await self._close_popups(page)
            await self._check_lost(sess, page)
            navs = await self._drain_nav(page)
            dst = await self._record_state(page, role)
            if src is not None:
                locs = (step.get("locators") or []) if form is None else (
                    form.submit_locators or next(
                        (f.locators for f in reversed(form.fields) if f.locators), []))
                self.store.add_transition(src.id, dst.id, role, step["type"], label,
                                          self._locset(locs), ev_id, ids)
            if result is not None:
                self._update_form(step.get("form_id"), result.success,
                                  result.validation_messages, ids, result.values_tried)
            self._push_nav_urls(role, navs, page.url, 0)
            # Children are restored from the URL the action left the page on: re-doing the
            # approved action as a restore step is never allowed.
            here = self._safe_url(page.url)
            if canon_url(page.url) != before and self.frontier.push(
                    role, "url", url=here, depth=1, priority=PRIORITY["url"], source="ui"):
                self.frontier.mark(self.frontier.id_of(role, "url", url=here), "done")
            await self._expand(page, role, dst, [{"type": "goto", "url": here}], 1)
        except _Lost:  # the approved action ended the session
            if not await self._relogin(sess, page):
                log.warning("[%s] re-login after an approved action failed", role)
        except _Fail as exc:
            self.events.emit("error", role=role, message=f"after approved action: {exc}")

    def _approval_allows(self, req_method: str, req_url: str, page, role: str) -> bool:
        """During an approved action only: its predicted request (method + URL), or, when
        no write was predicted, held requests of the acted-on page in the action window."""
        ap = self._approval
        if ap is None or ap["role"] != role:
            return False
        if ap["match"] is not None:
            method, url = ap["match"]
            rm = req_method.upper()
            # a form's hidden _method override is sent as POST
            if not (rm == method or (rm == "POST" and method not in ("GET", "POST"))):
                return False
            if self._norm_url(req_url) != url:
                return False
        elif page is None or page is not ap["page"]:  # workers have no frame
            return False
        ap["allowed"].append((req_method, self.redactor.url(req_url)))
        return True

    def _norm_url(self, url: str) -> str:
        p = urlsplit(canon_url(self.redactor.url(url)))
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))

    def _settle_frontier(self, role: str, path: list[dict]) -> None:
        """The frontier item that was held for this approval is now done."""
        if not path:
            return
        if len(path) == 1 and path[0].get("type") == "goto":
            self.store.conn.execute(
                "UPDATE frontier SET status='done' WHERE role=? AND kind='url' AND url=? "
                "AND status='queued_approval'", (role, path[0].get("url") or ""))
        else:
            self.store.conn.execute(
                "UPDATE frontier SET status='done' WHERE role=? AND kind='action' "
                "AND action_path=? AND status='queued_approval'",
                (role, json.dumps(path, sort_keys=True)))

    # -- item processing -----------------------------------------------------
    async def _process(self, sess: RoleSession, page: Page, item: FrontierItem) -> str:
        try:
            outcome = await self._process_guarded(sess, page, item)
            g = self._guard
            if g is not None and g["kind"] == "action" and g["hits"]:
                outcome = "queued_approval"  # its request fired late (timer) and was held
            return outcome
        finally:
            self._guard = None

    async def _process_guarded(self, sess: RoleSession, page: Page, item: FrontierItem) -> str:
        role = sess.role.name
        entry = ([{"type": "goto", "url": item.url}] if item.kind == "url"
                 else item.action_path[:-1])
        for attempt in (1, 2):
            self._arm_guard(role, "load", entry, item=item.id)
            try:
                if item.kind == "url":
                    return await self._do_url(sess, page, item)
                return await self._do_action(sess, page, item)
            except _Lost:
                if attempt == 2:
                    self.events.emit("error", role=role, message="session lost again after re-login",
                                     url=self.redactor.url(page.url))
                    return "failed"
                if not await self._relogin(sess, page):
                    raise _AuthFailed()  # the item is not at fault: it stays pending
                log.info("[%s] session restored; retrying item %d", role, item.id)
            except _Fail as exc:
                self.events.emit("error", role=role, message=str(exc))
                log.info("[%s] item %d failed: %s", role, item.id, exc)
                return "failed"
            except (CircuitOpen, _AuthFailed):
                raise
            except Exception as exc:  # a single item must never stop the crawl
                msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                self.events.emit("error", role=role, message=f"item failed: {msg}")
                log.warning("[%s] item %d error: %s", role, item.id, msg)
                return "failed"
        return "failed"

    async def _relogin(self, sess: RoleSession, page: Page) -> bool:
        """Re-authenticate after a detected session loss. The crawl page is parked on
        about:blank (and popups closed) first, so its pending timers cannot fire while the
        login flow runs.

        At most ``MAX_RELOGINS`` consecutive re-logins per role (reset when an item
        completes); a failed re-login is never repeated in this invocation (lockout risk).
        Either way the role is stopped (``auth_failed``). Manual roles never log in here."""
        role = sess.role.name
        if role in self.auth_failed:
            return False
        n = self._relogins.get(role, 0)
        if n >= MAX_RELOGINS:
            self._stop_role(role, f"session lost again after {n} re-login(s) "
                                  f"(limit {MAX_RELOGINS})")
            return False
        self._relogins[role] = n + 1
        self.events.emit("session_lost", role=role, url=self.redactor.url(page.url),
                         status=self.recorder.last_status.get(role))
        await self._close_popups(page)
        try:
            await page.goto("about:blank")
        except Exception:
            pass
        sess.auth_headers.clear()
        ok = await login(sess, self.cfg, self.events)
        if not ok:
            self._stop_role(role, "manual role's session expired" if sess.role.login.type
                            == "manual" else "re-login after session loss failed")
        return ok

    async def _check_lost(self, sess: RoleSession, page: Page) -> None:
        """A 401 or a login-looking page is only a *candidate* loss (field fix 2: apps
        answer 401 to a logged-in user opening a resource they do not own). Confirmed
        with a known-authenticated probe: a live session makes it an access-denied page
        (recorded as a normal state, no re-login); only a dead one raises ``_Lost``."""
        last = self.recorder.last_status.get(sess.role.name)
        if not await is_session_lost(sess, page, last, sess.role.login.url):
            return
        if await self._session_alive(sess, page.url):
            self._record_denied(sess.role.name, page.url, last)
            return
        raise _Lost()

    async def _session_alive(self, sess: RoleSession, current: str) -> bool:
        """Load a page that needs the session (``auth_probe_urls``) in a separate tab:
        alive unless it answers 401 or shows/redirects to the login form. Nothing to
        probe, or the probe fails: not alive (the old behaviour: re-login)."""
        role = sess.role.name
        urls = auth_probe_urls(sess, self.store, sess.role.login.url, exclude=current)
        if not urls:
            return False
        saved_guard, saved_last = self._guard, self._last_guard.get(role)
        prev_origin = self.recorder.origin_by_role.get(role)
        probe: Page | None = None
        try:
            probe = await self._open_page(sess)
            self.recorder.origin_by_role[role] = "login"  # auth bookkeeping, not the map
            for url in urls:
                self._arm_guard(role, "load", [{"type": "goto", "url": self._safe_url(url)}])
                try:
                    resp = await probe.goto(url, wait_until="load")
                except Exception:
                    continue
                try:
                    await wait_settled(probe, self.limits.quiet_window_ms)
                except Exception:
                    pass
                alive = not await is_session_lost(sess, probe, resp.status if resp else None,
                                                  sess.role.login.url)
                log.info("[%s] session liveness probe %s: %s", role, self.redactor.url(url),
                         "alive" if alive else "lost")
                return alive
            return False
        except Exception as exc:
            log.warning("[%s] session liveness probe failed: %s", role, exc)
            return False
        finally:
            if probe is not None and not probe.is_closed():
                await probe.close()
            await self.recorder.drain()
            if prev_origin is None:
                self.recorder.origin_by_role.pop(role, None)
            else:
                self.recorder.origin_by_role[role] = prev_origin
            self._guard = saved_guard
            if saved_last is not None:
                self._last_guard[role] = saved_last
            self.recorder.last_status[role] = None  # the item's own 401 was handled

    def _record_denied(self, role: str, url: str, status: int | None) -> None:
        safe = self.redactor.url(url)
        detail = f"HTTP {status} with a live session" if status == 401 else \
            "login page shown with a live session"
        ev = self.events.emit("observation", role=role, kind="access_denied", url=safe,
                              status=status, detail=detail)
        self.store.add_observation("access_denied", detail, safe, role, ev.id)
        log.info("[%s] access denied (session alive): %s %s", role, status, safe)

    async def _do_url(self, sess: RoleSession, page: Page, item: FrontierItem) -> str:
        role, url = sess.role.name, item.url
        flag = classify(ActionDescriptor("click", "GET", url, "", "", False, role, "ui"),
                        self.cfg.profile)
        if flag.held:
            self._enqueue(role, None, url, flag, [{"type": "goto", "url": url}],
                          {"method": "GET", "url": url})
            return "queued_approval"
        if self._is_login_url(sess, url):
            return "skipped"  # visiting it would look exactly like a lost session
        mark = self._exchange_mark()
        ev = self.events.emit("navigate", role=role, url=url)
        await self._goto(page, url)
        await self._check_lost(sess, page)
        st = await self._record_state(page, role)
        await self.recorder.drain()
        ids = self._ids_since(mark, role)
        for link_id, from_state, label, locs in self.frontier.mark_visited(role, url, st.id):
            self.store.add_transition(from_state, st.id, role, "navigate", label,
                                      self._locset(locs), ev.id, ids)
            self.frontier.resolve_link(link_id, st.id)
        await self._expand(page, role, st, [{"type": "goto", "url": url}], item.depth)
        return "done"

    async def _do_action(self, sess: RoleSession, page: Page, item: FrontierItem) -> str:
        role, path = sess.role.name, item.action_path
        if not path:
            raise _Fail("action item without a path")
        step, restore = path[-1], path[:-1]
        flag = classify(self._step_descriptor(step, role), self.cfg.profile)
        if flag.held:  # never restore toward an action that will not be performed
            self._enqueue(role, None, self._step_label(step), flag, path, self._predicted(step))
            return "queued_approval"
        restore_hits = self._guard["hits"]
        await self._replay(page, restore)
        if restore_hits:
            raise _Fail("restoring the action path issued a request that needs approval; "
                        "aborted it")
        await self._check_lost(sess, page)
        src = await self._record_state(page, role, visit=False)
        before = canon_url(page.url)
        await self._drain_nav(page)
        self._confirm_seen = False
        mark = self._exchange_mark()
        result = None
        hits = self._arm_guard(role, "action", path, label=self._step_label(step),
                               context=step.get("context", ""), state=src.id, item=item.id)
        if step["type"] == "click":
            loc = await self._resolve(page, step["locators"])
            ev = self.events.emit("click", role=role, label=step.get("label", ""),
                                  url=self.redactor.url(page.url))
            await loc.click(timeout=ACTION_TIMEOUT_MS)
            await asyncio.sleep(0.05)
            await wait_settled(page, self.limits.quiet_window_ms)
            locs, label = step["locators"], step.get("label", "")
        elif step["type"] == "submit":
            form = await self._find_form(page, step)
            if form is None:
                raise _Fail(f"form {step.get('form_label')!r} not found after restore")
            live = self._form_descriptor(form, role, step.get("context", ""))
            flag = classify(live, self.cfg.profile)
            if flag.held:
                self._enqueue(role, src.id, form.label, flag, path, self._predicted(step))
                return "queued_approval"
            ev = self.events.emit("submit", role=role, label=form.label, method=form.method,
                                  action=form.action, url=self.redactor.url(page.url))
            self._learn_password_fields(page.url, form)
            result = await fill_and_submit(
                page, form, self.gen, max_retries=self.limits.max_form_retries,
                settle_ms=self.limits.quiet_window_ms,
                exchange_ids_since=lambda: self._ids_since(mark, role),
                learn_secret=self.redactor.learn_secret)
            locs = form.submit_locators or next(
                (f.locators for f in reversed(form.fields) if f.locators), [])
            label = form.label
        else:
            raise _Fail(f"unknown action step type {step.get('type')!r}")
        await self.recorder.drain()
        await self._close_popups(page)
        if hits:  # the action fired a destructive request: aborted and queued by the guard
            return "queued_approval"
        if self._confirm_seen:
            confirm = Flag(True, "delete", ["action opened a confirm() dialog (dismissed)"])
            self._enqueue(role, src.id, label, confirm, path, self._predicted(step))
            return "queued_approval"
        ids = self._ids_since(mark, role)
        mutating = self._has_write(ids)
        if mutating and step["type"] == "click":
            # Re-clicking it to restore children would repeat the write: mark it.
            step["mutating"] = True
            self.frontier.set_action_path(item.id, path)
        await self._check_lost(sess, page)
        navs = await self._drain_nav(page)
        dst = await self._record_state(page, role)
        # A client route change always yields a transition, whether or not the DOM
        # fingerprint changed (the state id also covers the URL's route pattern).
        self.store.add_transition(src.id, dst.id, role, step["type"], label,
                                  self._locset(locs), ev.id, ids)
        self._push_nav_urls(role, navs, page.url, item.depth)
        if result is not None:
            self._update_form(step.get("form_id"), result.success,
                              result.validation_messages, ids, result.values_tried)
        after = canon_url(page.url)
        if after != before:
            # A new URL is a fresh entry point: restore from it instead of re-acting.
            safe = self._safe_url(after)
            if self.frontier.push(role, "url", url=safe, depth=item.depth,
                                  priority=PRIORITY["url"], source="ui"):
                self.frontier.mark(self.frontier.id_of(role, "url", url=safe), "done")
            entry = [{"type": "goto", "url": safe}]
            fully = dst.new and not self._saturated(role, dst.pattern)
            created = result is not None and result.success and self._is_resource_url(after)
            self._creating = created
            try:
                await self._expand(page, role, dst, entry, item.depth)
                if created and not fully:
                    # The crawler just created this record: queue its destructive controls
                    # even though pages like it were seen, so approvals can act on it.
                    await self._push_controls(page, role, dst, entry, item.depth + 1,
                                              held_only=True)
            finally:
                self._creating = False
        elif step["type"] == "click" and not mutating:
            await self._expand(page, role, dst, path, item.depth)
        # A submit or write-issuing click that stayed on its URL is not expanded:
        # restoring its children would repeat the write.
        return "done"

    # -- SPA routes ---------------------------------------------------------------
    async def _drain_nav(self, page: Page) -> list[dict]:
        """Client route changes (pushState/replaceState/hashchange/popstate) since last read."""
        try:
            return await page.evaluate(DRAIN_NAV_JS)
        except Exception:
            return []

    def _push_nav_urls(self, role: str, navs: list[dict], current: str, depth: int) -> None:
        """In-scope URLs the app navigated through during an action become url items."""
        for n in navs:
            url = n.get("url") or ""
            if not url.startswith(("http://", "https://")) or not self.gate.scope.allows(url) \
                    or canon_url(url) == canon_url(current):
                continue
            self.frontier.push(role, "url", url=self._safe_url(url), depth=depth + 1,
                               priority=PRIORITY["url"], source="ui")

    async def _seed_spa(self, page: Page, role: str) -> int:
        """Detect the SPA (until a router is found), record its routes, and queue each
        concrete route for this role. Returns how many new frontier items were queued."""
        if self.cfg.spa == "off" or not page.url.startswith(("http://", "https://")):
            return 0
        if role not in self._spa_routes:
            info = await detect_spa(page)
            if not (info["has_router"] or self.cfg.spa == "on"):
                return 0

            async def fetch_text(url: str) -> str | None:
                try:
                    return await page.evaluate(
                        "u => fetch(u).then(r => r.ok ? r.text() : null)", url)
                except Exception:
                    return None

            routes = await extract_routes(page, page.url, fetch_text)
            if not routes and info["framework"] in ("unknown", "react", "angular") \
                    and self.cfg.spa != "on":
                return 0
            self._spa_routes[role] = routes
            self._spa_base[role] = page.url
            host = urlsplit(page.url).netloc.lower()
            log.info("[%s] SPA %s: %d routes", role, info["framework"], len(routes))
            for r in routes:
                rid = self.store.upsert_route(r.pattern, host, "spa_route")
                self.store.upsert_source("route", rid, r.source)
                if r.pattern not in self.known:
                    self.known.append(r.pattern)
                if r.pattern not in self.recorder.known_patterns:
                    self.recorder.known_patterns.append(r.pattern)
        return self._fill_spa(role)

    def _fill_spa(self, role: str) -> int:
        base, pushed = self._spa_base[role], 0
        for r in self._spa_routes[role]:
            if "{" in r.pattern:
                # Sampled like lists: at most ``cap`` concrete URLs per role and pattern over
                # the whole run (what earlier invocations queued counts).
                room = self.cap - self._spa_sampled(role, base, r.pattern)
                if room <= 0:
                    continue
                ids = observed_ids(self.store.conn, Path(self.rd.root), role, r.pattern, 1000)
                if not ids:
                    self._spa_skipped[(role, r.pattern)] = (
                        f"spa route {r.pattern}: no observed ids for role {role}; "
                        "recorded, not queued")
                    continue
                self._spa_skipped.pop((role, r.pattern), None)
                urls = [u for u in (concrete_url(base, r.pattern, i) for i in ids)
                        if self.frontier.id_of(role, "url", url=self._safe_url(u)) is None]
                urls = urls[:room]  # ids the crawl already reached are not repeated
            else:
                urls = [concrete_url(base, r.pattern, "")]
            for url in urls:
                if self.gate.scope.allows(url) and self.frontier.push(
                        role, "url", url=self._safe_url(url), depth=0,
                        priority=PRIORITY["url"], source="spa_router"):
                    pushed += 1
        return pushed

    def _spa_sampled(self, role: str, base: str, pattern: str) -> int:
        """Concrete URLs of ``pattern`` already queued from the router for ``role``."""
        key = self._url_pattern(concrete_url(base, pattern, "0"))
        return sum(1 for r in self.store.conn.execute(
            "SELECT url FROM frontier WHERE role=? AND kind='url' AND source='spa_router'",
            (role,)) if self._url_pattern(r["url"]) == key)

    # -- restore -------------------------------------------------------------
    async def _goto(self, page: Page, url: str) -> None:
        try:
            await page.goto(url, timeout=NAV_TIMEOUT_MS)
        except Exception as exc:
            # A failed/blocked navigation can leave the page on chrome-error with stale
            # navigations queued; a fresh page is the only reliable reset.
            await page.close()
            raise _Fail(f"navigation to {self.redactor.url(url)} failed: "
                        f"{str(exc).splitlines()[0]}") from None
        await wait_settled(page, self.limits.quiet_window_ms)

    async def _replay(self, page: Page, steps: list[dict]) -> None:
        if not steps or steps[0].get("type") != "goto":
            raise _Fail("action path does not start at an entry URL")
        for step in steps:
            if step["type"] == "goto":
                await self._goto(page, step["url"])
            elif step["type"] == "click":
                if step.get("mutating"):
                    raise _Fail("cannot replay a click that issues writes while restoring state")
                loc = await self._resolve(page, step["locators"])
                await loc.click(timeout=ACTION_TIMEOUT_MS)
                await asyncio.sleep(0.05)
                await wait_settled(page, self.limits.quiet_window_ms)
            else:
                raise _Fail(f"cannot replay a {step['type']!r} step while restoring state")

    async def _resolve(self, page: Page, locators: list[dict]):
        loc = await resolve(page, locators)
        if loc is None:  # control may sit behind a collapsed menu/tab
            try:
                await expand_reveals(page, self.cfg.profile)
            except Exception:
                pass
            loc = await resolve(page, locators)
        if loc is None:
            raise _Fail("locator did not resolve to exactly one element")
        return loc

    async def _find_form(self, page: Page, step: dict) -> FormInfo | None:
        forms = await discover_forms(page)
        want = _xpath(step.get("locators") or [])
        label = step.get("form_label", "")
        for f in forms:
            if f.label == label and want and _xpath(f.submit_locators) == want:
                return f
        return next((f for f in forms if f.label == label), None)

    async def _close_popups(self, page: Page) -> None:
        for p in list(page.context.pages):
            if p is not page:
                try:
                    await p.close()
                except Exception:
                    pass

    # -- states ----------------------------------------------------------------
    async def _record_state(self, page: Page, role: str, *, visit: bool = True) -> _State:
        """Fingerprint the page. ``visit=False`` only records a state not seen before."""
        url = page.url
        if not url.startswith(("http://", "https://")) or not self.gate.scope.allows(url):
            raise _Fail("page is not on an in-scope http(s) URL")
        fp = dom_fingerprint(await page.evaluate(SKELETON_JS))
        pattern = normalize_path(urlsplit(url).path or "/", self.known, marker=self.gen.marker)
        st = _State(state_id(pattern, fp, role), url, pattern, await page.title())
        exists = self.store.conn.execute("SELECT 1 FROM states WHERE id=?", (st.id,)).fetchone()
        if exists and not visit:
            return st
        st.new = exists is None
        ev = self.events.emit("state_entered", role=role, state_id=st.id,
                              url=self.redactor.url(url), route_pattern=pattern, new=st.new)
        shot = dom = None
        if st.new:
            shot, dom = await self._artifacts(page, st.id)
        self.store.upsert_state(st.id, role, self.redactor.url(url), pattern, fp,
                                self.redactor.text(st.title), first_seen_event=ev.id,
                                screenshot=shot, dom_snapshot=dom)
        rid = self.store.upsert_route(pattern, urlsplit(url).netloc.lower(), "page")
        self.store.upsert_source("route", rid, "ui")
        if pattern in self.known:
            self.store.upsert_source("route", rid, "spa_router")
        return st

    async def _artifacts(self, page: Page, sid: str) -> tuple[str | None, str | None]:
        art = Path(self.rd.artifacts_dir)
        art.mkdir(parents=True, exist_ok=True)
        shot: str | None = f"artifacts/{sid}.png"
        try:
            await page.screenshot(path=str(art / f"{sid}.png"), timeout=10000)
        except Exception:
            shot = None
        dom: str | None = f"artifacts/{sid}.html"
        try:
            html = await page.evaluate(_DOM_JS)
            (art / f"{sid}.html").write_text(self.redactor.text(html), encoding="utf-8")
        except Exception:
            dom = None
        return shot, dom

    def _saturated(self, role: str, pattern: str) -> bool:
        visits = self.store.conn.execute(
            "SELECT coalesce(sum(visits),0) FROM states WHERE role=? AND route_pattern=?",
            (role, pattern)).fetchone()[0]
        return visits > self.cap

    # -- expansion -------------------------------------------------------------
    async def _expand(self, page: Page, role: str, st: _State, parent: list[dict],
                      depth: int) -> None:
        if self._saturated(role, st.pattern):
            log.info("[%s] %s: %d-visit sample reached; not expanding", role, st.pattern, self.cap)
            return
        if depth + 1 > self.limits.max_depth:
            return
        try:
            await expand_reveals(page, self.cfg.profile)
        except Exception:
            pass
        clickables = await enumerate_clickables(page)
        here = canon_url(page.url)
        others: list[Clickable] = []
        for c in clickables:
            href = (c.href or "").strip()
            target = canon_url(urljoin(page.url, href)) if href else None
            if not target or href.startswith("#") or urlsplit(target).scheme not in ("http", "https"):
                others.append(c)  # in-page anchors (#, javascript:) act like buttons
                continue
            if target == here:
                continue  # link to the current page
            if not self.gate.scope.allows(target):
                self.store.upsert_external_link(self.redactor.url(target),
                                                self.redactor.url(page.url), role)
                continue
            self._push_link(role, st, self._safe_url(target), c, depth + 1)
        if not st.new:
            return  # controls and forms of a known state were queued on its first visit
        await self._push_controls(page, role, st, parent, depth + 1, others=others)

    async def _push_controls(self, page: Page, role: str, st: _State, parent: list[dict],
                             depth: int, *, others: list[Clickable] | None = None,
                             held_only: bool = False) -> None:
        """Queue forms and non-anchor clickables of the current page as child actions.

        ``held_only`` queues just the ones the classifier holds (for approval)."""
        if others is None:
            others = [c for c in await enumerate_clickables(page)
                      if not c.href or c.href.strip().startswith("#")
                      or urlsplit(urljoin(page.url, c.href)).scheme not in ("http", "https")]
        forms = await discover_forms(page)
        form_meta = await page.evaluate(_FORMS_JS)
        excluded = {x for f in forms
                    for x in [_xpath(f.submit_locators)] + [_xpath(fl.locators) for fl in f.fields]
                    if x}
        for f in forms:
            if held_only and not classify(self._form_descriptor(f, role, st.title),
                                          self.cfg.profile).held:
                continue
            self._push_form(role, st, parent, f, depth)
        groups: Counter = Counter()
        for c in others:
            xp = _xpath(c.locators)
            if c.tag in _SKIP_CLICK_TAGS or (xp and any(xp == e or xp.startswith(e + "/")
                                                       for e in excluded)):
                continue
            sig = _signature(c)
            if groups[sig] >= self.cap:  # list/card sampling
                continue
            groups[sig] += 1
            href = urljoin(page.url, c.href) if c.href else None
            step = {"type": "click", "locators": self._safe_locs(c.locators), "label": c.label}
            if href:
                step["href"] = self._safe_url(href)
            owner = next((m for m in form_meta if xp and xp.startswith(m["xpath"] + "/")), None)
            if owner:  # a secondary button of a form acts with that form's request
                step.update(method=owner["method"], action=self._safe_url(owner["action"]),
                            has_confirm=owner["confirm"], context=st.title)
            if held_only and not classify(self._step_descriptor(step, role),
                                          self.cfg.profile).held:
                continue
            self._push_action(role, st, parent + [step], depth, PRIORITY["click"])

    def _pattern_counts(self, role: str) -> Counter:
        if role not in self._url_patterns:
            cnt: Counter = Counter()
            for r in self.store.conn.execute(
                    "SELECT url FROM frontier WHERE role=? AND kind='url'", (role,)):
                cnt[self._url_pattern(r["url"])] += 1
            self._url_patterns[role] = cnt
        return self._url_patterns[role]

    def _url_pattern(self, url: str) -> str:
        p = urlsplit(url)
        return p.netloc.lower() + normalize_path(p.path or "/", self.known, marker=self.gen.marker)

    def _push_link(self, role: str, st: _State, target: str, c: Clickable, depth: int) -> None:
        item_id = self.frontier.id_of(role, "url", url=target)
        if item_id is None:
            counts = self._pattern_counts(role)
            pattern = self._url_pattern(target)
            if counts[pattern] >= self.cap:  # pagination / long lists: sample the pattern
                return
            flag = classify(ActionDescriptor("click", "GET", target, c.label, "", False, role,
                                             "ui"), self.cfg.profile)
            if not self.frontier.push(role, "url", url=target, depth=depth,
                                      priority=PRIORITY["url"], source="ui"):
                return
            counts[pattern] += 1
            if flag.held:
                self.frontier.mark(self.frontier.id_of(role, "url", url=target), "queued_approval")
                self._enqueue(role, st.id, c.label or target, flag,
                              [{"type": "goto", "url": target}], {"method": "GET", "url": target})
                return
        elif self.frontier.status_of(item_id) == "queued_approval":
            return
        # Persist link provenance; if the target was already visited, record the edge now.
        locs = self._safe_locs(c.locators)
        link_id = self.frontier.add_link(role, target, st.id, c.label, locs)
        to_state = self.frontier.visited_state(role, target) if link_id else None
        if to_state:
            self.store.add_transition(st.id, to_state, role, "navigate", c.label,
                                      self._locset(locs), None, [])
            self.frontier.resolve_link(link_id, to_state)

    def _push_form(self, role: str, st: _State, parent: list[dict], form: FormInfo,
                   depth: int) -> None:
        form_id = self._record_form(st.id, form)
        self._learn_password_fields(st.url, form)
        if self._is_login_form(role, form):
            return  # never re-authenticate (or switch identity) through a crawled login form
        step = {"type": "submit", "form_label": form.label,
                "locators": self._safe_locs(form.submit_locators), "form_id": form_id,
                "method": form.method, "action": self._safe_url(form.action) if form.action else None,
                "has_confirm": form.has_confirm, "context": st.title}
        self._push_action(role, st, parent + [step], depth, PRIORITY["submit"])

    def _push_action(self, role: str, st: _State, path: list[dict], depth: int,
                     priority: int) -> None:
        if not self.frontier.push(role, "action", url=self._safe_url(st.url), action_path=path,
                                  depth=depth, priority=priority, source="ui"):
            return
        step = path[-1]
        flag = classify(self._step_descriptor(step, role), self.cfg.profile)
        if flag.held:
            self.frontier.mark(self.frontier.id_of(role, "action", url=self._safe_url(st.url),
                                                   action_path=path), "queued_approval")
            self._enqueue(role, st.id, self._step_label(step), flag, path, self._predicted(step))

    # -- classification / approvals -------------------------------------------
    @staticmethod
    def _step_descriptor(step: dict, role: str) -> ActionDescriptor:
        if step.get("type") == "submit":
            return ActionDescriptor("submit", step.get("method"), step.get("action"),
                                    step.get("form_label", ""), step.get("context", ""),
                                    bool(step.get("has_confirm")), role, "ui")
        return ActionDescriptor("click", step.get("method"), step.get("href") or step.get("action"),
                                step.get("label", ""), step.get("context", ""),
                                bool(step.get("has_confirm")), role, "ui")

    @staticmethod
    def _form_descriptor(form: FormInfo, role: str, context: str) -> ActionDescriptor:
        # form.method already carries a hidden ``_method`` override (Rails/Laravel)
        return ActionDescriptor("submit", form.method, form.action, form.label, context,
                                form.has_confirm, role, "ui")

    @staticmethod
    def _step_label(step: dict) -> str:
        return step.get("form_label") or step.get("label") or step.get("url") or ""

    @staticmethod
    def _predicted(step: dict) -> dict | None:
        if step.get("type") == "submit" and (step.get("method") or step.get("action")):
            return {"method": step.get("method") or "POST", "url": step.get("action")}
        if step.get("href"):
            return {"method": "GET", "url": step["href"]}
        return None

    def _enqueue(self, role: str, state: str | None, label: str, flag: Flag,
                 path: list[dict], predicted: dict | None) -> str:
        aid = self.queue.enqueue(PendingAction(
            id="", role=role, state_id=state, category=flag.category, label=label,
            reasons=list(flag.reasons), action_path=path, predicted_request=predicted,
            created_by_run=self._creating))
        self.events.emit("approval_queued", role=role, approval_id=aid, category=flag.category,
                         label=label, reasons=flag.reasons)
        log.info("[%s] held for approval %s (%s): %s", role, aid, flag.category, label)
        return aid

    def _is_login_url(self, sess: RoleSession, url: str) -> bool:
        spec = sess.role.login
        if spec.type not in ("form", "manual") or not spec.url or spec.success_selector:
            return False
        a, b = urlsplit(url), urlsplit(spec.url)
        return (a.netloc.lower() == b.netloc.lower()
                and (a.path.rstrip("/") or "/") == (b.path.rstrip("/") or "/"))

    def _is_login_form(self, role: str, form: FormInfo) -> bool:
        if self.sessions[role].role.login.type == "none":
            return False
        pw = [f for f in form.fields if f.type == "password"]
        user = [f for f in form.fields if f.type in ("text", "email") and f.widget == "input"]
        return len(pw) == 1 and bool(user)

    # -- persistence helpers ----------------------------------------------------
    def _safe_url(self, url: str) -> str:
        return self.redactor.url(canon_url(url))

    def _safe_locs(self, locs: list[dict]) -> list[dict]:
        return [{**l, "value": self.redactor.text(l["value"])} for l in locs]

    def _locset(self, locs: list[dict]) -> int:
        ls = self.store.add_locator_set(locs[0]["strategy"] if locs else None)
        for l in locs:
            self.store.add_locator(ls, l["strategy"], self.redactor.text(l["value"]),
                                   l.get("rank", 0))
        return ls

    def _learn_password_fields(self, page_url: str, form: FormInfo) -> None:
        """Field fix 2: ``<input type=password>`` names (VTM's ``ssn``) redact like a
        password key in requests to the form's endpoint, whatever they are called.
        Persisted (names only) so later invocations and HAR finalization know them."""
        names = [f.name for f in form.fields if f.type == "password" and f.name]
        if not names:
            return
        target = urljoin(page_url, form.action) if form.action else page_url
        before = self.redactor.password_fields()
        for n in names:
            self.redactor.learn_password_field(target, n)
        after = self.redactor.password_fields()
        if after != before:
            self.store.set_meta("redact_password_fields", json.dumps(after))

    def _record_form(self, state: str, form: FormInfo) -> int:
        sub = self._locset(form.submit_locators) if form.submit_locators else None
        fid = self.store.add_form(state, inferred=form.inferred, method=form.method,
                                  submit_locator_set_id=sub)
        for f in form.fields:
            cons = {k: v for k, v in (("required", f.required), ("pattern", f.pattern),
                                      ("min", f.min), ("max", f.max),
                                      ("maxlength", f.maxlength)) if v not in (None, False)}
            self.store.add_field(fid, f.name, f.type, f.widget, constraints=cons or None,
                                 options=f.options or None,
                                 locator_set_id=self._locset(f.locators) if f.locators else None)
        return fid

    def _update_form(self, form_id: int | None, success: bool,
                     messages: dict[str, list[str]], ids: list[int],
                     values: dict[str, list] | None = None) -> None:
        if not form_id:
            return
        conn = self.store.conn
        ep = None
        if ids:
            marks = ",".join("?" * len(ids))
            row = conn.execute(
                f"SELECT endpoint_id FROM exchanges WHERE id IN ({marks}) "
                f"ORDER BY (method='GET'), id LIMIT 1", ids).fetchone()
            ep = row[0] if row else None
        conn.execute("UPDATE forms SET succeeded=?, action_endpoint_id=coalesce(?, action_endpoint_id) "
                     "WHERE id=?", (int(success), ep, form_id))
        for name, vals in (values or {}).items():
            row = conn.execute("SELECT id, values_tried FROM fields WHERE form_id=? AND name=?",
                               (form_id, name)).fetchone()
            if row is None:
                continue
            old = json.loads(row["values_tried"] or "[]")
            new = old + [v for v in (self.redactor.json(x) for x in vals) if v not in old]
            conn.execute("UPDATE fields SET values_tried=? WHERE id=?",
                         (json.dumps(new[-_MAX_VALUES_TRIED:]), row["id"]))
        for name, msgs in messages.items():
            if not name:
                continue
            row = conn.execute("SELECT id, validation_messages FROM fields WHERE form_id=? AND name=?",
                               (form_id, name)).fetchone()
            if row is None:
                continue
            old = json.loads(row["validation_messages"] or "[]")
            merged = old + [self.redactor.text(m) for m in msgs if m not in old]
            conn.execute("UPDATE fields SET validation_messages=? WHERE id=?",
                         (json.dumps(merged), row["id"]))

    def _has_write(self, ids: list[int]) -> bool:
        if not ids:
            return False
        marks = ",".join("?" * len(ids))
        return self.store.conn.execute(
            f"SELECT 1 FROM exchanges WHERE id IN ({marks}) AND upper(method) NOT IN "
            f"('GET','HEAD','OPTIONS') LIMIT 1", ids).fetchone() is not None

    def _is_resource_url(self, url: str) -> bool:
        """A URL naming one record: carries the run marker or an id/uuid/hash segment."""
        path = urlsplit(url).path or "/"
        return self.gen.marker in url or normalize_path(path, marker=self.gen.marker) != path

    def _exchange_mark(self) -> int:
        return int(self.store.conn.execute("SELECT coalesce(max(id),0) FROM exchanges").fetchone()[0])

    def _ids_since(self, mark: int, role: str) -> list[int]:
        return [r[0] for r in self.store.conn.execute(
            "SELECT id FROM exchanges WHERE id>? AND role=? ORDER BY id", (mark, role))]

    @staticmethod
    def _describe(steps: list[dict]) -> str:
        return " ".join(f"{s.get('type')}:{s.get('form_label') or s.get('label') or ''}"
                        for s in steps)


# -- runtime assembly (shared by the CLI and tests) ------------------------------
@dataclass
class Runtime:
    redactor: Redactor
    store: Store
    events: EventLog
    gate: RequestGate
    recorder: TrafficRecorder
    queue: ApprovalQueue


def make_redactor(cfg: RunConfig) -> Redactor:
    secrets = [c[1] for r in cfg.roles if (c := cfg.credentials(r.name))]
    return Redactor(secrets, cfg.redact_patterns)


def _learn_saved_sessions(redactor: Redactor, cfg: RunConfig, rd: RunDir) -> None:
    """Session values saved by ``appmap auth`` (another process): cookie values and
    token-named storage entries are scrubbed verbatim from this command's output too."""
    for role in cfg.roles:
        try:
            state = json.loads(state_path(rd, role.name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for c in state.get("cookies") or []:
            if isinstance(c, dict):
                redactor.learn(c.get("value"))
        for origin in state.get("origins") or []:
            for item in (origin.get("localStorage") or []) if isinstance(origin, dict) else []:
                if isinstance(item, dict) and re.search(r"token|auth|session|jwt|csrf|xsrf",
                                                        str(item.get("name")), re.I):
                    redactor.learn(item.get("value"))


def build_runtime(cfg: RunConfig, rd: RunDir) -> Runtime:
    redactor = make_redactor(cfg)
    _learn_saved_sessions(redactor, cfg, rd)
    store = Store(rd.db_path)
    store.init_schema()
    try:
        redactor.load_password_fields(
            json.loads(store.get_meta("redact_password_fields") or "{}"))
    except ValueError:
        pass
    events = EventLog(rd.events_path, redactor)
    lim = cfg.limits
    gate = RequestGate(ScopeGuard(cfg.scope),
                       RateLimiter(lim.max_requests_per_second, lim.jitter_seconds),
                       CircuitBreaker(lim.breaker_window, lim.breaker_threshold), store)
    known = [r["pattern"] for r in store.conn.execute(
        "SELECT pattern FROM routes WHERE kind='spa_route'")]
    recorder = TrafficRecorder(store, events, redactor, rd, gate, known)
    return Runtime(redactor, store, events, gate, recorder, ApprovalQueue(store, rd.approvals_path))


def make_datagen(cfg: RunConfig, rd: RunDir) -> DataGen:
    marker = cfg.form_data.marker_prefix.replace("{run_id}", rd.run_id)
    fixtures = None
    if cfg.form_data.fixtures:
        p = Path(cfg.form_data.fixtures).expanduser()
        if not p.is_absolute():
            p = cfg.source_path.parent / p
        fixtures = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return DataGen(marker, fixtures)


def select_roles(cfg: RunConfig, names: list[str] | None) -> list[Role]:
    if not names:
        return list(cfg.roles)
    by = {r.name: r for r in cfg.roles}
    missing = [n for n in names if n not in by]
    if missing:
        raise ValueError(f"unknown role(s): {', '.join(missing)}")
    return [by[n] for n in names]


async def run_crawl(cfg: RunConfig, rd: RunDir, *, roles: list[str] | None = None,
                    budget_minutes: float, approved_only: bool = False,
                    browser: Browser | None = None) -> CrawlStats:
    """Open role sessions, crawl, and checkpoint (HARs redacted, approvals.yaml written)."""
    if browser is None:
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            try:
                return await _crawl_with(cfg, rd, roles, budget_minutes, approved_only, b)
            finally:
                await b.close()
    return await _crawl_with(cfg, rd, roles, budget_minutes, approved_only, browser)


async def _crawl_with(cfg: RunConfig, rd: RunDir, names: list[str] | None, budget: float,
                      approved_only: bool, browser: Browser) -> CrawlStats:
    rt = build_runtime(cfg, rd)
    warnings: list[str] = []
    if rd.approvals_path.exists():
        rt.queue.load_decisions()
        warnings += [f"approvals.yaml: {w}" for w in rt.queue.warnings]
        rt.queue.write_yaml()  # rule decisions become visible items (no revocation on reload)
    sessions: dict[str, RoleSession] = {}
    crawl_roles: list[str] = []
    failed: dict[str, str] = {}
    try:
        for role in select_roles(cfg, names):
            sess = await open_role_session(browser, cfg, rd, role, rt.recorder)
            sessions[role.name] = sess
            # No saved session (``appmap auth`` not run): log in now. A manual role is
            # never prompted for here (fails closed: the operator runs ``appmap auth``).
            if role.login.type != "none" and not state_path(rd, role.name).exists() \
                    and not await login(sess, cfg, rt.events):
                failed[role.name] = ("no saved session (manual roles log in only through "
                                     "'appmap auth --manual-login')"
                                     if role.login.type == "manual" else "login failed")
                log.warning("role %s: %s; not crawled", role.name, failed[role.name])
            crawl_roles.append(role.name)
        crawler = Crawler(cfg, rd, rt.store, rt.events, rt.gate, rt.queue, rt.recorder,
                          sessions, make_datagen(cfg, rd))
        crawler.auth_failed.update(failed)
        stats = await crawler.run(crawl_roles, budget_minutes=budget, approved_only=approved_only)
        stats.warnings = warnings + stats.warnings
        return stats
    finally:
        await rt.recorder.drain()
        for name, sess in sessions.items():
            try:
                await sess.context.close()
                rt.recorder.finalize_har(name)
            except Exception as exc:
                log.warning("closing role %s failed: %s", name, exc)
        try:
            if rd.approvals_path.exists():  # keep decisions edited while the crawl ran
                rt.queue.load_decisions()
                for w in rt.queue.warnings:
                    log.warning("approvals.yaml: %s", w)
        except Exception as exc:
            log.warning("re-reading approvals.yaml failed: %s", exc)
        rt.queue.write_yaml()
        rt.store.close()
