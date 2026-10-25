"""Extract client-side route patterns from a loaded SPA, and track client navigation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Page

from ..normalize import normalize_path
from .detect import detect_spa

# Installed as a context init script: records every client-side route change.
HISTORY_PATCH_JS = """(() => {
  if (window.__appmap_nav) return;
  window.__appmap_nav = [];
  const rec = (type) => window.__appmap_nav.push({type, url: location.href});
  for (const m of ['pushState', 'replaceState']) {
    const orig = history[m];
    history[m] = function () {
      const r = orig.apply(this, arguments);
      rec(m);
      return r;
    };
  }
  window.addEventListener('hashchange', () => rec('hashchange'));
  window.addEventListener('popstate', () => rec('popstate'));
})();"""

DRAIN_NAV_JS = """() => { const n = window.__appmap_nav || []; window.__appmap_nav = []; return n; }"""

_ROUTES_JS = """() => {
  const out = [];
  let router = null;
  for (const el of document.querySelectorAll('body *')) {
    const a = el.__vue_app__;
    if (a && a.config.globalProperties.$router) { router = a.config.globalProperties.$router; break; }
  }
  if (!router && window.$nuxt && window.$nuxt.$router) router = window.$nuxt.$router;
  if (router && router.getRoutes) for (const r of router.getRoutes()) out.push(r.path);
  for (const k of Object.keys(window)) {
    if (!k.startsWith('__sveltekit')) continue;
    const v = window[k];
    const rs = v && (v.routes || (v.manifest && v.manifest.routes));
    if (Array.isArray(rs)) for (const r of rs) out.push(typeof r === 'string' ? r : (r && r.id) || '');
  }
  return {paths: out, buildId: (window.__NEXT_DATA__ || {}).buildId || null};
}"""

_MODIFIERS = re.compile(r":(\w+)(\([^)]*\))?[?+*]?")
_NEXT_SKIP = {"/_app", "/_error", "/_document"}


@dataclass(frozen=True)
class SpaRoute:
    pattern: str
    source: str


def _normalize_route(path: str) -> str | None:
    """Route-config path -> ``{param}`` pattern; None for catch-alls and empties."""
    if not path or "*" in path or "(.*)" in path or path.startswith("/:pathMatch"):
        return None
    path = _MODIFIERS.sub(r":\1", path)
    path = re.sub(r"\[\[?\.{0,3}(\w+)\]?\]", r"[\1]", path)  # [[...slug]] / [...slug]
    if not path.startswith("/"):
        path = "/" + path
    return normalize_path(path, [path])


def parse_next_build_manifest(text: str) -> list[str]:
    """Page patterns (``{param}`` form) from a Next.js ``_buildManifest.js`` body."""
    keys = re.findall(r'"(/[^"]*)"\s*:\s*\[', text)
    m = re.search(r"sortedPages\s*:\s*\[(.*?)\]\s*[,}]", text)
    if m:
        keys += re.findall(r'"(/[^"]*)"', m.group(1))
    out: list[str] = []
    for k in keys:
        if k in _NEXT_SKIP or k.startswith("/_"):
            continue
        n = _normalize_route(k)
        if n and n not in out:
            out.append(n)
    return out


async def extract_routes(page: Page, base_url: str,
                         fetch_text: Callable[[str], Awaitable[str | None]]) -> list[SpaRoute]:
    """Route patterns the loaded app's router exposes at runtime. React Router and Angular
    have no runtime hook: [] (bundle mining covers them)."""
    framework = (await detect_spa(page))["framework"]
    if framework in ("react", "angular", "unknown"):
        return []
    try:
        data = await page.evaluate(_ROUTES_JS)
    except Exception:
        return []
    patterns: list[str] = []
    for p in data["paths"]:
        n = _normalize_route(p)
        if n and n not in patterns:
            patterns.append(n)
    if framework == "next" and data.get("buildId"):
        url = urljoin(base_url, f"/_next/static/{data['buildId']}/_buildManifest.js")
        text = await fetch_text(url)
        for n in parse_next_build_manifest(text or ""):
            if n not in patterns:
                patterns.append(n)
    return [SpaRoute(p, "spa_router") for p in patterns]


# -- concrete URLs for parameterized routes --------------------------------------
_PARAM_SEG = re.compile(r"^\{\w+\}$")


def _json_ids(body: str | None) -> list[str]:
    """``id`` values of a JSON body: the object itself, or objects in any top-level list."""
    try:
        data = json.loads(body or "")
    except ValueError:
        return []
    objs = [data] if isinstance(data, dict) else []
    if isinstance(data, list):
        objs += data
    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                objs += v
    return [str(o["id"]) for o in objs
            if isinstance(o, dict) and isinstance(o.get("id"), (str, int))
            and not isinstance(o.get("id"), bool) and re.fullmatch(r"[\w.~-]+", str(o["id"]))]


def _param_index(pattern: str) -> tuple[list[str], int | None]:
    """Segments of ``pattern`` and the index of its single parameter (None if not one)."""
    segs = [s for s in pattern.strip("/").split("/") if s]
    idx = [i for i, s in enumerate(segs) if _PARAM_SEG.match(s)]
    return segs, (idx[0] if len(idx) == 1 else None)


def _json_ids_near(rows, root: Path | None, anchor: str | None, ids: list[str],
                   limit: int) -> list[str]:
    """Append (to ``ids``) the ``id`` fields of JSON responses in ``rows`` whose URL path
    contains ``anchor``; bodies stored on disk are read from ``root``."""
    for r in rows:
        if len(ids) >= limit:
            break
        if "json" not in (r["content_type"] or "").lower():
            continue
        if anchor and anchor not in urlsplit(r["url"]).path.split("/"):
            continue
        body = r["resp_body"]
        if not body and root is not None and r["resp_body_path"]:
            try:
                body = (root / r["resp_body_path"]).read_text(encoding="utf-8")
            except OSError:
                body = None
        for v in _json_ids(body):
            if v not in ids:
                ids.append(v)
    return ids[:limit]


_ROWS = ("SELECT url, resp_body, resp_body_path, content_type FROM exchanges "
         "WHERE role=? AND status BETWEEN 200 AND 299 AND ")


def observed_ids(conn, root: Path | None, role: str, pattern: str, limit: int) -> list[str]:
    """IDs a role has seen for a one-parameter ``pattern``: URL segments of exchanges that
    already match it first, then ``id`` fields of the role's JSON responses whose URL
    mentions the segment before the parameter. Empty if the pattern has no single parameter."""
    segs, i = _param_index(pattern)
    if i is None:
        return []
    ids: list[str] = []
    rows = conn.execute(_ROWS + "upper(method)='GET' ORDER BY id", (role,)).fetchall()
    for r in rows:  # ids in URLs that already match the pattern
        path = [s for s in urlsplit(r["url"]).path.strip("/").split("/") if s]
        if len(path) == len(segs) and all(
                j == i or path[j] == segs[j] for j in range(len(segs))):
            if path[i] not in ids and not path[i].startswith("{"):
                ids.append(path[i])
    return _json_ids_near(rows, root, segs[i - 1] if i else None, ids, limit)


def created_ids(conn, root: Path | None, role: str, pattern: str, limit: int) -> list[str]:
    """``id`` fields of the role's successful non-GET JSON responses (records its own
    writes created) whose URL mentions the segment before ``pattern``'s one parameter."""
    segs, i = _param_index(pattern)
    if i is None:
        return []
    rows = conn.execute(_ROWS + "upper(method) NOT IN ('GET','HEAD','OPTIONS') ORDER BY id",
                        (role,))
    return _json_ids_near(rows, root, segs[i - 1] if i else None, [], limit)


def concrete_url(base_url: str, pattern: str, value: str) -> str:
    """``pattern`` with its parameter segment replaced by ``value``, joined to ``base_url``."""
    path = "/" + "/".join(value if _PARAM_SEG.match(s) else s
                          for s in pattern.strip("/").split("/") if s)
    return urljoin(base_url, path)
