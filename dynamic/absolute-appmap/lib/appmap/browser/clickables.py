"""Discover clickable elements and expand collapsed UI."""
from __future__ import annotations

from dataclasses import dataclass

from playwright.async_api import Page

from ..destructive import ActionDescriptor, classify
from .locators import LOCATOR_JS, rank_locators

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
  const ROLES = new Set(%(roles)s);
  const SKIPTAG = new Set(['HTML', 'BODY', 'HEAD', 'SCRIPT', 'STYLE', 'META', 'LINK', 'SVG', 'PATH']);
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const list = [], els = [];
  window.__appmapCands = els;
  const all = document.body ? document.body.querySelectorAll('*') : [];
  for (const el of all) {
    if (list.length >= %(cap)d) break;
    if (SKIPTAG.has(el.tagName.toUpperCase()) || el.closest('svg') && el.tagName.toLowerCase() !== 'svg') continue;
    if (!el.getClientRects().length) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden') continue;
    const role = el.getAttribute('role');
    let reason = '';
    if (el.tagName === 'A' && el.hasAttribute('href')) reason = 'anchor';
    else if (role && ROLES.has(role)) reason = 'role';
    else if (el.hasAttribute('onclick')) reason = 'onclick';
    else if (cs.cursor === 'pointer' && !(el.parentElement && getComputedStyle(el.parentElement).cursor === 'pointer')) reason = 'cursor';
    const raw = locate(el);
    const label = norm(el.getAttribute('aria-label')) || norm(el.innerText || el.textContent).slice(0, 80)
      || norm(el.getAttribute('title')) || (el.tagName === 'INPUT' ? norm(el.value) : '');
    els.push(el);
    list.push({reason, tag: el.tagName.toLowerCase(), role, label, raw,
               href: el.tagName === 'A' ? el.getAttribute('href') : null});
  }
  return list;
}""" % {"locator": LOCATOR_JS, "roles": '["button","link","tab","menuitem","option"]', "cap": MAX_CANDIDATES}


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


async def enumerate_clickables(page: Page) -> list[Clickable]:
    cands = await page.evaluate(_COLLECT_JS)
    # Listener-bearing elements are candidates only when not already explained by markup.
    with_listener = await _listener_indexes(page, list(range(len(cands))))
    out: list[Clickable] = []
    seen: set[tuple[str, str]] = set()
    for i, c in enumerate(cands):
        reason = c["reason"]
        if i in with_listener and reason in ("", "cursor"):
            reason = "listener"
        if not reason:
            continue
        locs = rank_locators(c["raw"])
        if not locs:
            continue
        key = (locs[0]["strategy"], locs[0]["value"])
        if key in seen:
            continue
        seen.add(key)
        out.append(Clickable(locs, c["tag"], c["role"] or None, c["label"], c["href"], reason))
    return out


_REVEAL_SELECTORS = ("[aria-expanded=false]", "[role=tab]:not([aria-selected=true])",
                     "details:not([open]) > summary")
_HOVER_SELECTOR = "nav a, nav [role=menuitem], [role=menubar] [role=menuitem]"


async def _is_destructive(label: str) -> bool:
    flag = classify(ActionDescriptor("click", None, None, label, "", False, "", "ui"), "staging")
    return flag.held


async def _snapshot(page: Page) -> int:
    return await page.evaluate("() => document.getElementsByTagName('*').length")


async def expand_reveals(page: Page) -> int:
    """Click collapsed controls and hover nav items; return how many revealed something."""
    revealed = 0
    for selector in _REVEAL_SELECTORS:
        handles = await page.locator(selector).element_handles()
        for h in handles:
            try:
                if not await h.is_visible():
                    continue
                label = (await h.inner_text()).strip() or (await h.get_attribute("aria-label") or "")
                if await _is_destructive(label):
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
            label = (await h.inner_text()).strip()
            if await _is_destructive(label):
                continue
            before = await _snapshot(page)
            await h.hover(timeout=1000)
            if await _snapshot(page) != before:
                revealed += 1
        except Exception:
            continue
    return revealed
