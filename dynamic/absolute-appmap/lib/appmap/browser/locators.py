"""Element locator extraction, ranking and resolution."""
from __future__ import annotations

import json

from playwright.async_api import Locator, Page

# Content anchor of a list-row control (field fix 3): what identifies *which* row it acts
# on when its only unique locators are positional. ``attr`` is a ``data-*id``-style
# attribute (name, value) of the element or its row, ``href`` its link/formaction,
# ``row`` the row's text, ``tag`` its tag name.
ANCHOR_JS = """(el) => {
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const idAttr = (n) => {
    if (!n || !n.attributes) return null;
    for (const a of n.attributes)
      if (/^data-(?:[\\w-]*-)?(?:id|pk|key|uuid)$/i.test(a.name) && a.value) return [a.name, a.value];
    return null;
  };
  const row = el.closest('li,tr,article,[role=row],[role=listitem],[data-id]');
  const attr = idAttr(el) || idAttr(row) || null;
  const href = el.getAttribute('href') || el.getAttribute('formaction')
    || (el.form && el.form.getAttribute('action')) || '';
  return {tag: el.tagName.toLowerCase(), attr, href: href.startsWith('#') ? '' : href,
          row: row ? norm(row.innerText).slice(0, 160) : ''};
}"""

# Elements of the anchor's tag whose anchor matches; returns their xpaths.
_FIND_ANCHOR_JS = """([want, anchorOf]) => {
  const fn = eval('(' + anchorOf + ')');
  const same = (a) => {
    if (want.attr || want.href) {
      if (want.attr && !(a.attr && a.attr[0] === want.attr[0] && a.attr[1] === want.attr[1]))
        return false;
      if (want.href && a.href !== want.href) return false;
      return true;
    }
    return !!want.row && a.row === want.row;
  };
  const xp = (el) => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const sibs = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [n];
      parts.unshift(n.tagName.toLowerCase() + '[' + (sibs.indexOf(n) + 1) + ']');
    }
    return '/' + parts.join('/');
  };
  return [...document.getElementsByTagName(want.tag || '*')]
    .filter(el => el.getClientRects().length && same(fn(el))).map(xp);
}"""

LOCATOR_JS = """(el) => {
  const q = (s) => JSON.stringify(s);
  const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const testid = el.getAttribute('data-testid') || el.getAttribute('data-test')
    || el.getAttribute('data-cy') || '';
  const implicit = {A: el.hasAttribute('href') ? 'link' : '', BUTTON: 'button', SELECT: 'combobox',
    TEXTAREA: 'textbox', SUMMARY: 'button', H1: 'heading', H2: 'heading', H3: 'heading',
    H4: 'heading', H5: 'heading', H6: 'heading', NAV: 'navigation', LI: 'listitem'};
  let role = el.getAttribute('role') || implicit[el.tagName] || '';
  if (el.tagName === 'INPUT') {
    const t = (el.getAttribute('type') || 'text').toLowerCase();
    role = {button: 'button', submit: 'button', reset: 'button', checkbox: 'checkbox',
      radio: 'radio', range: 'slider', search: 'searchbox'}[t]
      || (['hidden', 'password', 'file', 'color', 'date'].includes(t) ? '' : 'textbox');
  }
  let label = '';
  if (el.labels && el.labels.length) label = norm(el.labels[0].textContent);
  const lb = el.getAttribute('aria-labelledby');
  if (!label && lb) {
    const n = document.getElementById(lb.split(/\\s+/)[0]);
    if (n) label = norm(n.textContent);
  }
  if (!label && ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))
    label = norm(el.getAttribute('aria-label'));
  const text = norm(el.innerText || el.textContent).slice(0, 80);
  const name = norm(el.getAttribute('aria-label')) || label || text
    || norm(el.getAttribute('title')) || norm(el.getAttribute('placeholder'))
    || norm(el.value && el.tagName === 'INPUT' ? el.value : '');
  const esc = (s) => (window.CSS && CSS.escape ? CSS.escape(s) : s);
  const cssPath = () => {
    if (el.id && document.querySelectorAll('#' + esc(el.id)).length === 1) return '#' + esc(el.id);
    const parts = [];
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      if (n.id && document.querySelectorAll('#' + esc(n.id)).length === 1) {
        parts.unshift('#' + esc(n.id)); break;
      }
      const tag = n.tagName.toLowerCase();
      const sibs = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [n];
      parts.unshift(sibs.length > 1 ? tag + ':nth-of-type(' + (sibs.indexOf(n) + 1) + ')' : tag);
    }
    return parts.join(' > ');
  };
  const xpath = () => {
    const parts = [];
    for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
      const tag = n.tagName.toLowerCase();
      const sibs = n.parentElement ? [...n.parentElement.children].filter(c => c.tagName === n.tagName) : [n];
      parts.unshift(tag + '[' + (sibs.indexOf(n) + 1) + ']');
    }
    return '/' + parts.join('/');
  };
  return {testid, role, name, label, text, css: cssPath(), xpath: xpath()};
}"""

_TESTID_CSS = '[data-testid={v}], [data-test={v}], [data-cy={v}]'


def rank_locators(raw: dict) -> list[dict]:
    candidates = [
        ("testid", raw.get("testid") or ""),
        ("role_name", f"{raw['role']}|{raw['name']}" if raw.get("role") and raw.get("name") else ""),
        ("label", raw.get("label") or ""),
        ("text", raw.get("text") or ""),
        ("css", raw.get("css") or ""),
        ("xpath", raw.get("xpath") or ""),
    ]
    kept = [(s, v) for s, v in candidates if v]
    return [{"strategy": s, "value": v, "rank": i} for i, (s, v) in enumerate(kept)]


def _locator(page: Page, strategy: str, value: str) -> Locator | None:
    if strategy == "testid":
        q = '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        return page.locator(_TESTID_CSS.format(v=q))
    if strategy == "role_name":
        role, _, name = value.partition("|")
        return page.get_by_role(role, name=name, exact=True)
    if strategy == "label":
        return page.get_by_label(value, exact=True)
    if strategy == "text":
        return page.get_by_text(value, exact=True)
    if strategy == "css":
        return page.locator(value)
    if strategy == "xpath":
        return page.locator("xpath=" + value)
    return None  # "anchor" is not a selector: resolve() checks positional hits against it


def anchor_of(locators: list[dict]) -> dict | None:
    for loc in locators or []:
        if loc.get("strategy") == "anchor":
            try:
                a = json.loads(loc["value"])
            except (TypeError, ValueError):
                return None
            return a if isinstance(a, dict) and (a.get("attr") or a.get("href")
                                                 or a.get("row")) else None
    return None


def _same_anchor(want: dict, got: dict) -> bool:
    if want.get("attr") or want.get("href"):
        if want.get("attr") and list(got.get("attr") or []) != list(want["attr"]):
            return False
        return not want.get("href") or got.get("href") == want["href"]
    return bool(want.get("row")) and got.get("row") == want["row"]


async def _by_anchor(page: Page, anchor: dict) -> Locator | None:
    try:
        hits = await page.evaluate(_FIND_ANCHOR_JS, [anchor, ANCHOR_JS])
    except Exception:
        return None
    return page.locator("xpath=" + hits[0]) if len(hits) == 1 else None


async def resolve(page: Page, locators: list[dict], *, strict: bool = False) -> Locator | None:
    """First strategy (by rank) that matches exactly one element.

    With a content anchor (field fix 3), a positional (css/xpath) hit must be the anchored
    element; otherwise the element is re-found by its anchor. ``strict`` (approved
    actions): no anchored element -> None, never the positional fallback."""
    anchor = anchor_of(locators)
    positional = None
    for loc in sorted(locators, key=lambda x: x.get("rank", 0)):
        if loc.get("strategy") == "anchor":
            continue
        try:
            candidate = _locator(page, loc["strategy"], loc["value"])
            if candidate is None or await candidate.count() != 1:
                continue
            if anchor is None or loc["strategy"] not in ("css", "xpath"):
                return candidate
            if _same_anchor(anchor, await candidate.evaluate(ANCHOR_JS)):
                return candidate
            positional = positional or candidate
        except Exception:
            continue
    if anchor is not None:
        found = await _by_anchor(page, anchor)
        if found is not None or strict:
            return found
    return positional
