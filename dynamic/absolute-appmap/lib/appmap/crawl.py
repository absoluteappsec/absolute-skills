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
from .browser.session import (RoleSession, ensure_authenticated, is_session_lost, login,
                              open_role_session, state_path)
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
from .store import Store
from .throttle import CircuitBreaker, CircuitOpen, RateLimiter

log = logging.getLogger("appmap.crawl")

CrawlStatus = Literal["done", "in_progress", "paused_circuit", "limit_reached"]
# url items first (cheap, map pages), then clicks, then form submissions
PRIORITY = {"url": 2, "click": 1, "submit": 0}
NAV_TIMEOUT_MS = 15000
ACTION_TIMEOUT_MS = 5000
_NTH = re.compile(r":nth-of-type\(\d+\)")
_SKIP_CLICK_TAGS = {"select", "textarea", "option", "label"}

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
        # anchor -> (from_state, label, locators), to record the link transition on visit
        self._links: dict[tuple[str, str], tuple[str, str, list[dict]]] = {}
        self._installed: set = set()
        self._confirm_seen = False
        self._accept_confirms = False  # set while executing approved actions (Task 17)

    # -- driver --------------------------------------------------------------
    async def run(self, roles: list[str], *, budget_minutes: float,
                  approved_only: bool = False) -> CrawlStats:
        if approved_only:
            # Executing approved actions is implemented by the approvals task (Task 17).
            log.info("approved-only crawl: no approved-action executor yet; nothing to do")
            return self.stats("done")
        start = time.monotonic()
        prior = float(self.store.get_meta("crawl_elapsed_s") or 0)
        budget_s = budget_minutes * 60
        limit_s = self.limits.max_runtime_minutes * 60
        status: CrawlStatus = "done"
        log.info("crawl started: roles=%s budget=%.2fmin pending=%d", roles, budget_minutes,
                 self.frontier.pending_count())
        try:
            for role in roles:
                status = await self._crawl_role(role, start, prior, budget_s, limit_s)
                if status != "done":
                    break
        finally:
            self.store.set_meta("crawl_elapsed_s", f"{prior + time.monotonic() - start:.3f}")
        if status == "done" and any(self.frontier.pending_count(r) for r in roles):
            status = "in_progress"
        stats = self.stats(status)
        log.info("crawl stopped: status=%s states=%d transitions=%d pending_approvals=%d",
                 status, stats.states, stats.transitions, stats.queued_approvals)
        return stats

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
                    return "done"
                if page is None or page.is_closed():
                    page = await self._open_page(sess)
                try:
                    outcome = await self._process(sess, page, item)
                except CircuitOpen:
                    return "paused_circuit"
                if self.gate.breaker.tripped:
                    log.warning("circuit breaker tripped; item %d left pending", item.id)
                    return "paused_circuit"
                self.frontier.mark(item.id, outcome)
                log.info("[%s] item %d %s %s -> %s", role, item.id, item.kind,
                         item.url or self._describe(item.action_path[-1:]), outcome)
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

    async def _on_dialog(self, dialog: Dialog, role: str, page: Page) -> None:
        self.events.emit("dialog", role=role, dialog_type=dialog.type,
                         message=(dialog.message or "")[:500], url=self.redactor.url(page.url))
        try:
            if dialog.type == "confirm" and not self._accept_confirms:
                self._confirm_seen = True
                await dialog.dismiss()
            else:  # alert, prompt, beforeunload: record and accept
                await dialog.accept()
        except Exception:
            pass  # already handled / page closed

    # -- item processing -----------------------------------------------------
    async def _process(self, sess: RoleSession, page: Page, item: FrontierItem) -> str:
        role = sess.role.name
        for attempt in (1, 2):
            try:
                if item.kind == "url":
                    return await self._do_url(sess, page, item)
                return await self._do_action(sess, page, item)
            except _Lost:
                if attempt == 2:
                    self.events.emit("error", role=role, message="session lost again after re-login",
                                     url=self.redactor.url(page.url))
                    return "failed"
                if not await ensure_authenticated(sess, page, self.cfg, self.events):
                    log.warning("[%s] re-login failed", role)
                    return "failed"
                log.info("[%s] session restored; retrying item %d", role, item.id)
            except _Fail as exc:
                self.events.emit("error", role=role, message=str(exc))
                log.info("[%s] item %d failed: %s", role, item.id, exc)
                return "failed"
            except CircuitOpen:
                raise
            except Exception as exc:  # a single item must never stop the crawl
                msg = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
                self.events.emit("error", role=role, message=f"item failed: {msg}")
                log.warning("[%s] item %d error: %s", role, item.id, msg)
                return "failed"
        return "failed"

    async def _check_lost(self, sess: RoleSession, page: Page) -> None:
        last = self.recorder.last_status.get(sess.role.name)
        if await is_session_lost(sess, page, last, sess.role.login.url):
            raise _Lost()

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
        link = self._links.pop((role, url), None)
        if link:
            await self.recorder.drain()
            from_state, label, locs = link
            self.store.add_transition(from_state, st.id, role, "navigate", label,
                                      self._locset(locs), ev.id, self._ids_since(mark, role))
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
        await self._replay(page, restore)
        await self._check_lost(sess, page)
        src = await self._record_state(page, role, visit=False)
        before = canon_url(page.url)
        self._confirm_seen = False
        mark = self._exchange_mark()
        result = None
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
            result = await fill_and_submit(
                page, form, self.gen, max_retries=self.limits.max_form_retries,
                settle_ms=self.limits.quiet_window_ms,
                exchange_ids_since=lambda: self._ids_since(mark, role))
            locs = form.submit_locators or next(
                (f.locators for f in reversed(form.fields) if f.locators), [])
            label = form.label
        else:
            raise _Fail(f"unknown action step type {step.get('type')!r}")
        await self.recorder.drain()
        await self._close_popups(page)
        if self._confirm_seen:
            confirm = Flag(True, "delete", ["action opened a confirm() dialog (dismissed)"])
            self._enqueue(role, src.id, label, confirm, path, self._predicted(step))
            return "queued_approval"
        ids = self._ids_since(mark, role)
        await self._check_lost(sess, page)
        dst = await self._record_state(page, role)
        self.store.add_transition(src.id, dst.id, role, step["type"], label,
                                  self._locset(locs), ev.id, ids)
        if result is not None:
            self._update_form(step.get("form_id"), result.success,
                              result.validation_messages, ids)
        after = canon_url(page.url)
        if after != before:
            # A new URL is a fresh entry point: restore from it instead of re-acting.
            safe = self._safe_url(after)
            if self.frontier.push(role, "url", url=safe, depth=item.depth,
                                  priority=PRIORITY["url"], source="ui"):
                self.frontier.mark(self.frontier.id_of(role, "url", url=safe), "done")
            await self._expand(page, role, dst, [{"type": "goto", "url": safe}], item.depth)
        elif step["type"] == "click":
            await self._expand(page, role, dst, path, item.depth)
        # a submit that stayed on its URL is not expanded: restoring would resubmit
        return "done"

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
        forms = await discover_forms(page)
        excluded = {x for f in forms
                    for x in [_xpath(f.submit_locators)] + [_xpath(fl.locators) for fl in f.fields]
                    if x}
        for f in forms:
            self._push_form(role, st, parent, f, depth + 1)
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
            self._push_action(role, st, parent + [step], depth + 1, PRIORITY["click"])

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
        if self.frontier.id_of(role, "url", url=target) is not None:
            return
        counts = self._pattern_counts(role)
        pattern = self._url_pattern(target)
        if counts[pattern] >= self.cap:  # pagination / long lists: sample the pattern
            return
        flag = classify(ActionDescriptor("click", "GET", target, c.label, "", False, role, "ui"),
                        self.cfg.profile)
        if not self.frontier.push(role, "url", url=target, depth=depth, priority=PRIORITY["url"],
                                  source="ui"):
            return
        counts[pattern] += 1
        if flag.held:
            self.frontier.mark(self.frontier.id_of(role, "url", url=target), "queued_approval")
            self._enqueue(role, st.id, c.label or target, flag, [{"type": "goto", "url": target}],
                          {"method": "GET", "url": target})
        else:
            self._links[(role, target)] = (st.id, c.label, self._safe_locs(c.locators))

    def _push_form(self, role: str, st: _State, parent: list[dict], form: FormInfo,
                   depth: int) -> None:
        form_id = self._record_form(st.id, form)
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
        return ActionDescriptor("click", None, step.get("href"), step.get("label", ""), "",
                                False, role, "ui")

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
            reasons=list(flag.reasons), action_path=path, predicted_request=predicted))
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
                     messages: dict[str, list[str]], ids: list[int]) -> None:
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


def build_runtime(cfg: RunConfig, rd: RunDir) -> Runtime:
    redactor = make_redactor(cfg)
    store = Store(rd.db_path)
    store.init_schema()
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
    sessions: dict[str, RoleSession] = {}
    crawl_roles: list[str] = []
    try:
        for role in select_roles(cfg, names):
            sess = await open_role_session(browser, cfg, rd, role, rt.recorder)
            sessions[role.name] = sess
            # No saved session (``appmap auth`` not run): log in now.
            if role.login.type != "none" and not state_path(rd, role.name).exists() \
                    and not await login(sess, cfg, rt.events):
                warnings.append(f"role {role.name}: login failed; role not crawled")
                log.warning("role %s: login failed; skipped", role.name)
                continue
            crawl_roles.append(role.name)
        crawler = Crawler(cfg, rd, rt.store, rt.events, rt.gate, rt.queue, rt.recorder,
                          sessions, make_datagen(cfg, rd))
        stats = await crawler.run(crawl_roles, budget_minutes=budget, approved_only=approved_only)
        stats.warnings = warnings
        return stats
    finally:
        await rt.recorder.drain()
        for name, sess in sessions.items():
            try:
                await sess.context.close()
                rt.recorder.finalize_har(name)
            except Exception as exc:
                log.warning("closing role %s failed: %s", name, exc)
        rt.queue.write_yaml()
        rt.store.close()
