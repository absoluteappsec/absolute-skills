"""Passive discovery: robots.txt, sitemaps and well-known documents."""
from __future__ import annotations

import html
import json
import re
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

from ..direct import DirectClient
from ..frontier import Frontier
from ..normalize import normalize_path
from ..store import Store
from ._common import base_origins, expired, with_source

MAX_SITEMAP_DEPTH = 3
_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S | re.I)
_CDATA = re.compile(r"^<!\[CDATA\[(.*)\]\]>$", re.S)
_ROBOTS_LINE = re.compile(r"^\s*(allow|disallow|sitemap)\s*:\s*(.*)$", re.I)
_WELL_KNOWN = ("/.well-known/security.txt", "/.well-known/openid-configuration", "/manifest.json")


def _robots(text: str) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    sitemaps: list[str] = []
    for line in text.splitlines():
        m = _ROBOTS_LINE.match(line.split("#", 1)[0])
        if not m:
            continue
        kind, value = m.group(1).lower(), m.group(2).strip()
        if kind == "sitemap":
            if value and value not in sitemaps:
                sitemaps.append(value)
            continue
        value = value.split("*", 1)[0].rstrip("$")
        if value.startswith("/") and value not in paths:
            paths.append(value)
    return paths, sitemaps


def parse_robots(text: str) -> list[str]:
    """Allow/Disallow paths (wildcards cut at the first ``*``), then Sitemap URLs."""
    paths, sitemaps = _robots(text)
    return paths + sitemaps


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """``(urls, child_sitemaps)``; a sitemap index yields only children."""
    locs = []
    for raw in _LOC.findall(xml or ""):
        m = _CDATA.match(raw)
        locs.append((m.group(1).strip() if m else html.unescape(raw)).strip())
    if re.search(r"<sitemapindex", xml or "", re.I):
        return [], locs
    return locs, []


_DJANGO_URLCONF = re.compile(r"Using the URLconf defined in.*?<ol>(.*?)</ol>", re.S | re.I)
_LI = re.compile(r"<li\b[^>]*>(.*?)</li>", re.S | re.I)
_CODE = re.compile(r"<code\b[^>]*>(.*?)</code>", re.S | re.I)
_DJ_NAME = re.compile(r"\[name=['\"][^'\"]*['\"]\]")
_DJ_GROUP = re.compile(r"\(\?P<(\w+)>(?:[^()]|\([^()]*\))*\)")
_DJ_CONVERTER = re.compile(r"<(?:\w+:)?(\w+)>")
_DJ_ESCAPED = re.compile(r"\\([./_-])")
_DJ_REGEX_LEFT = re.compile(r"[\\()\[\]*+?|<>]")


def _django_pattern(raw: str) -> str | None:
    p = html.unescape(re.sub(r"<[^>]+>", "", raw))
    p = _DJ_NAME.sub("", p).replace("^", "").replace("$", "")
    p = re.sub(r"\s+", "", p)
    p = _DJ_GROUP.sub(lambda m: "{%s}" % m.group(1), p)
    p = _DJ_CONVERTER.sub(lambda m: "{%s}" % m.group(1), p)
    p = _DJ_ESCAPED.sub(r"\1", p).replace("\\Z", "")
    if _DJ_REGEX_LEFT.search(p):
        return None  # unnamed groups, alternations, character classes: no usable path
    return "/" + p.lstrip("/")


def parse_django_urlconf(text: str) -> list[str]:
    """Route patterns listed on a Django DEBUG 404 page ("Using the URLconf defined in
    ..."), one per list item, prefixes joined; regex groups and path converters become
    ``{name}``. Patterns that still contain regex syntax are dropped."""
    m = _DJANGO_URLCONF.search(text or "")
    if not m:
        return []
    out: list[str] = []
    for li in _LI.findall(m.group(1)):
        p = _django_pattern("".join(_CODE.findall(li)))
        if p and p not in out:
            out.append(p)
    return out


def _json_urls(obj, out: list[str]) -> None:
    """URL-looking string values (and manifest-style url/start_url fields) anywhere."""
    if isinstance(obj, dict):
        for v in obj.values():
            _json_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _json_urls(v, out)
    elif isinstance(obj, str) and (obj.startswith(("http://", "https://")) or obj.startswith("/")):
        out.append(obj)


def _well_known_urls(path: str, text: str, base: str) -> list[str]:
    found: list[str] = []
    if path.endswith("security.txt"):
        for line in text.splitlines():
            k, _, v = line.partition(":")
            if k.strip().lower() in ("contact", "policy", "acknowledgments", "canonical",
                                     "encryption", "hiring") and v.strip().startswith("http"):
                found.append(v.strip())
    else:
        try:
            _json_urls(json.loads(text), found)
        except ValueError:
            pass
    return [urljoin(base + "/", u) for u in found]


async def probe_passive(client: DirectClient, api, cfg, store: Store, frontier: Frontier,
                        role: str, *, deadline: float | None = None) -> dict[str, int]:
    """Fetch passive sources as ``role``; queue every in-scope URL for every role."""
    counts = {"requests": 0, "routes": 0, "queued": 0, "timed_out": 0}
    scope = client.gate.scope
    seen: set[str] = set()
    known = [r["pattern"] for r in store.conn.execute(
        "SELECT pattern FROM routes WHERE kind='spa_route'")]

    def add(url: str, source: str) -> None:
        url = urldefrag(url)[0]
        if not scope.allows(url):
            return
        parts = urlsplit(url)
        rid = store.upsert_route(normalize_path(parts.path or "/", known), parts.netloc.lower())
        store.upsert_source("route", rid, source)
        if (url, source) not in seen:
            seen.add((url, source))
            counts["routes"] += 1
        for r in cfg.roles:
            if frontier.push(r.name, "url", url=url, depth=0, priority=0, source=source):
                counts["queued"] += 1

    async def fetch(url: str, source: str) -> str | None:
        if expired(deadline):
            counts["timed_out"] = 1
            return None
        got = await with_source(client, source, attribute="ok_nonhtml").request(
            api, "GET", url, role=role, origin="passive")
        counts["requests"] += 1
        if got is None or got[0] != 200:
            return None
        if "html" in (got[1].headers.get("content-type") or "").lower():
            return None  # a catch-all SPA's index.html, not the document asked for
        return await got[1].text()

    for base in base_origins(cfg):
        text = await fetch(base + "/robots.txt", "robots")
        sitemaps = [base + "/sitemap.xml"]
        if text:
            paths, declared = _robots(text)
            for p in paths:
                add(urljoin(base + "/", p), "robots")
            sitemaps += [s for s in declared if s not in sitemaps]
        queue = [(s, 0) for s in sitemaps]
        done: set[str] = set()
        while queue and not counts["timed_out"]:
            url, depth = queue.pop(0)
            if url in done:
                continue
            done.add(url)
            body = await fetch(url, "sitemap")
            if not body:
                continue
            urls, children = parse_sitemap(body)
            for u in urls:
                add(u, "sitemap")
            if depth < MAX_SITEMAP_DEPTH:
                queue += [(c, depth + 1) for c in children]
        for path in _WELL_KNOWN:
            body = await fetch(base + path, "well_known")
            if body:
                for u in _well_known_urls(path, body, base):
                    add(u, "well_known")
        if counts["timed_out"]:
            break
    _django_debug_routes(store, cfg, frontier, scope, known, counts, seen)
    return counts


def _django_debug_routes(store: Store, cfg, frontier: Frontier, scope, known: list[str],
                         counts: dict[str, int], seen: set) -> None:
    """Routes listed by any recorded Django DEBUG 404 page (often robots.txt's own 404).
    Recorded with source ``well_known``; concrete paths are queued for every role."""
    for row in store.conn.execute(
            "SELECT url, resp_body, resp_body_path FROM exchanges WHERE status=404 AND "
            "(resp_body LIKE '%Using the URLconf defined in%' OR "
            "(resp_body IS NULL AND resp_body_path IS NOT NULL AND content_type='text/html'))").fetchall():
        text = row["resp_body"]
        if text is None:
            try:
                text = (Path(store.db_path).parent / row["resp_body_path"]).read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                continue
        parts = urlsplit(row["url"])
        base = f"{parts.scheme}://{parts.netloc}"
        for pattern in parse_django_urlconf(text):
            url = base + pattern
            if not scope.allows(url):
                continue
            rid = store.upsert_route(normalize_path(pattern, known), parts.netloc.lower())
            store.upsert_source("route", rid, "well_known")
            if (url, "django") not in seen:
                seen.add((url, "django"))
                counts["routes"] += 1
            if "{" in pattern:
                continue  # a pattern, not a URL the crawler can visit
            for r in cfg.roles:
                if frontier.push(r.name, "url", url=url, depth=0, priority=0,
                                 source="well_known"):
                    counts["queued"] += 1
