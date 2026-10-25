"""Map building: flows, value links, preconditions, naming, appmap.json and APP-MAP.md.

Everything here is derived from the store (plus ``events.jsonl`` and ``run.json`` next to
it) and is idempotent: ``derive_flows`` rebuilds ``flows``/``flow_steps`` and
``derive_value_links`` rebuilds ``value_links`` from scratch on every call. Flow names and
form purposes written by the agent live in ``names.yaml`` and are re-applied by stable key
(``preconditions.flow_key``), never by autoincrement id.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, unquote, unquote_plus, urljoin, urlsplit, urlunsplit

import yaml

from appmap import SCHEMA_VERSION
from appmap.store import Store

__all__ = ["derive_flows", "derive_value_links", "derive_preconditions", "emit_naming_stub",
           "apply_names", "created_records", "export_json", "render_app_map", "build_map",
           "INTERNAL_TABLES", "is_internal_meta_key"]

# Crawl-internal tables: never exported, documented as internal in SCHEMA.md.
INTERNAL_TABLES = ("frontier", "frontier_links")
_INTERNAL_META_PREFIXES = ("approval_failed:", "approval_inflight:", "pass_")
_INTERNAL_META_KEYS = {"pass", "last_activity_ts", "probe_active"}

# TEXT columns holding json.dumps values; decoded in appmap.json.
JSON_COLUMNS = {
    "transitions": ("exchange_ids",),
    "endpoints": ("content_types",),
    "params": ("types", "examples"),
    "exchanges": ("req_headers", "resp_headers"),
    "responses": ("statuses", "content_types", "body_shape", "sample_exchange_ids"),
    "fields": ("constraints", "options", "values_tried", "validation_messages"),
    "flows": ("preconditions",),
    "approvals": ("reasons", "action_path", "predicted_request"),
}

_SAFE = {"GET", "HEAD", "OPTIONS"}
_MAX_LEAVES = 2000          # JSON leaves examined per producer response
_MAX_BODY_FILE = 2_000_000  # bodies/ files larger than this are not scanned for values
_TOKEN = re.compile(r"[\w.@~-]+")
_SKIP_HEADERS = {"cookie", "authorization", "proxy-authorization", "user-agent", "referer",
                 "origin", "host", "accept", "accept-encoding", "accept-language",
                 "content-type", "content-length", "connection", "upgrade-insecure-requests",
                 "cache-control", "pragma", "if-none-match", "if-modified-since"}
_COMMON_WORDS = {
    "true", "false", "null", "none", "undefined", "nan", "data", "item", "items", "name",
    "type", "user", "users", "admin", "error", "errors", "message", "success", "status",
    "result", "results", "value", "values", "text", "html", "json", "default", "general",
    "public", "private", "active", "inactive", "enabled", "disabled", "pending", "open",
    "closed", "list", "page", "next", "prev", "previous", "self", "link", "links", "href",
    "title", "description", "read", "write", "create", "update", "delete", "owner", "role",
    "roles", "info", "warning", "high", "medium", "low", "normal", "string", "number",
    "object", "array", "query", "mutation", "draft", "done", "test", "guest", "member",
}


# -- shared helpers ------------------------------------------------------------------
def _run_root(store: Store) -> Path:
    return store.db_path.parent


def _run_meta(store: Store) -> dict:
    try:
        meta = json.loads((_run_root(store) / "run.json").read_text(encoding="utf-8"))
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def run_marker(store: Store) -> str | None:
    """The run's data marker (``form_data.marker_prefix`` with ``{run_id}`` filled in)."""
    meta = _run_meta(store)
    run_id = meta.get("run_id")
    if not run_id:
        return None
    prefix = ((meta.get("config") or {}).get("form_data") or {}).get("marker_prefix") \
        or "appmap-{run_id}"
    return str(prefix).replace("{run_id}", str(run_id))


def is_internal_meta_key(key: str) -> bool:
    return key in _INTERNAL_META_KEYS or key.startswith(_INTERNAL_META_PREFIXES)


def _loads(v, default=None):
    if v is None or v == "":
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


def _canon(url: str | None) -> str:
    if not url:
        return ""
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc, p.path or "/", p.query, ""))


def _rows(store: Store, sql: str, *args) -> list[dict]:
    return [dict(r) for r in store.conn.execute(sql, args)]


def _one(store: Store, sql: str, *args):
    r = store.conn.execute(sql, args).fetchone()
    return None if r is None else r[0]


def _marks(items) -> str:
    return ",".join("?" * len(items))


def _body_text(store: Store, inline: str | None, path: str | None) -> str | None:
    if inline is not None:
        return inline
    if not path:
        return None
    p = _run_root(store) / path
    try:
        if p.stat().st_size > _MAX_BODY_FILE:
            return None
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


# -- value links ---------------------------------------------------------------------
def _leaves(obj: Any, path: str = "$", out: list | None = None, depth: int = 0) -> list:
    out = [] if out is None else out
    if len(out) >= _MAX_LEAVES or depth > 32:
        return out
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f".{k}" if re.fullmatch(r"[A-Za-z_][\w]*", str(k)) else f"[{json.dumps(str(k))}]"
            _leaves(v, path + key, out, depth + 1)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _leaves(v, f"{path}[{i}]", out, depth + 1)
    else:
        out.append((path, obj))
    return out


def _candidate(v: Any, marker: str | None) -> str | None:
    """A leaf worth tracing: ints >= 2 or distinctive strings of 4..256 chars."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return str(v) if v >= 2 else None
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not 4 <= len(s) <= 256 or "<redacted:" in s or s.lower() in _COMMON_WORDS:
        return None
    if marker and (s == marker or (len(s) >= 8 and marker.startswith(s))):
        return None  # the run marker alone (or its truncation) says nothing about provenance
    return s


def _json_values(text: str) -> set[str] | None:
    try:
        obj = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(obj, (dict, list)):
        return None
    out = set()
    for _p, v in _leaves(obj):
        if isinstance(v, bool) or v is None:
            continue
        out.add(str(v))
    return out


def _consumer_tokens(store: Store, row) -> dict[str, set[str]]:
    """What a request carries, per location (exact segments/values plus word tokens)."""
    toks: dict[str, set[str]] = {"path": set(), "query": set(), "body": set(), "header": set()}
    parts = urlsplit(row["url"] or "")
    toks["path"] = {unquote(s) for s in parts.path.split("/") if s}
    for _k, v in parse_qsl(parts.query, keep_blank_values=False):
        toks["query"].add(v)
        toks["query"].update(_TOKEN.findall(v))
    body = _body_text(store, row["req_body"], row["req_body_path"])
    if body:
        vals = _json_values(body)
        if vals is not None:
            toks["body"] |= vals
        elif "=" in body and "\n" not in body.strip():
            toks["body"].update(v for _k, v in parse_qsl(body, keep_blank_values=False))
        toks["body"].update(_TOKEN.findall(body))
    for name, value in (_loads(row["req_headers"], {}) or {}).items():
        if not isinstance(name, str) or name.lower() in _SKIP_HEADERS \
                or name.lower().startswith("sec-") or not isinstance(value, str):
            continue
        toks["header"].add(value)
        toks["header"].update(_TOKEN.findall(value))
    return toks


def _location_leaves(row) -> list[tuple[str, Any]]:
    """A redirect's ``Location`` as producer leaves (SSR apps hand out a created record's id
    in the redirect, not in JSON): ``$location.path[i]`` per segment, ``$location.query.k``."""
    headers = _loads(row["resp_headers"], {}) or {}
    loc = next((v for k, v in headers.items()
                if isinstance(k, str) and k.lower() == "location" and isinstance(v, str)), None)
    if not loc:
        return []
    parts = urlsplit(urljoin(row["url"] or "", loc))
    out: list[tuple[str, Any]] = [(f"$location.path[{i}]", _int_or_str(unquote(seg)))
                                  for i, seg in enumerate(s for s in parts.path.split("/") if s)]
    out += [(f"$location.query.{k}", _int_or_str(v))
            for k, v in parse_qsl(parts.query, keep_blank_values=False)]
    return out


def _int_or_str(v: str) -> Any:
    return int(v) if v.isdigit() and len(v) < 19 else v


def derive_value_links(store: Store, *, marker: str | None = None) -> int:
    """Rebuild ``value_links``: a JSON response leaf (or a segment/parameter of a redirect's
    ``Location``) later sent by the same role.

    A value links only from its first producer, only when the role had not already sent it
    (including in the producer's own request), and never when it is a redacted placeholder,
    the run marker, 0/1, or present in more than half of the role's requests (with >= 4).
    """
    store.conn.execute("DELETE FROM value_links")
    marker = marker if marker is not None else run_marker(store)
    rows = store.conn.execute(
        "SELECT id, role, method, url, req_headers, req_body, req_body_path, content_type, "
        "resp_headers, resp_body, resp_body_path FROM exchanges WHERE role IS NOT NULL "
        "ORDER BY id").fetchall()
    # inverted index per role: value -> [(exchange id, location)]
    index: dict[str, dict[str, list[tuple[int, str]]]] = defaultdict(lambda: defaultdict(list))
    total: dict[str, int] = defaultdict(int)
    for r in rows:
        t = _consumer_tokens(store, r)
        total[r["role"]] += 1
        for loc, vals in t.items():
            for v in vals:
                index[r["role"]][v].append((r["id"], loc))
    seen: set[tuple[str, str]] = set()
    n = 0
    for r in rows:
        leaves = _location_leaves(r)
        if "json" in (r["content_type"] or "").lower():
            text = _body_text(store, r["resp_body"], r["resp_body_path"])
            try:
                obj = json.loads(text) if text else None
            except ValueError:
                obj = None
            if isinstance(obj, (dict, list)):
                leaves += _leaves(obj)
        role, pid = r["role"], r["id"]
        for jpath, leaf in leaves:
            v = _candidate(leaf, marker)
            if v is None or (role, v) in seen:
                continue
            seen.add((role, v))
            hits = index[role].get(v, [])
            if any(xid <= pid for xid, _loc in hits):
                continue  # the client sent it before (or with) this response: not produced here
            if total[role] >= 4 and len({xid for xid, _ in hits}) * 2 > total[role]:
                continue  # trivially common for this role
            vh = hashlib.sha256(v.encode("utf-8")).hexdigest()[:16]
            done: set[tuple[int, str]] = set()
            for xid, loc in hits:
                if (xid, loc) in done:
                    continue
                done.add((xid, loc))
                store.add_value_link(pid, jpath, xid, loc, vh)
                n += 1
    return n


# -- flows ---------------------------------------------------------------------------
@dataclass
class _Step:
    type: str
    label: str = ""
    url: str | None = None
    mutating: bool = False
    transition_id: int | None = None
    exchange_id: int | None = None
    method: str | None = None
    path_pattern: str | None = None
    status: int | None = None


@dataclass
class _Flow:
    role: str
    labels: list[str]
    route_pattern: str
    origin: str
    steps: list[_Step]
    prefix_len: int              # leading steps that restore the page (goto/click)
    start_url: str               # canonical URL of the first goto ('' if none)
    landing_url: str             # canonical URL the final action left the page on
    form_ids: list[int] = field(default_factory=list)
    approval_id: str | None = None
    event_id: str | None = None  # events.jsonl id of the final action (submit/click)

    @property
    def key(self) -> str:
        sig = []
        for s in self.steps[self.prefix_len:]:
            if s.exchange_id is not None:
                sig.append(f"{s.method} {s.path_pattern or urlsplit(s.url or '').path}")
            else:
                sig.append(f"{s.type.upper()} {s.label}@{self.route_pattern}")
        return "|".join([self.role, *sig])


def _state_url(store: Store, state_id: str | None) -> str:
    return _canon(_one(store, "SELECT url FROM states WHERE id=?", state_id)) if state_id else ""


def _route_of(store: Store, state_id: str | None) -> str:
    return (_one(store, "SELECT route_pattern FROM states WHERE id=?", state_id) or "") \
        if state_id else ""


def _prefix_steps(store: Store, role: str, prefix: list[dict]) -> list[_Step]:
    """Resolve the restore part of an action path to recorded transitions/exchanges."""
    steps: list[_Step] = []
    cur: str | None = None
    for st in prefix:
        kind = st.get("type") or "?"
        if kind == "goto":
            url = st.get("url") or ""
            sids = [r["id"] for r in store.conn.execute(
                "SELECT id, url FROM states WHERE role=?", (role,)) if _canon(r["url"]) == _canon(url)]
            tid = None
            if sids:
                row = store.conn.execute(
                    f"SELECT id, to_state FROM transitions WHERE role=? AND action_type='navigate' "
                    f"AND to_state IN ({_marks(sids)}) ORDER BY id LIMIT 1", (role, *sids)).fetchone()
                tid, cur = (row["id"], row["to_state"]) if row else (None, sids[0])
            x = store.conn.execute(
                "SELECT x.id, x.status, e.path_pattern FROM exchanges x "
                "LEFT JOIN endpoints e ON e.id=x.endpoint_id WHERE x.role=? AND x.method='GET' "
                "AND x.url=? ORDER BY x.id LIMIT 1", (role, url)).fetchone()
            steps.append(_Step("goto", "", url, False, tid, x["id"] if x else None,
                               "GET" if x else None, x["path_pattern"] if x else None,
                               x["status"] if x else None))
        else:
            label = st.get("form_label") or st.get("label") or ""
            row = store.conn.execute(
                "SELECT id, to_state FROM transitions WHERE role=? AND action_type=? "
                "AND coalesce(action_label,'')=? ORDER BY (from_state IS ?) DESC, id LIMIT 1",
                (role, kind, label, cur)).fetchone()
            if row:
                cur = row["to_state"]
            steps.append(_Step(kind, label, st.get("url") or st.get("href"),
                               bool(st.get("mutating")) or kind == "submit",
                               row["id"] if row else None))
    return steps


def _action_steps(store: Store, kind: str, label: str, tid: int | None, ids: list[int],
                  action_endpoint_id: int | None = None) -> list[_Step]:
    """The final action: one step per request it sent (writes first, else the form's
    endpoint, else non-static requests), failed attempts and assets left out."""
    rows = _rows(store, f"""SELECT x.id, x.method, x.url, x.status, x.endpoint_id,
                                   e.path_pattern, e.kind FROM exchanges x
                            LEFT JOIN endpoints e ON e.id=x.endpoint_id
                            WHERE x.id IN ({_marks(ids)}) ORDER BY x.id""", *ids) if ids else []
    ok = [r for r in rows if r["status"] is None or r["status"] < 400]
    chosen = [r for r in ok if (r["method"] or "").upper() not in _SAFE] \
        or [r for r in ok if action_endpoint_id and r["endpoint_id"] == action_endpoint_id] \
        or [r for r in ok if r["kind"] not in ("static", None)]
    if not chosen:
        return [_Step(kind, label, None, False, tid)]
    return [_Step(kind, label, r["url"], (r["method"] or "").upper() not in _SAFE, tid, r["id"],
                  (r["method"] or "").upper(), r["path_pattern"], r["status"]) for r in chosen]


def _form_labels(store: Store) -> dict[int, str]:
    """form id -> label, from the action paths that targeted it."""
    out: dict[int, str] = {}
    for (raw,) in store.conn.execute(
            "SELECT action_path FROM frontier WHERE kind='action' "
            "UNION ALL SELECT action_path FROM approvals"):
        for st in _loads(raw, []) or []:
            if isinstance(st, dict) and st.get("form_id") and st.get("form_label"):
                out.setdefault(int(st["form_id"]), str(st["form_label"]))
    return out


def _ui_submissions(store: Store) -> list[_Flow]:
    flows: list[_Flow] = []
    used: set[int] = set()
    for item in _rows(store, "SELECT id, role, action_path FROM frontier "
                             "WHERE kind='action' AND status='done' ORDER BY id"):
        path = _loads(item["action_path"], []) or []
        if not path or not isinstance(path[-1], dict) or path[-1].get("type") != "submit":
            continue
        last, role = path[-1], item["role"]
        form = store.conn.execute("SELECT * FROM forms WHERE id=?",
                                  (last.get("form_id"),)).fetchone()
        if form is None or form["succeeded"] != 1:
            continue
        label = last.get("form_label") or ""
        pattern = _route_of(store, form["state_id"])
        tr = None
        for t in _rows(store, "SELECT t.*, s.route_pattern FROM transitions t "
                              "LEFT JOIN states s ON s.id=t.from_state WHERE t.role=? "
                              "AND t.action_type='submit' AND coalesce(t.action_label,'')=? "
                              "ORDER BY (t.from_state IS ?) DESC, t.id", role, label,
                       form["state_id"]):
            if t["id"] in used:
                continue
            if t["from_state"] == form["state_id"] or t["route_pattern"] == pattern:
                tr = t
                break
        if tr is None:
            continue
        used.add(tr["id"])
        prefix = _prefix_steps(store, role, path[:-1])
        action = _action_steps(store, "submit", label, tr["id"], _loads(tr["exchange_ids"], []) or [],
                               form["action_endpoint_id"])
        first = path[0] if isinstance(path[0], dict) else {}
        flows.append(_Flow(role, [label or "submit"], _route_of(store, tr["from_state"]) or pattern,
                           "ui", prefix + action, len(prefix),
                           _canon(first.get("url")) if first.get("type") == "goto" else "",
                           _state_url(store, tr["to_state"]), form_ids=[form["id"]],
                           event_id=tr["event_id"]))
    return flows


def _read_events(store: Store) -> tuple[dict[str, dict], dict[str, tuple[str, int]]]:
    """One pass over events.jsonl: ``approval_executed`` events by id, and each ``submit``
    event's (role, position among that role's submits)."""
    executed: dict[str, dict] = {}
    submits: dict[str, tuple[str, int]] = {}
    counters: dict[str, int] = defaultdict(int)
    path = _run_root(store) / "events.jsonl"
    if not path.exists():
        return executed, submits
    with open(path, encoding="utf-8") as f:
        for line in f:
            if '"approval_executed"' not in line and '"submit"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if not isinstance(ev, dict):
                continue
            if ev.get("type") == "approval_executed":
                executed[ev.get("id")] = ev
            elif ev.get("type") == "submit":
                role = ev.get("role") or ""
                submits[ev.get("id")] = (role, counters[role])
                counters[role] += 1
    return executed, submits


def _approved_executions(store: Store, events: dict[str, dict]) -> list[_Flow]:
    failed = {r[0][len("approval_failed:"):] for r in store.conn.execute(
        "SELECT key FROM meta WHERE key LIKE 'approval_failed:%'")}
    rows = _rows(store, "SELECT * FROM approvals WHERE executed_event IS NOT NULL ORDER BY id")
    flows: list[_Flow] = []
    for a in rows:
        ev = events.get(a["executed_event"])
        if a["id"] in failed or ev is None or (ev.get("data") or {}).get("outcome") != "done":
            continue
        data = ev.get("data") or {}
        path = [s for s in (_loads(a["action_path"], []) or []) if isinstance(s, dict)]
        last = path[-1] if path else {}
        tr = store.conn.execute("SELECT * FROM transitions WHERE event_id=? AND role IS ?",
                                (ev.get("caused_by"), a["role"])).fetchone() \
            if ev.get("caused_by") else None
        restore = path[:-1] if last.get("type") in ("click", "submit") else []
        prefix = _prefix_steps(store, a["role"], restore)
        kind = last.get("type") or "request"
        action = _action_steps(store, "goto" if kind == "goto" else kind, a["label"] or "",
                               tr["id"] if tr else None, list(data.get("exchange_ids") or []))
        if all(s.exchange_id is None and s.transition_id is None for s in action):
            continue
        pattern = _route_of(store, tr["from_state"]) if tr else ""
        if not pattern:
            pattern = next((s.path_pattern for s in action if s.path_pattern), "") or ""
        first = path[0] if path else {}
        form_ids = [int(last["form_id"])] if last.get("type") == "submit" \
            and isinstance(last.get("form_id"), int) else []
        flows.append(_Flow(a["role"], [a["label"] or a["category"] or "approved action"],
                           pattern, "approved", prefix + action, len(prefix),
                           _canon(first.get("url")) if first.get("type") == "goto" else "",
                           _state_url(store, tr["to_state"]) if tr else "",
                           form_ids=form_ids, approval_id=a["id"],
                           event_id=ev.get("caused_by")))
    return flows


def _merge_wizards(flows: list[_Flow], submits: dict[str, tuple[str, int]]) -> list[_Flow]:
    """Multi-step forms: flow B continues flow A when B's submit is the very next submit
    event of the same role after A's (one sequential session, nothing submitted in
    between) and B's restore is just ``goto`` of the URL A's submit landed on.

    Merged flows are additive: every standalone flow is kept and each maximal chain is
    returned once more as one multi-step flow. Event order makes chains acyclic; a
    visited-set guard still stops any loop."""
    at = {submits[f.event_id]: f for f in flows if f.event_id in submits}
    nxt: dict[int, _Flow] = {}
    for f in flows:
        if f.event_id not in submits or not f.landing_url:
            continue
        role, i = submits[f.event_id]
        g = at.get((role, i + 1))
        if g is not None and g is not f and g.role == f.role and g.prefix_len == 1 \
                and g.steps and g.steps[0].type == "goto" and g.start_url == f.landing_url:
            nxt[id(f)] = g
    absorbed = {id(g) for g in nxt.values()}
    merged: list[_Flow] = []
    for f in flows:
        if id(f) not in nxt or id(f) in absorbed:
            continue  # only chain heads start a merged flow
        cur, seen, m = f, {id(f)}, f
        while id(cur) in nxt and id(nxt[id(cur)]) not in seen:
            g = nxt[id(cur)]
            seen.add(id(g))
            m = _Flow(m.role, m.labels + g.labels, m.route_pattern, m.origin,
                      m.steps + g.steps[1:], m.prefix_len, m.start_url, g.landing_url,
                      m.form_ids + g.form_ids, m.approval_id or g.approval_id, g.event_id)
            cur = g
        merged.append(m)
    return flows + merged


def _describe(steps: list[_Step]) -> str:
    parts = []
    for i, s in enumerate(steps, 1):
        if s.type == "goto":
            txt = f"goto {s.url}"
        else:
            txt = f"{s.type} '{s.label}'"
            if s.method:
                txt += f" -> {s.method} {s.path_pattern or s.url}" + \
                    (f" ({s.status})" if s.status else "")
        parts.append(f"{i}. {txt}" + (" [mutating]" if s.mutating else ""))
    return "\n".join(parts)


def derive_flows(store: Store) -> int:
    """Rebuild ``flows``/``flow_steps``: one flow per successful form submission and per
    executed approval, plus one merged flow per sequential multi-step form chain
    (duplicates by flow key dropped)."""
    store.conn.execute("DELETE FROM flow_steps")
    store.conn.execute("DELETE FROM flows")
    executed, submits = _read_events(store)
    flows = _merge_wizards(_ui_submissions(store) + _approved_executions(store, executed),
                           submits)
    seen: set[str] = set()
    n = 0
    for f in flows:
        key = f.key
        if key in seen:
            continue
        seen.add(key)
        name = f"{f.role}: {' → '.join(f.labels)} @ {f.route_pattern or '/'}"
        pre = {"role": f.role, "requires": [], "flow_key": key, "origin": f.origin,
               "approval_id": f.approval_id, "form_ids": f.form_ids,
               "steps": [{"ordinal": i, "type": s.type, "label": s.label, "url": s.url,
                          "method": s.method, "path_pattern": s.path_pattern,
                          "status": s.status, "mutating": s.mutating,
                          "transition_id": s.transition_id, "exchange_id": s.exchange_id}
                         for i, s in enumerate(f.steps, 1)]}
        fid = store.add_flow(name, f.role, _describe(f.steps), pre)
        for i, s in enumerate(f.steps, 1):
            store.add_flow_step(fid, i, s.transition_id, s.exchange_id)
        n += 1
    return n


def derive_preconditions(store: Store) -> int:
    """Per flow: values its requests consume that a request outside the flow produced.
    Returns the number of requirements recorded across all flows."""
    total = 0
    for f in _rows(store, "SELECT id, role, preconditions FROM flows ORDER BY id"):
        pre = _loads(f["preconditions"], {}) or {}
        mine = [r[0] for r in store.conn.execute(
            "SELECT exchange_id FROM flow_steps WHERE flow_id=? AND exchange_id IS NOT NULL",
            (f["id"],))]
        requires, seen = [], set()
        if mine:
            for l in _rows(store, f"""
                    SELECT v.*, x.method, coalesce(e.path_pattern, x.url) AS path
                    FROM value_links v JOIN exchanges x ON x.id=v.producer_exchange_id
                    LEFT JOIN endpoints e ON e.id=x.endpoint_id
                    WHERE v.consumer_exchange_id IN ({_marks(mine)})
                      AND v.producer_exchange_id NOT IN ({_marks(mine)})
                    ORDER BY v.consumer_exchange_id, v.id""", *mine, *mine):
                endpoint = f"{l['method']} {l['path']}"
                if (endpoint, l["producer_json_path"]) in seen:
                    continue
                seen.add((endpoint, l["producer_json_path"]))
                requires.append({"endpoint": endpoint, "json_path": l["producer_json_path"],
                                 "producer_exchange_id": l["producer_exchange_id"],
                                 "consumer_exchange_id": l["consumer_exchange_id"],
                                 "consumer_location": l["consumer_location"]})
        pre.update({"role": f["role"], "requires": requires})
        store.conn.execute("UPDATE flows SET preconditions=? WHERE id=?",
                           (json.dumps(pre), f["id"]))
        total += len(requires)
    return total


# -- names.yaml ----------------------------------------------------------------------
_STUB_HEADER = (
    "# names.yaml: name and describe flows, give forms a purpose, then run\n"
    "#   appmap build --run <run dir> --names names.yaml\n"
    "# Flows are matched by `key` (stable across rebuilds), forms by `id`; a form's purpose\n"
    "# applies to every form with the same label, method and page. Empty values are ignored.\n"
    "# A rebuild appends new entries here and never changes existing ones.\n")


def _form_groups(store: Store) -> list[dict]:
    """One entry per distinct (label, method, page) form; ``ids`` lists every row."""
    labels = _form_labels(store)
    groups: dict[tuple, dict] = {}
    for r in _rows(store, "SELECT f.id, f.method, f.purpose, s.route_pattern FROM forms f "
                          "LEFT JOIN states s ON s.id=f.state_id ORDER BY f.id"):
        label = labels.get(r["id"], "")
        sig = (label, (r["method"] or "").upper(), r["route_pattern"] or "")
        g = groups.setdefault(sig, {"id": r["id"], "label": label or f"form {r['id']}",
                                    "method": sig[1], "page": sig[2], "ids": [],
                                    "purpose": r["purpose"] or ""})
        g["ids"].append(r["id"])
        if r["purpose"] and not g["purpose"]:
            g["purpose"] = r["purpose"]
    return list(groups.values())


def _read_names(path: Path) -> dict:
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"{path}: not valid YAML ({e})") from None
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping with 'flows' and 'forms'")
    return data


def emit_naming_stub(store: Store, path: Path) -> None:
    """Write ``names.yaml`` for the agent; an existing file keeps every entry as written
    and only gains entries for flows/forms it does not mention yet."""
    path = Path(path)
    old = _read_names(path) if path.exists() else {}
    flows = [f for f in (old.get("flows") or []) if isinstance(f, dict)]
    forms = [f for f in (old.get("forms") or []) if isinstance(f, dict)]
    have_keys = {f.get("key") for f in flows}
    for r in _rows(store, "SELECT id, name, preconditions FROM flows ORDER BY id"):
        key = (_loads(r["preconditions"], {}) or {}).get("flow_key")
        if key not in have_keys:
            flows.append({"id": r["id"], "key": key, "name": r["name"], "description": ""})
            have_keys.add(key)
    have_forms = {f.get("id") for f in forms}
    for g in _form_groups(store):
        if not have_forms & set(g["ids"]):
            forms.append({"id": g["id"], "label": g["label"], "purpose": g["purpose"]})
    text = _STUB_HEADER + yaml.safe_dump({"flows": flows, "forms": forms}, sort_keys=False,
                                         allow_unicode=True, default_flow_style=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def apply_names(store: Store, path: Path) -> int:
    """Apply flow names/descriptions (by ``key``, else ``id``) and form purposes.
    Returns the number of rows changed. Raises ValueError for unreadable YAML."""
    data = _read_names(path)
    by_key = {}
    for r in _rows(store, "SELECT id, preconditions FROM flows"):
        k = (_loads(r["preconditions"], {}) or {}).get("flow_key")
        if k:
            by_key[k] = r["id"]
    changed = 0
    for e in data.get("flows") or []:
        if not isinstance(e, dict):
            continue
        key, id_ = e.get("key"), e.get("id")
        if key is not None:
            fid = by_key.get(key) if isinstance(key, str) else None
        else:
            fid = id_ if isinstance(id_, int) and not isinstance(id_, bool) and _one(
                store, "SELECT 1 FROM flows WHERE id=?", id_) else None
        if fid is None:
            continue
        for col in ("name", "description"):
            v = e.get(col)
            if isinstance(v, str) and v.strip():
                cur = store.conn.execute(f"UPDATE flows SET {col}=? WHERE id=? AND {col} IS NOT ?",
                                         (v.strip(), fid, v.strip()))
                changed += cur.rowcount
    groups = _form_groups(store)
    for e in data.get("forms") or []:
        if not isinstance(e, dict) or not isinstance(e.get("purpose"), str) \
                or not e["purpose"].strip():
            continue
        fid = e.get("id")
        if not isinstance(fid, int) or isinstance(fid, bool):
            continue
        g = next((g for g in groups if fid in g["ids"]), None)
        if g is None:
            continue
        cur = store.conn.execute(
            f"UPDATE forms SET purpose=? WHERE id IN ({_marks(g['ids'])}) AND purpose IS NOT ?",
            (e["purpose"].strip(), *g["ids"], e["purpose"].strip()))
        changed += cur.rowcount
    return changed


# -- appmap.json ---------------------------------------------------------------------
def _tables(store: Store) -> list[str]:
    return [r[0] for r in store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name") if r[0] not in INTERNAL_TABLES]


def export_json(store: Store, path: Path) -> None:
    """``{"schema_version", "run": run.json, "<table>": [rows]}`` for every public table;
    JSON columns decoded; internal meta keys left out."""
    out: dict[str, Any] = {"schema_version": store.get_meta("schema_version") or SCHEMA_VERSION,
                           "run": _run_meta(store)}
    for t in _tables(store):
        rows = _rows(store, f"SELECT * FROM {t}" + (" ORDER BY key" if t == "meta" else
                                                    " ORDER BY rowid"))
        if t == "meta":
            rows = [r for r in rows if not is_internal_meta_key(r["key"])]
        for col in JSON_COLUMNS.get(t, ()):
            for r in rows:
                if isinstance(r.get(col), str):
                    r[col] = _loads(r[col], r[col])
        out[t] = rows
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


# -- APP-MAP.md ----------------------------------------------------------------------
CAPS = {"routes": 150, "endpoint_groups": 40, "endpoints_per_group": 40, "matrix": 120,
        "flows": 40, "steps": 25, "forms": 60, "graphql": 100, "approvals": 50, "gaps": 15,
        "observations": 40, "records": 100}


def _cap(items: list, n: int) -> tuple[list, int]:
    return items[:n], max(len(items) - n, 0)


def _ctx_routes(store: Store) -> dict:
    """Routes as an indented tree by path segment (intermediate segments without a route
    of their own are shown as plain prefixes)."""
    rows = _rows(store, """SELECT r.id, r.pattern, r.host, r.kind,
        (SELECT group_concat(source, ', ') FROM (SELECT source FROM sources WHERE item_kind='route'
           AND item_id=r.id ORDER BY source)) AS sources,
        (SELECT group_concat(role, ', ') FROM (SELECT DISTINCT role FROM states
           WHERE route_pattern=r.pattern ORDER BY role)) AS reached_by
        FROM routes r ORDER BY r.host, r.pattern, r.kind""")
    hosts = sorted({r["host"] for r in rows})
    by_key: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        by_key[(r["host"], tuple(s for s in r["pattern"].split("/") if s))].append(r)
    lines: list[dict] = []
    emitted: set[tuple] = set()
    for host, segs in sorted(by_key):
        for i in range(len(segs) + 1):
            k = (host, segs[:i])
            if k in emitted or (i < len(segs) and i == 0):
                continue
            emitted.add(k)
            depth = max(i - 1, 0)
            label = "/" + "/".join(segs[:i])
            for r in by_key.get(k, []) or [None]:
                lines.append({"depth": depth, "pattern": label, "host": host,
                              "kind": r["kind"] if r else "", "sources": r["sources"] if r else "",
                              "reached_by": r["reached_by"] if r else "", "prefix_only": r is None})
    shown, _ = _cap(lines, CAPS["routes"])
    more = len(rows) - sum(1 for line in shown if not line["prefix_only"])
    return {"rows": shown, "more": more, "multi_host": len(hosts) > 1}


def _ctx_endpoints(store: Store) -> dict:
    rows = _rows(store, """SELECT e.*,
        (SELECT group_concat(source, ', ') FROM (SELECT source FROM sources WHERE item_kind='endpoint'
           AND item_id=e.id ORDER BY source)) AS sources,
        (SELECT group_concat(role, ', ') FROM (SELECT DISTINCT role FROM exchanges
           WHERE endpoint_id=e.id AND role IS NOT NULL ORDER BY role)) AS roles
        FROM endpoints e WHERE e.kind != 'static' ORDER BY e.path_pattern, e.method, e.graphql_op""")
    statuses: dict[int, set] = defaultdict(set)
    for r in store.conn.execute("SELECT endpoint_id, statuses FROM responses"):
        statuses[r[0]].update(_loads(r[1], []) or [])
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        r["statuses"] = ", ".join(str(s) for s in sorted(statuses.get(r["id"], ())))
        seg = next((s for s in r["path_pattern"].split("/") if s), "")
        groups["/" + seg].append(r)
    out = []
    for name in sorted(groups):
        shown, more = _cap(groups[name], CAPS["endpoints_per_group"])
        out.append({"name": name, "rows": shown, "more": more, "total": len(groups[name])})
    shown, more = _cap(out, CAPS["endpoint_groups"])
    return {"groups": shown, "more_groups": more,
            "more_endpoints": sum(g["total"] for g in out[CAPS["endpoint_groups"]:]),
            "static": _one(store, "SELECT count(*) FROM endpoints WHERE kind='static'") or 0}


def _ctx_matrix(store: Store) -> dict:
    roles = [r[0] for r in store.conn.execute(
        "SELECT name FROM roles UNION SELECT DISTINCT role FROM role_access ORDER BY 1")]
    cells: dict[tuple, dict] = defaultdict(dict)
    for r in _rows(store, """SELECT ra.item_kind, ra.item_id, ra.role, ra.outcome,
            CASE ra.item_kind WHEN 'route' THEN (SELECT pattern FROM routes WHERE id=ra.item_id)
            ELSE (SELECT method || ' ' || path_pattern ||
                  CASE WHEN graphql_op != '' THEN ' ' || graphql_op ELSE '' END
                  FROM endpoints WHERE id=ra.item_id) END AS item
            FROM role_access ra ORDER BY ra.item_kind, item"""):
        cells[(r["item_kind"], r["item"] or f"#{r['item_id']}")][r["role"]] = r["outcome"]
    rows = [{"kind": k, "item": i, "cells": [c.get(role, "") for role in roles]}
            for (k, i), c in cells.items()]
    shown, more = _cap(rows, CAPS["matrix"])
    return {"roles": roles, "rows": shown, "more": more}


def _ctx_flows(store: Store) -> dict:
    out = []
    for f in _rows(store, "SELECT * FROM flows ORDER BY role, id"):
        pre = _loads(f["preconditions"], {}) or {}
        steps, more = _cap(pre.get("steps") or [], CAPS["steps"])
        out.append({"id": f["id"], "name": f["name"], "role": f["role"],
                    "origin": pre.get("origin"), "description": f["description"] or "",
                    "generated": f["description"] == _describe(
                        [_Step(**{k: s.get(k) for k in _Step.__dataclass_fields__})
                         for s in pre.get("steps") or []]),
                    "steps": steps, "more_steps": more, "requires": pre.get("requires") or []})
    shown, more = _cap(out, CAPS["flows"])
    return {"rows": shown, "more": more}


def _ctx_forms(store: Store) -> dict:
    groups = _form_groups(store)
    out = []
    for g in groups:
        f = store.conn.execute("""SELECT f.*, e.method AS ep_method, e.path_pattern
            FROM forms f LEFT JOIN endpoints e ON e.id=f.action_endpoint_id WHERE f.id IN ({})
            ORDER BY (f.succeeded IS 1) DESC, (f.action_endpoint_id IS NOT NULL) DESC, f.id
            LIMIT 1""".format(_marks(g["ids"])), g["ids"]).fetchone()
        fields = []
        for x in _rows(store, "SELECT * FROM fields WHERE form_id=? ORDER BY id", f["id"]):
            cons = _loads(x["constraints"], {}) or {}
            fields.append(f"{x['name'] or '?'} ({x['type'] or x['widget'] or '?'}"
                          + (", required" if cons.get("required") else "") + ")")
        ok = [r[0] for r in store.conn.execute(
            f"SELECT succeeded FROM forms WHERE id IN ({_marks(g['ids'])})", g["ids"])]
        out.append({**g, "action": f"{f['ep_method']} {f['path_pattern']}" if f["path_pattern"] else "",
                    "fields": ", ".join(fields),
                    "result": "succeeded" if 1 in ok else ("failed" if 0 in ok else "not submitted")})
    shown, more = _cap(out, CAPS["forms"])
    return {"rows": shown, "more": more}


_FIELD_OFFSETS = {"text": 0, "email": 13, "url": 21}  # datagen: "@example.test", "https://example.test/"
_NO_MARKER_TYPES = {"number", "range", "date", "datetime-local", "month", "time", "week",
                    "color", "checkbox", "radio", "select", "select-one", "select-multiple",
                    "file", "tel", "hidden", "submit", "button", "image", "reset"}


def _field_kind(ftype: str | None, name: str | None) -> str | None:
    """How datagen fills a field with the marker (None: it never does)."""
    t, ident = (ftype or "text").lower(), (name or "").lower()
    if t == "email" or re.search(r"e-?mail", ident):
        return "email"
    if t == "url" or re.search(r"\burl\b|website", ident):
        return "url"
    if t == "password":
        return "password"
    if t in _NO_MARKER_TYPES or re.search(r"phone|mobile|tel\b", ident):
        return None
    return "text"


def marker_variants(store: Store, marker: str, run_id: str | None = None) -> set[str]:
    """The marker plus the truncations the data generator produced in THIS run.

    Per recorded field: the room from its ``maxlength`` or the length hints in its
    validation messages, minus datagen's offset for that field kind (email, url, password
    tail, plain text). A truncation is used only while it still holds the run-specific
    part: the text before the run id plus at least the 8 date digits of the run id."""
    from appmap.datagen import _PASSWORD_TAIL, hint_from_validation

    run_id = run_id or _run_meta(store).get("run_id") or ""
    at = marker.find(run_id) if run_id else -1
    min_len = at + 8 if at >= 0 else len(marker)
    out = {marker}
    for ftype, name, cons, msgs in store.conn.execute(
            "SELECT type, name, constraints, validation_messages FROM fields"):
        kind = _field_kind(ftype, name)
        if kind is None:
            continue
        rooms: set[int] = set()
        ml = (_loads(cons, {}) or {}).get("maxlength")
        if isinstance(ml, int) and not isinstance(ml, bool):
            rooms.add(ml)
        for m in _loads(msgs, []) or []:
            h = hint_from_validation(m) if isinstance(m, str) else None
            if h and h.startswith("max:"):
                rooms.add(int(h[4:]))
            elif h and h.startswith("len:"):
                rooms.add(int(h[4:].split("-")[1]))
        off = len(_PASSWORD_TAIL) if kind == "password" else _FIELD_OFFSETS[kind]
        for room in rooms:
            n = room - off
            if min_len <= n < len(marker):
                out.add(marker[:n])
    return out


def _value_forms(token: str) -> set[str]:
    """A body/query token as datagen may have built it: the email local part and the
    password with its fixed tail removed are compared too."""
    from appmap.datagen import _PASSWORD_TAIL

    forms = {token, token.split("@", 1)[0]}
    for tail in (_PASSWORD_TAIL, _PASSWORD_TAIL.rstrip("!")):
        if token.endswith(tail):
            forms.add(token[: -len(tail)])
    return forms


_AUTH_PATH = re.compile(r"(?:^|/)(?:log|sign)[-_]?(?:in|out|on|off)(?:/|$|\.)", re.I)


def _login_paths(store: Store) -> set[str]:
    """Paths of the configured roles' login pages (from run.json)."""
    out = set()
    for r in ((_run_meta(store).get("config") or {}).get("roles") or []):
        url = ((r or {}).get("login") or {}).get("url") if isinstance(r, dict) else None
        if isinstance(url, str) and url:
            out.add(urlsplit(url).path.rstrip("/") or "/")
    return out


def _collect_records(store: Store) -> tuple[list[dict], list[dict], str | None, set[str]]:
    """Records created by this run (ruling R23): every exchange in the run directory was
    sent by this run, so this is every successful (2xx/3xx) non-GET exchange except login
    traffic, login/logout endpoints and writes redirected to a login page (session lost:
    nothing was created). ``marker`` annotates the marker or run-specific truncation found
    in the query string or body (never the URL host or path); rows with a marker come
    first."""
    login_paths = _login_paths(store)
    marker = run_marker(store)
    variants = marker_variants(store, marker) if marker else set()
    short = variants - {marker}
    matched, other = [], []
    for x in _rows(store, """SELECT x.id, x.role, x.method, x.url, x.status, x.origin, x.req_body,
            x.req_body_path, x.resp_headers, e.path_pattern
        FROM exchanges x LEFT JOIN endpoints e ON e.id=x.endpoint_id
        WHERE upper(x.method) NOT IN ('GET','HEAD','OPTIONS') AND x.status BETWEEN 200 AND 399
          AND x.origin != 'login' ORDER BY x.id"""):
        if _AUTH_PATH.search(urlsplit(x["url"] or "").path):
            continue
        x["marker"] = ""
        if marker:
            text = unquote_plus(" ".join(filter(None, (
                urlsplit(x["url"] or "").query,
                _body_text(store, x["req_body"], x["req_body_path"])))))
            if marker in text:
                x["marker"] = marker
            elif short:
                hit = {v for t in _TOKEN.findall(text) for v in short & _value_forms(t)}
                x["marker"] = max(hit, key=len) if hit else ""
        hdrs = _loads(x["resp_headers"], {}) or {}
        x["location"] = next((v for k, v in hdrs.items() if str(k).lower() == "location"), None)
        if x["location"]:
            to = urlsplit(urljoin(x["url"] or "", x["location"])).path
            if _AUTH_PATH.search(to) or (to.rstrip("/") or "/") in login_paths:
                continue  # bounced to the login page: the write did not happen
        (matched if x["marker"] else other).append(x)
    return matched, other, marker, short


def created_records(store: Store) -> list[dict]:
    """Every record this run created, uncapped, marker-matched rows first."""
    matched, other, _, _ = _collect_records(store)
    return matched + other


def _ctx_records(store: Store) -> dict:
    matched, other, marker, short = _collect_records(store)
    shown, more = _cap(matched + other, CAPS["records"])
    return {"rows": shown, "more": more, "marker": marker or "",
            "matched": len(matched), "variants": sorted(short, key=len, reverse=True)}


def _ctx_gaps(store: Store) -> dict:
    from appmap.gaps import compute_gaps

    g = compute_gaps(store).to_dict()
    lists = {}
    for k in ("unexercised_endpoints", "unsuccessful_forms", "unreached_routes",
              "unexplored_actions"):
        shown, more = _cap(g[k], CAPS["gaps"])
        lists[k] = {"rows": shown, "more": more, "total": len(g[k])}
    appr = {}
    for k in ("pending_approvals", "denied_approvals"):
        shown, more = _cap(g[k], CAPS["approvals"])
        appr[k] = {"rows": shown, "more": more, "total": len(g[k])}
    return {"gaps": lists, "approvals": appr}


def _ctx_observations(store: Store) -> dict:
    kinds = _rows(store, "SELECT kind, count(*) AS n FROM observations GROUP BY kind ORDER BY n DESC")
    rows = _rows(store, "SELECT * FROM observations ORDER BY id")
    shown, more = _cap(rows, CAPS["observations"])
    return {"kinds": kinds, "rows": shown, "more": more}


def _ctx_summary(store: Store, run: dict) -> dict:
    cfg = run.get("config") or {}
    counts = {t: _one(store, f"SELECT count(*) FROM {t}") for t in (
        "states", "transitions", "routes", "endpoints", "exchanges", "forms", "flows",
        "value_links", "approvals", "observations", "external_links")}
    elapsed = store.get_meta("crawl_elapsed_s")
    try:
        minutes = f"{float(elapsed) / 60:.1f} min" if elapsed else ""
    except ValueError:
        minutes = ""
    return {"targets": _targets(run),
            "roles": _rows(store, "SELECT name, login_type, auth_ok FROM roles ORDER BY name"),
            "profile": run.get("profile") or cfg.get("profile") or "",
            "run_id": run.get("run_id") or "", "started": run.get("started_at") or "",
            "updated": run.get("updated_at") or "", "crawl_time": minutes,
            "pass": store.get_meta("pass") or "1", "counts": counts}


def _targets(run: dict) -> list[str]:
    cfg = run.get("config") or {}
    return list(cfg.get("base_urls") or (cfg.get("target") or {}).get("base_urls") or [])


def _cell(v: Any) -> str:
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def render_app_map(store: Store, rd, template: Path | str | None = None) -> str:
    """Render APP-MAP.md from the store with the Jinja2 template."""
    import jinja2

    if template is None:
        src = resources.files("appmap").joinpath("templates", "app-map.md.j2").read_text(
            encoding="utf-8")
    else:
        src = Path(template).read_text(encoding="utf-8")
    env = jinja2.Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True,
                             keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    env.filters["cell"] = _cell
    run = _run_meta(store)
    gaps = _ctx_gaps(store)
    host = urlsplit((_targets(run) or [""])[0]).hostname or rd.root.name
    ctx = {
        "host": host,
        "production": bool(run.get("contains_production_data")),
        "summary": _ctx_summary(store, run),
        "routes": _ctx_routes(store),
        "endpoints": _ctx_endpoints(store),
        "matrix": _ctx_matrix(store),
        "flows": _ctx_flows(store),
        "forms": _ctx_forms(store),
        "graphql": dict(zip(("rows", "more"), _cap(_rows(store, """
            SELECT e.graphql_type, e.graphql_op, e.path_pattern, e.host,
              (SELECT group_concat(source, ', ') FROM (SELECT source FROM sources
                 WHERE item_kind='endpoint' AND item_id=e.id ORDER BY source)) AS sources,
              (SELECT count(*) FROM exchanges x WHERE x.endpoint_id=e.id) AS exchanges
            FROM endpoints e WHERE e.kind='graphql_op'
            ORDER BY e.graphql_type, e.graphql_op"""), CAPS["graphql"]))),
        "approvals": gaps["approvals"],
        "gaps": gaps["gaps"],
        "observations": _ctx_observations(store),
        "records": _ctx_records(store),
        "external_links": _one(store, "SELECT count(*) FROM external_links") or 0,
        "schema_version": store.get_meta("schema_version") or SCHEMA_VERSION,
    }
    return env.from_string(src).render(**ctx)


# -- the whole build -----------------------------------------------------------------
def schema_md_text() -> str:
    return resources.files("appmap").joinpath("resources", "SCHEMA.md").read_text(encoding="utf-8")


def build_map(store: Store, rd, names: Iterable[Path] = ()) -> dict[str, int]:
    """Derive (idempotently), refresh names.yaml, apply names, write the three outputs."""
    store.conn.execute("BEGIN IMMEDIATE")
    try:
        links = derive_value_links(store)
        flows = derive_flows(store)
        requires = derive_preconditions(store)
        store.conn.execute("COMMIT")
    except BaseException:
        store.conn.execute("ROLLBACK")
        raise
    stub = Path(rd.root) / "names.yaml"
    emit_naming_stub(store, stub)
    applied = 0
    for p in [stub, *[Path(n) for n in names if Path(n).resolve() != stub.resolve()]]:
        applied += apply_names(store, p)
    export_json(store, rd.json_path)
    md = render_app_map(store, rd)
    tmp = Path(rd.map_md_path).with_name(f".APP-MAP.md.{os.getpid()}.tmp")
    tmp.write_text(md, encoding="utf-8")
    os.replace(tmp, rd.map_md_path)
    Path(rd.schema_md_path).write_text(schema_md_text(), encoding="utf-8")
    return {"flows": flows,
            "flow_steps": _one(store, "SELECT count(*) FROM flow_steps") or 0,
            "value_links": links, "preconditions": requires, "names_applied": applied,
            "forms": _one(store, "SELECT count(*) FROM forms") or 0,
            "endpoints": _one(store, "SELECT count(*) FROM endpoints") or 0,
            "routes": _one(store, "SELECT count(*) FROM routes") or 0,
            "states": _one(store, "SELECT count(*) FROM states") or 0}
