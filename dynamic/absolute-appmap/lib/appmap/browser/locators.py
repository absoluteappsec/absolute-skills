"""Element locator extraction, ranking and resolution."""
from __future__ import annotations

from playwright.async_api import Locator, Page

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
    return None


async def resolve(page: Page, locators: list[dict]) -> Locator | None:
    """First strategy (by rank) that matches exactly one element."""
    for loc in sorted(locators, key=lambda x: x.get("rank", 0)):
        try:
            candidate = _locator(page, loc["strategy"], loc["value"])
            if candidate is not None and await candidate.count() == 1:
                return candidate
        except Exception:
            continue
    return None
