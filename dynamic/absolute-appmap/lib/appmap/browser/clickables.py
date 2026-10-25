"""Discover clickable elements and expand collapsed UI."""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass

from playwright.async_api import Page

from ..destructive import ActionDescriptor, classify
import json

from .locators import ANCHOR_JS, LOCATOR_JS, _locator, rank_locators

log = logging.getLogger(__name__)

MAX_CANDIDATES = 2000
LISTENER_EVENTS = {"click", "mousedown", "pointerdown"}
_ROLES = "button|link|tab|menuitem|option"


@dataclass
class Clickable:
    locators: list[dict]
    tag: str
    role: str | None
    label: str
    href: str | None
    reason: str


_COLLECT_JS = """() => {
  const locate = (%(locator)s);
  const anchorOf = (%(anchor)s);
  const ROLES = new Set(%(roles)s);
  const SKIPTAG = new Set(['HTML', 'BODY', 'HEAD', 'SCRIPT', 'STYLE', 'META', 'LINK', 'SVG', 'PATH']);
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const list = [], els = [];
  let nReason = 0, nPlain = 0, truncated = false;
  window.__appmapCands = els;
  const all = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of all) {
    if (SKIPTAG.has(el.tagName.toUpperCase()) || el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if (!el.getClientRects().length) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden') continue;
    const role = el.getAttribute('role');
    let reason = '';
    if (el.tagName === 'A' && el.hasAttribute('href')) reason = 'anchor';
    else if (role && ROLES.has(role)) reason = 'role';
    else if (el.hasAttribute('onclick')) reason = 'onclick';
    else if (el.tagName === 'BUTTON' || el.tagName === 'SUMMARY' || el.tagName === 'SELECT'
             || (el.tagName === 'INPUT' && ['submit', 'button', 'reset', 'image'].includes(
                   (el.getAttribute('type') || '').toLowerCase()))) reason = 'native';
    else if (cs.cursor === 'pointer' && !(el.parentElement && getComputedStyle(el.parentElement).cursor === 'pointer')) reason = 'cursor';
    if (reason) { if (nReason >= %(cap)d) { truncated = true; continue; } nReason++; }
    else { if (nPlain >= %(cap)d) { truncated = true; continue; } nPlain++; }
    const raw = locate(el);
    const label = norm(el.getAttribute('aria-label')) || norm(el.innerText || el.textContent).slice(0, 80)
      || norm(el.getAttribute('title')) || (el.tagName === 'INPUT' ? norm(el.value) : '');
    els.push(el);
    list.push({reason, tag: el.tagName.toLowerCase(), role, label, raw,
               href: el.tagName === 'A' ? el.getAttribute('href') : null,
               anchor: /:nth-of-type\\(/.test(raw.css) ? anchorOf(el) : null});
  }
  return {items: list, truncated};
}""" % {"locator": LOCATOR_JS, "anchor": ANCHOR_JS, "roles": '["button","link","tab","menuitem","option"]', "cap": MAX_CANDIDATES}


async def _listener_indexes(page: Page, indexes: list[int]) -> set[int]:
    if not indexes:
        return set()
    cdp = await page.context.new_cdp_session(page)
    found: set[int] = set()
    try:
        for i in indexes:
            res = await cdp.send("Runtime.evaluate", {"expression": f"window.__appmapCands[{i}]"})
            oid = res.get("result", {}).get("objectId")
            if not oid:
                continue
            try:
                out = await cdp.send("DOMDebugger.getEventListeners", {"objectId": oid})
            except Exception:
                continue
            if any(l["type"] in LISTENER_EVENTS for l in out.get("listeners", [])):
                found.add(i)
    finally:
        await cdp.detach()
    return found


async def unique_locators(page: Page, raw: dict, dups: Counter) -> list[dict]:
    """Ranked locators, keeping a non-positional strategy only if it matches exactly one element."""
    out: list[dict] = []
    for loc in rank_locators(raw):
        if loc["strategy"] in ("css", "xpath"):
            out.append(loc)
        elif dups[(loc["strategy"], loc["value"])] == 1:
            try:
                if await _locator(page, loc["strategy"], loc["value"]).count() == 1:
                    out.append(loc)
            except Exception:
                pass
    return [{**l, "rank": i} for i, l in enumerate(out)]


async def enumerate_clickables(page: Page) -> list[Clickable]:
    res = await page.evaluate(_COLLECT_JS)
    cands = res["items"]
    if res["truncated"]:
        log.warning("enumerate_clickables: candidate cap (%d) reached on %s; list is incomplete",
                    MAX_CANDIDATES, page.url)
    # Listener-bearing elements are candidates only when not already explained by markup.
    with_listener = await _listener_indexes(page, list(range(len(cands))))
    await page.evaluate("() => { delete window.__appmapCands; }")
    chosen = []
    for i, c in enumerate(cands):
        reason = c["reason"]
        if i in with_listener and reason in ("", "cursor"):
            reason = "listener"
        if reason:
            chosen.append((c, reason))
    dups = Counter((l["strategy"], l["value"]) for c, _ in chosen for l in rank_locators(c["raw"]))
    out: list[Clickable] = []
    seen: set[tuple[str, str | None]] = set()
    for c, reason in chosen:
        locs = await unique_locators(page, c["raw"], dups)
        if not locs:
            continue
        key = (c["raw"]["xpath"], c["href"])
        if key in seen:
            continue
        seen.add(key)
        a = c.get("anchor")
        if a and (a.get("attr") or a.get("href") or a.get("row")) \
                and all(l["strategy"] in ("css", "xpath") for l in locs):
            # Only positional locators: pin the row it acts on (field fix 3).
            locs.append({"strategy": "anchor", "value": json.dumps(a, sort_keys=True),
                         "rank": len(locs)})
        out.append(Clickable(locs, c["tag"], c["role"] or None, c["label"], c["href"], reason))
    return out


_REVEAL_SELECTORS = ("[aria-expanded=false]", "[role=tab]:not([aria-selected=true])",
                     "details:not([open]) > summary")
_HOVER_SELECTOR = "nav a, nav [role=menuitem], [role=menubar] [role=menuitem]"


_INFO_JS = """e => {
  const n = (x) => (x || '').replace(/\\s+/g, ' ').trim();
  const img = e.querySelector('img[alt]');
  const label = n(e.innerText) || n(e.getAttribute('aria-label')) || n(e.getAttribute('title'))
    || (img && n(img.getAttribute('alt'))) || n(e.getAttribute('data-testid')) || n(e.id);
  const p = e.parentElement;
  // Nearby text only from a small local container, not page-wide wrappers.
  const ctx = p && !['BODY', 'MAIN', 'NAV', 'HEADER', 'FORM', 'SECTION'].includes(p.tagName)
    ? n(p.innerText) : '';
  return {label, href: e.tagName === 'A' ? e.getAttribute('href') : null,
          context: ctx.length <= 120 ? ctx : ''};
}"""


def _is_destructive(info: dict, profile: str = "staging") -> bool:
    a = ActionDescriptor("click", None, info.get("href"), info.get("label", ""),
                         info.get("context", ""), False, "", "ui")
    return classify(a, profile).held


async def _snapshot(page: Page) -> int:
    return await page.evaluate("() => document.getElementsByTagName('*').length")


async def expand_reveals(page: Page, profile: str = "staging") -> int:
    """Click collapsed controls and hover nav items; return how many revealed something."""
    revealed = 0
    for selector in _REVEAL_SELECTORS:
        handles = await page.locator(selector).element_handles()
        for h in handles:
            try:
                if not await h.is_visible():
                    continue
                if _is_destructive(await h.evaluate(_INFO_JS), profile):
                    continue
                before = await _snapshot(page)
                state = await h.evaluate(
                    "e => [e.getAttribute('aria-expanded'), e.getAttribute('aria-selected'),"
                    " e.parentElement && e.parentElement.open].join()")
                await h.click(timeout=2000)
                after = await h.evaluate(
                    "e => [e.getAttribute('aria-expanded'), e.getAttribute('aria-selected'),"
                    " e.parentElement && e.parentElement.open].join()")
                if after != state or await _snapshot(page) != before:
                    revealed += 1
            except Exception:
                continue
    for h in await page.locator(_HOVER_SELECTOR).element_handles():
        try:
            if not await h.is_visible():
                continue
            if _is_destructive(await h.evaluate(_INFO_JS), profile):
                continue
            before = await _snapshot(page)
            await h.hover(timeout=1000)
            if await _snapshot(page) != before:
                revealed += 1
        except Exception:
            continue
    return revealed
