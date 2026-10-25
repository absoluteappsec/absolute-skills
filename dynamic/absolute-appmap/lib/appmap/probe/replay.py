"""Active probing: API replay with variations, forced browsing, and cross-role replay.

Every request goes through ``DirectClient`` (scope, throttle, breaker, redirect gating) and
is classified with ``destructive.classify`` first; held requests are queued for approval
instead of being sent. Requests are sent as a role through that role's browser context
(``context.request`` shares its cookies) plus the auth headers the recorder learned while
the role's app made its own authenticated requests (bearer tokens kept in web storage).
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import re
import secrets
import time
import weakref
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from playwright.async_api import Browser, async_playwright

from ..approvals import ApprovalQueue, PendingAction
from ..browser.session import (MAX_RELOGINS, RoleSession, auth_probe_urls, is_session_lost,
                               login, open_role_session, state_path)
from ..config import RunConfig
from ..destructive import ActionDescriptor, Flag, classify
from ..direct import DirectClient
from ..frontier import Frontier
from ..normalize import normalize_path
from ..recording import STATIC_EXTS, body_shape, record_exchange
from ..rundir import RunDir
from ..spa.routes import concrete_url, created_ids, observed_ids
from ..store import Store
from ._common import expired, with_source

log = logging.getLogger("appmap.probe.replay")

_SKIP_KINDS = {"static", "websocket", "sse"}  # assets and streams: nothing to learn
_META_METHODS = ("OPTIONS", "HEAD")
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
_PAGINATION = {"page": "2", "offset": "10", "limit": "5"}
_MAX_ID_VARIANTS = 3
_LOGIN_SEG = re.compile(r"^(?:log-?in|sign[-_]?in|signin)(?:\.\w+)?$", re.I)
_LOGIN_PATH = re.compile(r"(?:^|/)(?:log-?in|sign[-_]?in)(?:/|$|\.)", re.I)
_REDACTED = "<redacted:"
# Captured traffic api_replay varies (its own replay/schema/cross_role output is not).
# Exchanges of approved (destructive) actions are never used: see approval_blocked_ids.
_CAPTURED = ("ui", "passive", "bundle")
PROGRESS_KEY = "probe_active"
WARM_UP_TIMEOUT_MS = 10000
_GUARDED: "weakref.WeakSet" = weakref.WeakSet()


# -- outcome classification ----------------------------------------------------
def _path_of(url: str) -> str:
    return urlsplit(url).path.rstrip("/") or "/"


def _is_login_url(url: str, login_url: str | None) -> bool:
    if not url:
        return False
    if login_url:
        a, b = urlsplit(url), urlsplit(login_url)
        if b.netloc and a.netloc.lower() != b.netloc.lower():
            return False
        return _path_of(url) == _path_of(login_url)
    return bool(_LOGIN_PATH.search(urlsplit(url).path or ""))


def classify_outcome(baseline_status: int, baseline_shape: dict | None, resp_status: int,
                     resp_url: str, resp_shape: dict | None, login_url: str | None,
                     *, requested_url: str | None = None) -> str:
    """Access outcome of one replay compared with the discovering role's exchange
    (ruling R20).

    ``resp_url`` is where the response ended up (the redirect target, or the requested
    URL). With ``requested_url``, landing on a different (non-login) path means the
    request was redirected away: ``different_content``. Pass ``login_url=None`` when the
    item itself is the login page; without a login URL, paths like ``/login`` or
    ``/users/sign_in`` count as the login page. 400/422 mean authorization passed and the
    input was rejected: ``different_content``."""
    if resp_status == 401 or _is_login_url(resp_url, login_url):
        return "redirect_login"
    if resp_status == 403:
        return "forbidden"
    if resp_status in (404, 410):
        return "not_found"
    if resp_status <= 0 or resp_status >= 500 or resp_status == 405:
        return "error"
    if 300 <= resp_status < 400 or resp_status in (400, 422):
        return "different_content"
    if requested_url and resp_url and _path_of(resp_url) != _path_of(requested_url):
        return "different_content"
    if 200 <= resp_status < 300:
        if 200 <= (baseline_status or 0) < 300 and baseline_shape is not None \
                and resp_shape != baseline_shape:
            return "different_content"
        return "allowed"
    return "error"


def run_login_url(cfg: RunConfig) -> str | None:
    for r in cfg.roles:
        if r.login.type in ("form", "manual") and r.login.url:
            return r.login.url
    return None


def login_endpoint_ids(store: Store, cfg: RunConfig) -> set[int]:
    """Endpoints never replayed: login-named paths, non-GETs to a role's login URL, and
    endpoints whose only traffic is a login flow sending data."""
    login_paths = {_path_of(r.login.url) for r in cfg.roles
                   if r.login.url and r.login.type in ("form", "manual")}
    ids: set[int] = set()
    for row in store.conn.execute("SELECT id, method, path_pattern FROM endpoints"):
        last = (row["path_pattern"].rstrip("/").rsplit("/", 1) or [""])[-1]
        if _LOGIN_SEG.match(last) or (row["method"].upper() not in _SAFE_METHODS
                                      and (row["path_pattern"].rstrip("/") or "/") in login_paths):
            ids.add(row["id"])
    for (eid,) in store.conn.execute(
            "SELECT endpoint_id FROM exchanges WHERE endpoint_id IS NOT NULL GROUP BY endpoint_id "
            "HAVING sum(origin='login' AND upper(method) NOT IN ('GET','HEAD','OPTIONS')) > 0 "
            "AND sum(origin!='login') = 0"):
        ids.add(eid)
    return ids


def _approvals(store: Store) -> list[tuple[str, str, str, str]]:
    """(METHOD, host, path, text) of every approval with a predicted request URL."""
    out = []
    for row in store.conn.execute("SELECT label, action_path, predicted_request FROM approvals"):
        try:
            pr = json.loads(row["predicted_request"]) if row["predicted_request"] else {}
        except ValueError:
            pr = {}
        url = (pr or {}).get("url")
        if not url:
            continue
        parts = urlsplit(url)
        method = (pr.get("method") or "GET").upper()
        text = f"{row['label'] or ''} {row['action_path'] or ''}"
        out.append((method, parts.netloc.lower(), parts.path or "/", text))
        try:
            steps = json.loads(row["action_path"]) if row["action_path"] else []
        except ValueError:
            steps = []
        if method not in ("GET", "POST") and any(
                isinstance(st, dict) and st.get("type") == "submit" for st in steps or []):
            # a form's hidden _method override (Rails/Laravel): the wire request is a POST
            out.append(("POST", parts.netloc.lower(), parts.path or "/", text))
    return out


def _approval_matches(approvals, method: str, host: str, pattern: str,
                      graphql_op: str = "", graphql_type: str | None = None) -> bool:
    for a_method, a_host, a_path, text in approvals:
        if a_method != method.upper() or a_host != host:
            continue
        if normalize_path(a_path, [pattern]) != pattern:
            continue
        # A GraphQL URL hosts many operations: block the named one, and every
        # non-query operation when the approval does not say which one it was.
        if graphql_op and graphql_op not in text and (graphql_type or "query") == "query":
            continue
        return True
    return False


def approval_blocked_ids(store: Store) -> set[int]:
    """Endpoints never replayed (ruling R20): captured with origin ``approved``, or
    referenced by any approval row (whatever its decision). Replay re-classifies by
    method/URL/body only and would lose the crawl's label/confirm/context signals."""
    ids = {eid for (eid,) in store.conn.execute(
        "SELECT DISTINCT endpoint_id FROM exchanges WHERE origin='approved' "
        "AND endpoint_id IS NOT NULL")}
    approvals = _approvals(store)
    if approvals:
        for ep in store.conn.execute("SELECT * FROM endpoints"):
            if _approval_matches(approvals, ep["method"], ep["host"], ep["path_pattern"],
                                 ep["graphql_op"], ep["graphql_type"]):
                ids.add(ep["id"])
    return ids


# -- helpers ---------------------------------------------------------------------
def _header(raw: str | None, name: str) -> str | None:
    try:
        h = json.loads(raw) if raw else {}
    except ValueError:
        return None
    for k, v in (h or {}).items():
        if k.lower() == name:
            return str(v)
    return None


def _shape_of(body: bytes | None, ctype: str | None) -> Any:
    if not body or "json" not in (ctype or "").lower():
        return None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return body_shape(obj) if isinstance(obj, (dict, list)) else None


def _generated(types: list[str], marker: str) -> Any:
    t = (types or ["string"])[0]
    if t in ("int", "integer", "number", "float"):
        return 1
    if t in ("bool", "boolean"):
        return True
    return marker


@dataclass
class _Req:
    method: str
    url: str
    body: bytes | None = None
    ctype: str | None = None
    label: str = ""      # classification label (GraphQL operation name)
    context: str = ""    # classification context (GraphQL document)

    @property
    def usable(self) -> bool:
        return _REDACTED not in self.url and _REDACTED.encode() not in (self.body or b"")


@dataclass
class _Sent:
    status: int
    final_url: str
    shape: Any
    exchange_id: int | None
    denied: bool = False  # 401/login redirect while the session was confirmed alive


@dataclass
class ActiveResult:
    counts: dict[str, int]
    warnings: list[str] = field(default_factory=list)
    pending_approvals: int = 0


class _Prober:
    """Shared plumbing: classification + approval queueing, sending as a role (with one
    re-login on a lost session), stored-exchange lookups and progress counters."""

    def __init__(self, client: DirectClient, sessions: dict[str, RoleSession], cfg: RunConfig,
                 store: Store, queue: ApprovalQueue, deadline: float | None):
        self.client, self.sessions, self.cfg = client, sessions, cfg
        self.store, self.queue, self.deadline = store, queue, deadline
        self.root = Path(store.db_path).parent
        self.login_url = run_login_url(cfg)
        self.counts: dict[str, int] = {"requests": 0, "held": 0, "skipped": 0, "timed_out": 0}
        self.relogins: dict[str, int] = {}
        self.genuine_redirects: set[str] = set()  # roles whose login redirects are real

    # -- bookkeeping -------------------------------------------------------
    def alive(self) -> bool:
        if expired(self.deadline):
            self.counts["timed_out"] = 1
            return False
        return True

    def warn(self, role: str, msg: str) -> None:
        self.counts["skipped"] += 1
        log.warning("[%s] %s", role, msg)
        if self.client.events is not None:
            self.client.events.emit("error", role=role, message=msg)

    def _q(self, sql: str, *args):
        return self.store.conn.execute(sql, args)

    def scheme_for(self, host: str) -> str:
        for b in self.cfg.base_urls:
            p = urlsplit(b)
            if p.netloc.lower() == host:
                return p.scheme
        return urlsplit(self.cfg.base_urls[0]).scheme

    def stored_body(self, text: str | None, path: str | None) -> bytes | None:
        if text is not None:
            return text.encode("utf-8")
        if path:
            try:
                return (self.root / path).read_bytes()
            except OSError:
                return None
        return None

    def request_of(self, ex, kind: str = "rest", op: str = "") -> _Req:
        body = self.stored_body(ex["req_body"], ex["req_body_path"])
        ctype = _header(ex["req_headers"], "content-type")
        ctx = body.decode("utf-8", "replace") if body else ""  # classify sees the body
        return _Req(ex["method"].upper(), ex["url"], body or None, ctype if body else None,
                    op, ctx)

    def rep_exchange(self, endpoint_id: int, role: str | None = None,
                     origins: tuple[str, ...] | None = None):
        """Representative exchange: 2xx first, then UI traffic, then oldest."""
        sql = ("SELECT * FROM exchanges WHERE endpoint_id=? "
               "AND origin NOT IN ('login','cross_role','approved')")
        args: list = [endpoint_id]
        if role is not None:
            sql += " AND role=?"
            args.append(role)
        if origins:
            sql += f" AND origin IN ({','.join('?' * len(origins))})"
            args += list(origins)
        sql += (" ORDER BY (status BETWEEN 200 AND 299) DESC, (origin='ui') DESC, id ASC "
                "LIMIT 1")
        return self._q(sql, *args).fetchone()

    def already_sent(self, role: str, method: str, url: str, origins: tuple[str, ...]) -> bool:
        return self._q(
            f"SELECT 1 FROM exchanges WHERE role=? AND method=? AND url=? AND origin IN "
            f"({','.join('?' * len(origins))}) LIMIT 1", role, method, url, *origins
        ).fetchone() is not None

    # -- classification ----------------------------------------------------
    def held(self, role: str, req: _Req, origin: str, *, enqueue: bool = True) -> bool:
        try:
            flag = classify(ActionDescriptor("request", req.method, req.url, req.label,
                                             req.context, False, role, origin), self.cfg.profile)
        except Exception:  # fail closed
            flag = Flag(True, "delete", ["request could not be classified"])
        if not flag.held:
            return False
        if not enqueue or flag.category == "logout":
            return True  # logout items are skipped outright, never queued (R20)
        step: dict[str, Any] = {"type": "request", "method": req.method, "url": req.url,
                                "origin": origin}
        if req.body is not None:
            step["body"] = req.body.decode("utf-8", "replace")
            step["content_type"] = req.ctype
        label = f"{req.method} {req.url}" + (f" [{req.label}]" if req.label else "")
        aid = self.queue.enqueue(PendingAction(
            id="", role=role, state_id=None, category=flag.category, label=label,
            reasons=list(flag.reasons), action_path=[step],
            predicted_request={"method": req.method, "url": req.url, "origin": origin}))
        self.counts["held"] += 1
        if self.client.events is not None:
            self.client.events.emit("approval_queued", role=role, approval_id=aid,
                                    category=flag.category, label=label, reasons=flag.reasons)
        log.info("[%s] held for approval %s (%s): %s", role, aid, flag.category, label)
        return True

    # -- sending -----------------------------------------------------------
    async def _fetch(self, role: str, req: _Req, origin: str,
                     client: DirectClient | None) -> _Sent | None:
        sess = self.sessions[role]
        mark = self._q("SELECT coalesce(max(id), 0) FROM exchanges").fetchone()[0]
        self.counts["requests"] += 1
        got = await (client or self.client).request(
            sess.context.request, req.method, req.url, role=role, origin=origin,
            headers={"Content-Type": req.ctype} if req.body is not None and req.ctype else None,
            data=req.body, extra_auth=dict(sess.auth_headers) or None)
        if got is None:
            return None
        status, resp = got
        first = self._q("SELECT id FROM exchanges WHERE id>? AND role=? AND origin=? "
                        "ORDER BY id LIMIT 1", mark, role, origin).fetchone()
        final = resp.url
        location = resp.headers.get("location")
        if 300 <= status < 400 and location:
            final = urljoin(resp.url, location)  # redirect not followed (out of scope)
        try:
            body = await resp.body()
        except Exception:
            body = None
        return _Sent(status, final, _shape_of(body, resp.headers.get("content-type")),
                     first[0] if first else None)

    def _lost(self, role: str, sent: _Sent, req: _Req) -> bool:
        sess = self.sessions[role]
        if sess.role.login.type == "none" or role in self.genuine_redirects:
            return False
        return sent.status == 401 or (_is_login_url(sent.final_url, self.login_url)
                                      and not _is_login_url(req.url, self.login_url))

    async def _session_alive(self, role: str, req: _Req) -> bool | None:
        """Confirm a candidate loss with a GET that needs the session (``auth_probe_urls``,
        sent as origin ``login``: auth bookkeeping, never a representative). True: alive
        (the 401 meant "not yours"); False: lost; None: nothing to probe with."""
        sess = self.sessions[role]
        urls = auth_probe_urls(sess, self.store, self.login_url, exclude=req.url,
                               prefer_evidence=True)
        for url in urls:
            sent = await self._fetch(role, _Req("GET", url), "login", None)
            if sent is None:
                continue
            alive = not (sent.status == 401 or _is_login_url(sent.final_url, self.login_url))
            log.info("[%s] session liveness probe %s: %s", role, url,
                     "alive" if alive else "lost")
            return alive
        return None

    def _denied(self, role: str, sent: _Sent, req: _Req) -> _Sent:
        sent.denied = True
        if self.client.events is not None:
            ev = self.client.events.emit("observation", role=role, kind="access_denied",
                                         url=req.url, status=sent.status,
                                         detail="denied with a live session")
            self.store.add_observation("access_denied", "denied with a live session",
                                       req.url, role, ev.id)
        return sent

    async def _relogin(self, role: str) -> bool:
        sess, events = self.sessions[role], self.client.events
        if sess.recorder is None or events is None or sess.role.login.type == "manual" \
                or self.relogins.get(role, 0) >= MAX_RELOGINS:
            return False
        self.relogins[role] = self.relogins.get(role, 0) + 1
        events.emit("session_lost", role=role, url=self.login_url or "",
                    status=sess.recorder.last_status.get(role))
        sess.auth_headers.clear()
        ok = await login(sess, self.cfg, events)
        if ok:
            await warm_up(sess, self.cfg)
        log.info("[%s] re-login after session loss: %s", role, "ok" if ok else "FAILED")
        return ok

    async def fetch(self, role: str, req: _Req, origin: str,
                    client: DirectClient | None = None) -> _Sent | None:
        """Send ``req`` as ``role``; on an apparent session loss re-login once and retry.
        If the retry is redirected to login again, the redirect is the real answer."""
        sent = await self._fetch(role, req, origin, client)
        if sent is None or not self._lost(role, sent, req):
            return sent
        # A 401 / login redirect is only a candidate loss: "not yours" answers 401 too.
        alive = await self._session_alive(role, req)
        if alive:
            return self._denied(role, sent, req)
        if not await self._relogin(role):
            return sent
        sent = await self._fetch(role, req, origin, client)
        if sent is not None and self._lost(role, sent, req):
            if alive is None:  # nothing to probe with: the redirect is the real answer
                self.genuine_redirects.add(role)
            elif await self._session_alive(role, req):
                return self._denied(role, sent, req)  # re-logged in, still denied
        return sent

    async def send_once(self, role: str, req: _Req, origin: str, *, enqueue: bool = True,
                        client: DirectClient | None = None) -> _Sent | None:
        """Classify, then send unless held or already sent by an earlier run."""
        if not req.usable:
            self.warn(role, f"{req.method} {req.url}: captured request holds redacted "
                            "credentials; not replayed")
            return None
        if self.already_sent(role, req.method, req.url, ("replay", "schema")):
            return None
        if self.held(role, req, origin, enqueue=enqueue) or not self.alive():
            return None
        return await self.fetch(role, req, origin, client)


# -- warm-up: learn a role's auth headers ------------------------------------------
async def _install_guard(sess: RoleSession, cfg: RunConfig) -> None:
    """Abort held non-GET requests the app fires while a warm-up page loads (a login
    flow's own data_create submit in production still goes through)."""
    if sess.context in _GUARDED:
        return
    _GUARDED.add(sess.context)
    role, rec = sess.role.name, sess.recorder

    async def guard(route, request) -> None:
        if request.method in _SAFE_METHODS:
            await route.fallback()
            return
        try:
            flag = classify(ActionDescriptor("request", request.method, request.url, "",
                                             (request.post_data or "")[:4096], False, role,
                                             "ui"), cfg.profile)
        except Exception:
            flag = Flag(True, "delete", ["request could not be classified"])
        from_login = False
        try:
            from_login = rec is not None and request.frame.page in rec.login_pages
        except Exception:
            pass
        if flag.held and not (flag.category == "data_create" and from_login):
            await route.abort("blockedbyclient")
            msg = (f"warm-up: held {request.method} request aborted ({flag.category}); "
                   "not queued (the crawl covers page loads)")
            log.info("[%s] %s: %s", role, msg, request.url)
            if rec is not None:
                rec.events.emit("error", role=role, url=request.url, message=msg)
            return
        await route.fallback()

    await sess.context.route("**/*", guard)


async def warm_up(sess: RoleSession, cfg: RunConfig) -> bool:
    """Load the first base URL in the role's browser so the app sends its authenticated
    requests (the recorder learns bearer/CSRF headers). False if the session looks lost."""
    rec = sess.recorder
    if rec is None:
        return True
    await _install_guard(sess, cfg)
    page = await rec.new_page(sess.context)
    try:
        try:
            await page.goto(cfg.base_urls[0], wait_until="networkidle",
                            timeout=WARM_UP_TIMEOUT_MS)
        except Exception:
            pass  # slow or never idle: whatever was sent so far is enough
        return not await is_session_lost(sess, page, rec.last_status.get(sess.role.name),
                                         sess.role.login.url)
    finally:
        await page.close()
        await rec.drain()


# -- API replay ------------------------------------------------------------------
def _with_query(parts, pairs: list[tuple[str, str]]) -> str:
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), ""))


def _get_variations(p: _Prober, ep, rep, role: str) -> list[str]:
    parts = urlsplit(rep["url"])
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    names = list(dict.fromkeys(k for k, _ in pairs))
    q_params = {r["name"]: r["required_evidence"] for r in p._q(
        "SELECT name, required_evidence FROM params WHERE endpoint_id=? AND location='query'",
        ep["id"])}
    out: list[str] = []
    for n in names:  # each optional query param omitted once
        if q_params.get(n) != "schema":
            out.append(_with_query(parts, [(k, v) for k, v in pairs if k != n]))
    for name, value in _PAGINATION.items():
        if name in names:
            out.append(_with_query(parts, [(k, value if k == name else v) for k, v in pairs]))
        elif name in q_params:
            out.append(_with_query(parts, pairs + [(name, value)]))
    segs = [s for s in ep["path_pattern"].split("/") if s]
    idx = [i for i, s in enumerate(segs) if s.startswith("{") and s.endswith("}")]
    path = [s for s in parts.path.split("/") if s]
    if len(idx) == 1 and len(path) == len(segs):
        i = idx[0]
        # records the role's own writes created come first: they exist only because of
        # this run, and replaying them links the write to its read (value_links)
        seen = created_ids(p.store.conn, p.root, role, ep["path_pattern"], 50)
        for r in [role] + [n for n in p.sessions if n != role]:
            seen += observed_ids(p.store.conn, p.root, r, ep["path_pattern"], 50)
        ids: list[str] = []
        for v in seen:
            if v != path[i] and v not in ids and _REDACTED not in v:
                ids.append(v)
        for v in ids[:_MAX_ID_VARIANTS]:
            new_path = "/" + "/".join(v if j == i else s for j, s in enumerate(path))
            out.append(urlunsplit((parts.scheme, parts.netloc, new_path, parts.query, "")))
    return [u for u in dict.fromkeys(out) if u != rep["url"]]


def _bound(client: DirectClient, pattern: str) -> DirectClient:
    """A client whose exchanges attribute to ``pattern`` (named path params kept)."""
    return DirectClient(client.gate, functools.partial(client.rec, known_patterns=[pattern]),
                        client.events)


def _example(row, marker: str) -> Any:
    try:
        examples = json.loads(row["examples"]) if row["examples"] else []
        types = json.loads(row["types"]) if row["types"] else []
    except ValueError:
        examples, types = [], []
    for e in examples:
        if e is not None and _REDACTED not in str(e):
            return e
    return _generated(types, marker)


def _schema_request(p: _Prober, ep, marker: str) -> _Req | None:
    host, pattern = ep["host"], ep["path_pattern"]
    params = list(p._q("SELECT * FROM params WHERE endpoint_id=?", ep["id"]))
    by_loc: dict[str, list] = {}
    for r in params:
        by_loc.setdefault(r["location"], []).append(r)
    path_vals = {r["name"]: r for r in by_loc.get("path", [])}
    segs = [s for s in pattern.split("/") if s]
    holes = [s for s in segs if s.startswith("{") and s.endswith("}")]
    ids: list[str] = []
    if len(holes) == 1:
        for role in p.sessions:
            ids += observed_ids(p.store.conn, p.root, role, pattern, 1)
    out = []
    for s in segs:
        if s.startswith("{") and s.endswith("}"):
            name = s[1:-1]
            if name in path_vals:
                out.append(str(_example(path_vals[name], marker)))
            else:
                out.append(ids[0] if ids else "1")
        else:
            out.append(s)
    url = f"{p.scheme_for(host)}://{host}/" + "/".join(out)
    query = [(r["name"], str(_example(r, marker))) for r in by_loc.get("query", [])
             if r["required_evidence"] == "schema"]
    if query:
        url += "?" + urlencode(query)
    method = ep["method"].upper()
    if ep["kind"] == "graphql_op":
        gtype, op = ep["graphql_type"] or "query", ep["graphql_op"]
        if gtype == "subscription" or not op:
            return None
        doc = f"{gtype} {op} {{ __typename }}"  # names the operation, selects nothing
        return _Req("POST", url, json.dumps({"query": doc}).encode(), "application/json",
                    op, doc)
    if method == "GET":
        return _Req("GET", url)
    body = json.dumps({r["name"]: _example(r, marker) for r in by_loc.get("body", [])})
    return _Req(method, url, body.encode(), "application/json", context=body)


async def api_replay(client: DirectClient, sessions: dict[str, RoleSession], cfg: RunConfig,
                     store: Store, queue: ApprovalQueue, role: str, *,
                     deadline: float | None = None, marker: str = "appmap",
                     exercise: bool = True) -> dict[str, int]:
    """Vary ``role``'s captured traffic; with ``exercise``, also request endpoints that
    have no exchanges at all (schema/bundle only). Pass ``exercise`` for one role per run
    so held schema-only requests are queued once, not once per role."""
    p = _Prober(client, sessions, cfg, store, queue, deadline)
    skip = login_endpoint_ids(store, cfg) | approval_blocked_ids(store)
    for ep in list(store.conn.execute("SELECT * FROM endpoints ORDER BY id")):
        if not p.alive():
            break
        if ep["kind"] in _SKIP_KINDS or ep["id"] in skip or ep["method"] in _META_METHODS:
            continue
        rep = p.rep_exchange(ep["id"], role, _CAPTURED)
        if rep is not None:
            if _REDACTED in rep["url"]:
                p.warn(role, f"{rep['url']}: captured URL holds redacted credentials; "
                             "not replayed")
                continue
            parts = urlsplit(rep["url"])
            bare = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
            for m in _META_METHODS:
                await p.send_once(role, _Req(m, bare), "replay", enqueue=False,
                                  client=with_source(client, "replay", attribute="ok"))
            if ep["method"] == "GET":
                for url in _get_variations(p, ep, rep, role):
                    if not p.alive():
                        break
                    await p.send_once(role, _Req("GET", url), "replay")
            else:
                await p.send_once(role, p.request_of(rep, ep["kind"], ep["graphql_op"]),
                                  "replay")
        elif exercise and p._q("SELECT 1 FROM exchanges WHERE endpoint_id=? LIMIT 1",
                               ep["id"]).fetchone() is None:
            req = _schema_request(p, ep, marker)
            if req is not None:
                await p.send_once(role, req, "schema", client=_bound(client, ep["path_pattern"]))
    return p.counts


# -- forced browsing ---------------------------------------------------------------
def load_wordlist(cfg: RunConfig) -> list[str]:
    texts = [resources.files("appmap").joinpath("resources/wordlist.txt")
             .read_text(encoding="utf-8")]
    for raw in cfg.discovery.wordlists:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = cfg.source_path.parent / path
        texts.append(path.read_text(encoding="utf-8"))
    words: list[str] = []
    for text in texts:
        for line in text.splitlines():
            w = line.strip()
            if w and not w.startswith("#") and w not in words:
                words.append(w)
    return words


def _digest(body: bytes | None) -> str:
    return hashlib.sha256(body or b"").hexdigest()


async def forced_browse(client: DirectClient, api, cfg: RunConfig, store: Store,
                        frontier: Frontier, role: str, *, deadline: float | None = None,
                        extra_auth: dict | None = None) -> dict[str, int]:
    """GET every wordlist path under every base URL (only when enabled). Results other
    than 404 (or the site's soft-404 page) become routes/endpoints with source wordlist
    and are queued for every role."""
    counts = {"requests": 0, "wordlist_found": 0, "queued": 0, "timed_out": 0}
    if not cfg.discovery.forced_browsing:
        return counts
    if cfg.profile == "production":  # config validation rejects this too (defence in depth)
        log.warning("forced browsing refused: not allowed with profile production")
        return counts
    fb = with_source(client, "wordlist", attribute=False)
    scope = client.gate.scope

    async def get(url: str):
        counts["requests"] += 1
        got = await fb.request(api, "GET", url, role=role, origin="passive",
                               extra_auth=extra_auth)
        if got is None:
            return None
        try:
            body = await got[1].body()
        except Exception:
            body = None
        return got[0], got[1].headers.get("content-type", ""), body

    words = load_wordlist(cfg)
    for base in cfg.base_urls:
        base = base.rstrip("/") + "/"
        soft: tuple[int, str] | None = None
        todo = [w for w in words
                if store.conn.execute("SELECT 1 FROM exchanges WHERE url=? AND role=? LIMIT 1",
                                      (base + w.lstrip("/"), role)).fetchone() is None]
        if not todo:
            continue
        if expired(deadline):
            counts["timed_out"] = 1
            break
        probe = await get(base + f"appmap-not-found-{secrets.token_hex(6)}")
        if probe is not None and probe[0] != 404:
            soft = (probe[0], _digest(probe[2]))
        for w in todo:
            if expired(deadline):
                counts["timed_out"] = 1
                return counts
            url = base + w.lstrip("/")
            if not scope.allows(url) or classify(ActionDescriptor(
                    "request", "GET", url, "", "", False, role, "passive"), cfg.profile).held:
                continue
            got = await get(url)
            if got is None or got[0] == 404 or (soft and (got[0], _digest(got[2])) == soft):
                continue
            parts = urlsplit(url)
            host, pattern = parts.netloc.lower(), normalize_path(parts.path or "/")
            if "html" in got[1].lower():
                rid = store.upsert_route(pattern, host, "page")
                store.upsert_source("route", rid, "wordlist")
            else:
                kind = "static" if parts.path.lower().endswith(STATIC_EXTS) else "rest"
                eid = store.upsert_endpoint("GET", host, pattern, kind)
                store.upsert_source("endpoint", eid, "wordlist")
            counts["wordlist_found"] += 1
            for r in cfg.roles:
                if frontier.push(r.name, "url", url=url, depth=0, priority=0, source="wordlist"):
                    counts["queued"] += 1
    return counts


# -- cross-role replay ----------------------------------------------------------------
class _Matrix:
    def __init__(self, p: _Prober, roles: list[str]):
        self.p, self.roles = p, roles
        self.cache: dict[tuple, tuple[str, int | None]] = {}
        p.counts["role_access"] = 0

    def has_outcome(self, kind: str, item_id: int, role: str) -> bool:
        """A definitive result exists (held/untestable and failed requests are retried)."""
        return self.p._q("SELECT 1 FROM role_access WHERE item_kind=? AND item_id=? AND role=? "
                         "AND outcome NOT IN ('not_tested','error')", kind, item_id,
                         role).fetchone() is not None

    def write(self, kind: str, item_id: int, role: str, outcome: str,
              evidence: int | None) -> None:
        self.p.store.upsert_role_access(kind, item_id, role, outcome, evidence)
        self.p.counts["role_access"] += 1

    def untested(self, kind: str, item_id: int) -> None:
        for r in self.roles:
            if self.p._q("SELECT 1 FROM role_access WHERE item_kind=? AND item_id=? AND role=?",
                         kind, item_id, r).fetchone() is None:
                self.write(kind, item_id, r, "not_tested", None)

    @staticmethod
    def own_outcome(rep, login: str | None) -> str:
        """The discovering role's outcome from its representative exchange: ``allowed``
        for 2xx, otherwise what that exchange itself shows (e.g. a 403 wordlist hit)."""
        status = rep["status"] or 0
        if 200 <= status < 300:
            return "allowed"
        landed = rep["url"]
        location = _header(rep["resp_headers"], "location")
        if 300 <= status < 400 and location:
            landed = urljoin(rep["url"], location)
        return classify_outcome(0, None, status, landed, None, login,
                                requested_url=rep["url"])

    async def item(self, kind: str, item_id: int, req: _Req, rep, *,
                   blocked: bool = False) -> None:
        """Fill ``role_access`` for one item. ``blocked`` items (approval-referenced) are
        never sent: every role but the discovering one gets ``not_tested``."""
        p = self.p
        base_status = rep["status"] if rep is not None else 0
        base_shape = None
        if rep is not None:
            base_shape = _shape_of(p.stored_body(rep["resp_body"], rep["resp_body_path"]),
                                   rep["content_type"])
        login = None if _is_login_url(req.url, p.login_url) else p.login_url
        for role in self.roles:
            if self.has_outcome(kind, item_id, role):
                continue
            if rep is not None and role == rep["role"]:  # its own exchange is the evidence
                self.write(kind, item_id, role, self.own_outcome(rep, login), rep["id"])
                continue
            if blocked or role not in p.sessions:
                self.write(kind, item_id, role, "not_tested", None)
                continue
            if not req.usable:
                p.warn(role, f"{req.method} {req.url}: captured request holds redacted "
                             "credentials; cross-role replay not tested")
                self.write(kind, item_id, role, "not_tested", None)
                continue
            key = (role, req.method, req.url, req.body)
            if key not in self.cache:
                if not p.alive():
                    return
                if p.held(role, req, "cross_role"):
                    self.cache[key] = ("not_tested", None)
                else:
                    sent = await p.fetch(role, req, "cross_role")
                    self.cache[key] = (("error", None) if sent is None else (
                        "forbidden" if sent.denied else
                        classify_outcome(base_status, base_shape, sent.status, sent.final_url,
                                         sent.shape, login, requested_url=req.url),
                        sent.exchange_id))
            self.write(kind, item_id, role, *self.cache[key])


def _get_url(p: _Prober, host: str, pattern: str) -> str | None:
    """A concrete URL for a GET of ``pattern``: a crawled state's URL, the pattern itself
    when it has no parameters, or the pattern filled with an observed ID."""
    for (url,) in p._q("SELECT url FROM states WHERE route_pattern=? ORDER BY rowid", pattern):
        if url and urlsplit(url).netloc.lower() == host:
            return url
    base = f"{p.scheme_for(host)}://{host}/"
    if "{" not in pattern:
        return base.rstrip("/") + pattern
    for role in p.sessions:
        ids = observed_ids(p.store.conn, p.root, role, pattern, 1)
        if ids:
            return concrete_url(base, pattern, ids[0])
    return None


def _route_target(p: _Prober, route) -> tuple[str | None, Any, int | None]:
    """(URL to fetch, representative exchange or None, page endpoint id or None)."""
    host, pattern = route["host"], route["pattern"]
    ep = p._q("SELECT id FROM endpoints WHERE method='GET' AND host=? AND path_pattern=? "
              "AND graphql_op=''", host, pattern).fetchone()
    eid = ep[0] if ep else None
    if eid is not None:
        rep = p.rep_exchange(eid)
        if rep is not None:
            return rep["url"], rep, eid
    return _get_url(p, host, pattern), None, eid


async def cross_role_replay(client: DirectClient, sessions: dict[str, RoleSession],
                            cfg: RunConfig, store: Store, queue: ApprovalQueue, *,
                            deadline: float | None = None) -> dict[str, int]:
    """Request every endpoint's representative exchange and every route as every other
    role and record the ``role_access`` matrix (spec decision 10 for writes)."""
    p = _Prober(client, sessions, cfg, store, queue, deadline)
    m = _Matrix(p, [r.name for r in cfg.roles])
    skip = login_endpoint_ids(store, cfg)
    blocked = approval_blocked_ids(store)
    approvals = _approvals(store)
    for ep in list(store.conn.execute("SELECT * FROM endpoints ORDER BY id")):
        if not p.alive():
            return p.counts
        if ep["kind"] in _SKIP_KINDS or ep["method"] in _META_METHODS or ep["id"] in skip:
            continue
        rep = p.rep_exchange(ep["id"])
        if rep is None:
            # Only login-flow traffic (or none): a plain GET is still a fresh request.
            url = _get_url(p, ep["host"], ep["path_pattern"]) \
                if ep["method"] == "GET" and ep["kind"] in ("page", "rest") else None
            if url is None or ep["id"] in blocked:
                m.untested("endpoint", ep["id"])
            else:
                await m.item("endpoint", ep["id"], _Req("GET", url), None)
            continue
        await m.item("endpoint", ep["id"], p.request_of(rep, ep["kind"], ep["graphql_op"]),
                     rep, blocked=ep["id"] in blocked)
    for route in list(store.conn.execute("SELECT * FROM routes ORDER BY id")):
        if not p.alive():
            return p.counts
        last = route["pattern"].rstrip("/").rsplit("/", 1)[-1]
        if _LOGIN_SEG.match(last):
            continue
        url, rep, eid = _route_target(p, route)
        is_blocked = eid in blocked or _approval_matches(approvals, "GET", route["host"],
                                                         route["pattern"])
        if url is None or (is_blocked and rep is None):
            m.untested("route", route["id"])
            continue
        await m.item("route", route["id"], _Req("GET", url), rep, blocked=is_blocked)
    return p.counts


# -- orchestration ---------------------------------------------------------------------
def _merge(total: dict[str, int], part: dict[str, int]) -> None:
    for k, v in part.items():
        total[k] = max(total.get(k, 0), v) if k == "timed_out" else total.get(k, 0) + v


async def run_active(cfg: RunConfig, rd: RunDir, *, roles: list[str] | None = None,
                     probe_role: str | None = None, budget_minutes: float,
                     browser: Browser | None = None) -> ActiveResult:
    """The ``probe --phase active`` pass: API replay (per role), forced browsing (if
    enabled), then cross-role replay last so it covers everything found."""
    if browser is None:
        async with async_playwright() as pw:
            b = await pw.chromium.launch()
            try:
                return await _run_active(cfg, rd, roles, probe_role, budget_minutes, b)
            finally:
                await b.close()
    return await _run_active(cfg, rd, roles, probe_role, budget_minutes, browser)


async def _open_sessions(browser: Browser, cfg: RunConfig, rd: RunDir, rt, deadline: float,
                         opened: dict[str, RoleSession], warnings: list[str]) -> dict:
    sessions: dict[str, RoleSession] = {}
    for role in cfg.roles:
        if expired(deadline):
            break
        sess = await open_role_session(browser, cfg, rd, role, rt.recorder)
        opened[role.name] = sess
        if role.login.type == "manual" and not state_path(rd, role.name).exists():
            warnings.append(f"role {role.name}: no saved session; run 'appmap auth "
                            f"--manual-login --role {role.name}' first")
            continue
        if role.login.type != "none":
            ok = state_path(rd, role.name).exists() or await login(sess, cfg, rt.events)
            if ok and not await warm_up(sess, cfg):  # saved session expired
                if role.login.type == "manual":
                    warnings.append(f"role {role.name}: saved session looks expired; run "
                                    f"'appmap auth --manual-login --role {role.name}'")
                else:
                    sess.auth_headers.clear()
                    ok = await login(sess, cfg, rt.events) and await warm_up(sess, cfg)
            if not ok:
                warnings.append(f"role {role.name}: login failed; not probed")
                log.warning("role %s: login failed; not probed", role.name)
                continue
        sessions[role.name] = sess
    return sessions


async def _run_active(cfg: RunConfig, rd: RunDir, roles: list[str] | None,
                      probe_role: str | None, budget_minutes: float,
                      browser: Browser) -> ActiveResult:
    from ..crawl import build_runtime  # crawl imports the browser stack; keep it lazy

    deadline = time.monotonic() + budget_minutes * 60
    rt = build_runtime(cfg, rd)
    result = ActiveResult({"requests": 0, "held": 0, "skipped": 0, "role_access": 0,
                           "wordlist_found": 0, "timed_out": 0})
    counts = result.counts
    if rd.approvals_path.exists():
        rt.queue.load_decisions()
        result.warnings += [f"approvals.yaml: {w}" for w in rt.queue.warnings]
    rec = functools.partial(record_exchange, rt.store, rt.events, rt.redactor, rd,
                            known_patterns=rt.recorder.known_patterns)
    client = DirectClient(rt.gate, rec, rt.events)
    marker = cfg.form_data.marker_prefix.replace("{run_id}", rd.run_id)
    probe_role = probe_role or cfg.roles[0].name
    opened: dict[str, RoleSession] = {}
    try:
        sessions = await _open_sessions(browser, cfg, rd, rt, deadline, opened, result.warnings)
        if expired(deadline):
            counts["timed_out"] = 1
        steps = []
        if cfg.discovery.api_replay:
            replay_roles = [n for n in roles or [r.name for r in cfg.roles] if n in sessions]
            exerciser = probe_role if probe_role in replay_roles else next(
                iter(replay_roles), None)
            for name in replay_roles:
                steps.append(functools.partial(api_replay, client, sessions, cfg, rt.store,
                                               rt.queue, name, deadline=deadline,
                                               marker=marker, exercise=name == exerciser))
        if cfg.discovery.forced_browsing and probe_role in sessions:
            s = sessions[probe_role]
            steps.append(functools.partial(forced_browse, client, s.context.request, cfg,
                                           rt.store, Frontier(rt.store), probe_role,
                                           deadline=deadline,
                                           extra_auth=s.auth_headers or None))
        steps.append(functools.partial(cross_role_replay, client, sessions, cfg, rt.store,
                                       rt.queue, deadline=deadline))
        for step in steps:
            if counts["timed_out"]:
                break
            _merge(counts, await step())
        if counts["skipped"]:
            result.warnings.append(f"{counts['skipped']} request(s) not replayed: captured "
                                   "requests hold redacted credentials (see crawl.log)")
        rt.store.set_meta(PROGRESS_KEY, json.dumps(
            {"status": "in_progress" if counts["timed_out"] else "done",
             "updated": time.time(), "counts": counts}))
    finally:
        await rt.recorder.drain()
        for name, sess in opened.items():
            try:
                await sess.context.close()
                rt.recorder.finalize_har(name)
            except Exception as exc:
                log.warning("closing role %s failed: %s", name, exc)
        rt.queue.write_yaml()
        result.pending_approvals = int(rt.store.conn.execute(
            "SELECT count(*) FROM approvals WHERE decision='pending'").fetchone()[0])
        rt.store.close()
    return result
