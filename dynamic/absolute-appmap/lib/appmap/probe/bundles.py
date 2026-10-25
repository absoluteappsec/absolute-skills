"""JS bundle and source-map mining: API paths, routes, GraphQL operations, absolute URLs."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlsplit

from ..direct import DirectClient
from ..frontier import Frontier
from ..normalize import normalize_path
from ..store import Store
from ._common import base_origins, expired, with_source

log = logging.getLogger("appmap.probe.bundles")

MAX_INPUT = 10 * 1024 * 1024
MAX_SCRIPTS = 200
MAX_PAGES = 200
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")

# Every pattern is linear: bounded character classes, no nested quantifiers.
_STRING = re.compile(r"\"([^\"\\\n]{1,300})\"|'([^'\\\n]{1,300})'|`([^`\\\n]{1,300})`")
_CALL = re.compile(
    r"(?:\bfetch\s*\(|\baxios\.(get|post|put|patch|delete)\s*\(|\baxios\s*\("
    r"|\.open\s*\(\s*[\"'](GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)[\"']\s*,)\s*"
    r"(?:\"([^\"\\\n]{1,300})\"|'([^'\\\n]{1,300})'|`([^`\\\n]{1,300})`)", re.I)
_METHOD_OPT = re.compile(r"method\s*:\s*[\"'`](\w{3,7})[\"'`]", re.I)
_API_PATH = re.compile(r"^/(?:api/|v\d+/|graphql)")
_ROUTE_OBJ = re.compile(r"\bpath\s*:\s*[\"']([^\"']{1,200})[\"']")
_ROUTE_JSX = re.compile(r"<Route\b[^>]{0,200}?\bpath\s*=\s*\{?\s*[\"']([^\"']{1,200})[\"']")
_GQL = re.compile(r"\b(query|mutation|subscription)\s+(\w{1,80})\s*[({]")
_SOURCE_MAP = re.compile(r"//[#@]\s*sourceMappingURL=(\S+)")
_ABS_URL = re.compile(r"https?://[^\s\"'`<>\\)\]}]{1,500}")
_TEMPLATE = re.compile(r"\$\{[^}]{0,200}\}")
_LEAD_TEMPLATE = re.compile(r"^\$\{[^}]{0,200}\}(?=/)")
_STATIC_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".css", ".js", ".mjs", ".map",
               ".woff", ".woff2", ".ttf", ".eot", ".webp", ".mp4", ".webm")
_BARE_PREFIX = re.compile(r"^/(?:api|v\d+)/*$")
_I18N_KEY = re.compile(r"^[A-Za-z_]+(?:\.[A-Za-z_]+){2,}$")
_FS_ROUTE = re.compile(r"^/(?:usr|bin|etc|home|var|tmp|opt)(?:/|$)|\.\w{1,5}$")
_PATHLIKE = re.compile(r"^[\w\-./~%:@{}!$&'()*+,;=?#\[\]]+$")
_SCRIPT_SRC = re.compile(r"<script\b[^>]{0,2000}?\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)


@dataclass
class BundleFindings:
    api_paths: set[str] = field(default_factory=set)
    routes: set[str] = field(default_factory=set)
    graphql_ops: set[tuple[str, str]] = field(default_factory=set)
    absolute_urls: set[str] = field(default_factory=set)
    source_maps: set[str] = field(default_factory=set)
    # path -> HTTP methods seen at a call site (paths without an entry are GET)
    methods: dict[str, set[str]] = field(default_factory=dict)
    # absolute URLs passed straight to fetch/axios (their methods live in ``methods``)
    call_urls: set[str] = field(default_factory=set)

    def update(self, other: "BundleFindings") -> None:
        self.api_paths |= other.api_paths
        self.routes |= other.routes
        self.graphql_ops |= other.graphql_ops
        self.absolute_urls |= other.absolute_urls
        self.source_maps |= other.source_maps
        self.call_urls |= other.call_urls
        for p, ms in other.methods.items():
            self.methods.setdefault(p, set()).update(ms)


def _clean_path(raw: str, follows_plus: bool) -> str | None:
    """Normalized relative API path from a literal, or None if it is not path-like."""
    s = _TEMPLATE.sub("{id}", _LEAD_TEMPLATE.sub("", raw.strip())).strip()
    if not s or s.startswith("//") or "://" in s or not _PATHLIKE.match(s):
        return None
    if not s.startswith("/"):
        s = "/" + s.lstrip("./")
    if s.endswith("/") and follows_plus and len(s) > 1:
        s += "{id}"  # "/api/items/" + id
    path = s.split("?", 1)[0].split("#", 1)[0]
    return path if _usable_path(path) else None


def _usable_path(path: str) -> bool:
    """Reject bare prefixes, static assets and dotted i18n keys."""
    if not path.strip("/") or _BARE_PREFIX.match(path) or path.lower().endswith(_STATIC_EXT):
        return False
    rest = re.sub(r"^/(?:api|v\d+)/", "/", path)[1:]
    return not ("/" not in rest and _I18N_KEY.match(rest))


def _route(raw: str) -> str | None:
    if not raw.startswith("/") or raw.startswith("//") or not _PATHLIKE.match(raw.replace(":", "")):
        return None
    if _FS_ROUTE.search(raw.split("?", 1)[0]):
        return None
    pat = re.sub(r"/:(\w+)\??", "/{\\1}", raw.split("?", 1)[0])
    return pat if pat == "/" else pat.rstrip("/")


def mine_bundle(js: str) -> BundleFindings:
    out = BundleFindings()
    js = (js or "")[:MAX_INPUT]

    def add_api(raw: str, end: int, method: str | None = None) -> None:
        path = _clean_path(raw, js[end:end + 4].lstrip().startswith("+"))
        if path is None:
            return
        out.api_paths.add(path)
        if method:
            out.methods.setdefault(path, set()).add(method.upper())

    for m in _STRING.finditer(js):
        raw = next(g for g in m.groups() if g is not None)
        if _API_PATH.match(raw):
            add_api(raw, m.end())
    for m in _CALL.finditer(js):
        verb, open_method, *lits = m.groups()
        raw = next((g for g in lits if g is not None), None)
        if raw is None:
            continue
        method = verb or open_method
        if not method and m.group(0).lstrip().startswith("fetch"):
            opt = _METHOD_OPT.search(js, m.end(), m.end() + 200)
            method = opt.group(1) if opt else None
        if method and method.upper() not in _HTTP_METHODS:
            method = None
        if raw.lower().startswith(("http://", "https://")):
            url = urldefrag(_TEMPLATE.sub("{id}", raw))[0]
            if _usable_path(urlsplit(url).path or "/"):
                out.call_urls.add(url)
                out.methods.setdefault(url, set()).add((method or "GET").upper())
        elif "/" in raw:
            add_api(raw, m.end(), method)
    for rx in (_ROUTE_OBJ, _ROUTE_JSX):
        for m in rx.finditer(js):
            r = _route(m.group(1))
            if r:
                out.routes.add(r)
    out.graphql_ops.update((m.group(1), m.group(2)) for m in _GQL.finditer(js))
    out.source_maps.update(m.group(1) for m in _SOURCE_MAP.finditer(js))
    for m in _ABS_URL.finditer(js):
        out.absolute_urls.add(m.group(0).rstrip(".,;:"))
    return out


# -- probe ------------------------------------------------------------------

def _read_stored(root: Path | None, body: str | None, path: str | None) -> str | None:
    if body:
        return body
    if path and root is not None:
        try:
            return (root / path).read_text(encoding="utf-8", errors="replace")[:MAX_INPUT]
        except OSError:
            return None
    return None


def _script_srcs(html: str, page_url: str) -> list[str]:
    return [urldefrag(urljoin(page_url, s.strip()))[0] for s in _SCRIPT_SRC.findall(html or "")
            if not s.strip().lower().startswith(("data:", "javascript:"))]


async def probe_bundles(client: DirectClient, api, cfg, store: Store, frontier: Frontier,
                        role: str, *, deadline: float | None = None) -> dict[str, int]:
    counts = {"requests": 0, "scripts": 0, "endpoints": 0, "routes": 0, "graphql_ops": 0,
              "external_links": 0, "queued": 0, "timed_out": 0}
    scope = client.gate.scope
    origins = base_origins(cfg)
    root = Path(store.db_path).parent if getattr(store, "db_path", None) else None
    known = [r["pattern"] for r in store.conn.execute(
        "SELECT pattern FROM routes WHERE kind='spa_route'")]
    q = store.conn.execute

    # -- collect script URLs ---------------------------------------------
    scripts: list[str] = []

    def add_script(u: str) -> None:
        if u.startswith(("http://", "https://")) and u not in scripts and len(scripts) < MAX_SCRIPTS:
            scripts.append(u)

    for (u,) in q("SELECT DISTINCT url FROM exchanges WHERE lower(content_type) LIKE '%javascript%'"
                  " AND url IS NOT NULL"):
        add_script(urldefrag(u)[0])
    seen_pages: set[str] = set()
    pages = list(q("SELECT url, resp_body, resp_body_path FROM exchanges WHERE "
                   "lower(content_type) LIKE '%html%' AND status BETWEEN 200 AND 299 "
                   "ORDER BY id DESC LIMIT ?", (MAX_PAGES,)))
    for row in pages:
        seen_pages.add(urldefrag(row["url"] or "")[0])
        for u in _script_srcs(_read_stored(root, row["resp_body"], row["resp_body_path"]) or "",
                              row["url"] or ""):
            add_script(u)
    if root is not None:
        for row in q("SELECT url, dom_snapshot FROM states WHERE dom_snapshot IS NOT NULL"):
            try:
                html = (root / row["dom_snapshot"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for u in _script_srcs(html, row["url"] or origins[0] + "/"):
                add_script(u)
    for base in cfg.base_urls:  # nothing crawled yet: read the entry pages themselves
        if urldefrag(base)[0] in seen_pages or expired(deadline):
            continue
        got = await with_source(client, "bundle", attribute=False).request(
            api, "GET", base, role=role, origin="passive")
        counts["requests"] += 1
        if got is not None and got[0] == 200:
            for u in _script_srcs(await got[1].text(), base):
                add_script(u)

    # -- fetch + mine -----------------------------------------------------
    async def body_of(url: str, source: str, origin: str) -> str | None:
        """Stored body when this URL was fetched before, else fetch it once."""
        row = q("SELECT resp_body, resp_body_path FROM exchanges WHERE url=? AND status=200 "
                "ORDER BY id DESC LIMIT 1", (url,)).fetchone()
        if row:
            return _read_stored(root, row["resp_body"], row["resp_body_path"])
        if q("SELECT 1 FROM exchanges WHERE url=? LIMIT 1", (url,)).fetchone():
            return None  # already tried (error / empty): do not re-fetch
        if expired(deadline):
            counts["timed_out"] = 1
            return None
        got = await with_source(client, source, attribute="ok").request(
            api, "GET", url, role=role, origin=origin)
        counts["requests"] += 1
        if got is None or got[0] != 200:
            return None
        return (await got[1].text())[:MAX_INPUT]

    def record(found: BundleFindings, source: str, script_url: str) -> None:
        hosts = [script_url] + origins
        graphql_urls: list[str] = []
        for path in sorted(found.api_paths):
            for base in dict.fromkeys(f"{urlsplit(h).scheme}://{urlsplit(h).netloc}" for h in hosts):
                url = base + path.split("?", 1)[0]
                if not scope.allows(url):
                    continue
                host = urlsplit(base).netloc.lower()
                pattern = normalize_path(path, known)
                if pattern.startswith("/graphql") or pattern.endswith("/graphql"):
                    graphql_urls.append(base + pattern)
                for method in sorted(found.methods.get(path) or {"GET"}):
                    eid = store.upsert_endpoint(method, host, pattern, "rest")
                    store.upsert_source("endpoint", eid, source)
                    counts["endpoints"] += 1
        for url in sorted(found.call_urls):
            parts = urlsplit(url)
            if scope.allows(url):
                pattern = normalize_path(parts.path or "/", known)
                for method in sorted(found.methods.get(url) or {"GET"}):
                    eid = store.upsert_endpoint(method, parts.netloc.lower(), pattern, "rest")
                    store.upsert_source("endpoint", eid, source)
                    counts["endpoints"] += 1
        for url in found.absolute_urls:
            url = urldefrag(url)[0]
            parts = urlsplit(url)
            if not scope.allows(url):
                store.upsert_external_link(url, script_url, role)
                counts["external_links"] += 1
            elif _API_PATH.match(parts.path or ""):
                pattern = normalize_path(parts.path, known)
                eid = store.upsert_endpoint("GET", parts.netloc.lower(), pattern, "rest")
                store.upsert_source("endpoint", eid, source)
                counts["endpoints"] += 1
        if found.graphql_ops:
            targets = graphql_urls or [o + "/graphql" for o in origins]
            for url in dict.fromkeys(targets):
                if not scope.allows(url):
                    continue
                parts = urlsplit(url)
                for kind, name in sorted(found.graphql_ops):
                    eid = store.upsert_endpoint("POST", parts.netloc.lower(), parts.path,
                                                "graphql_op", graphql_op=name, graphql_type=kind)
                    store.upsert_source("endpoint", eid, source)
                    counts["graphql_ops"] += 1
        for pattern in sorted(found.routes):
            for origin in origins:
                host = urlsplit(origin).netloc.lower()
                if not scope.allows(origin + pattern.replace("{", "").replace("}", "")):
                    continue
                rid = store.upsert_route(pattern, host, "spa_route")
                store.upsert_source("route", rid, source)
                counts["routes"] += 1
                if "{" in pattern or "*" in pattern:
                    continue
                for r in cfg.roles:
                    if frontier.push(r.name, "url", url=origin + pattern, depth=0, priority=0,
                                     source=source):
                        counts["queued"] += 1

    for url in list(scripts):
        if expired(deadline):
            counts["timed_out"] = 1
            break
        if not scope.allows(url):
            continue
        js = await body_of(url, "bundle", "bundle")
        if js is None:
            continue
        counts["scripts"] += 1
        found = mine_bundle(js)
        record(found, "bundle", url)
        maps = {urldefrag(urljoin(url, m))[0] for m in found.source_maps
                if not m.startswith("data:")} or {url.split("?", 1)[0] + ".map"}
        for map_url in sorted(maps):
            if not scope.allows(map_url):
                continue
            text = await body_of(map_url, "source_map", "passive")
            if not text:
                continue
            try:
                doc = json.loads(text)
            except ValueError:
                continue
            contents = doc.get("sourcesContent") if isinstance(doc, dict) else None
            mapped = BundleFindings()
            names = doc.get("sources") if isinstance(doc.get("sources"), list) else []
            for i, src in enumerate(contents if isinstance(contents, list) else []):
                name = str(names[i]) if i < len(names) else ""
                if "node_modules/" in name or "webpack/" in name:
                    continue  # third-party / bundler runtime sources
                if isinstance(src, str):
                    mapped.update(mine_bundle(src))
            record(mapped, "source_map", map_url)
    return counts
