"""Coverage gaps and pass tracking (orchestrator step 7: double back).

Pass tracking is derived, not accumulated: ``meta.pass`` (absent = 1) is the current pass
and ``meta.pass_<n>_baseline`` holds the states/endpoints/routes counts when pass n began
(absent for pass 1 = all zeros). ``new_in_pass`` is the current counts minus that baseline,
so crawl and probe need not write any per-pass data.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from appmap.store import Store

DISCOVERY_SOURCES = ("bundle", "openapi", "graphql", "robots", "sitemap", "well_known",
                     "source_map", "wordlist", "spa_router")
EXERCISED_ORIGINS = ("ui", "replay", "cross_role", "schema", "approved")
_ZERO = {"states": 0, "endpoints": 0, "routes": 0}


@dataclass
class Gaps:
    unexercised_endpoints: list[dict] = field(default_factory=list)
    unsuccessful_forms: list[dict] = field(default_factory=list)
    unreached_routes: list[dict] = field(default_factory=list)
    unexplored_actions: list[dict] = field(default_factory=list)
    pending_approvals: list[dict] = field(default_factory=list)
    denied_approvals: list[dict] = field(default_factory=list)
    # Informational, not a gap (field fix 3): routes whose only answer was a redirect to
    # another (non-login) page, e.g. '/' -> '/dashboard/'.
    reached_via_redirect: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.to_dict().items() if k != "reached_via_redirect"}


_LOGIN_PATH = re.compile(r"(?:^|/)(?:log-?in|sign[-_]?in|signin)(?:/|$|\.)", re.I)


def _login_paths(store: Store) -> set[str]:
    """Paths of the configured roles' login pages (run.json next to the database)."""
    try:
        meta = json.loads((Path(store.db_path).parent / "run.json").read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return set()
    out = set()
    for r in ((meta.get("config") or {}).get("roles") or []):
        url = ((r or {}).get("login") or {}).get("url") if isinstance(r, dict) else None
        if isinstance(url, str) and url:
            out.add(urlsplit(url).path.rstrip("/") or "/")
    return out


def _redirect_target(row, login_paths: set[str]) -> str | None:
    """Where a 3xx exchange pointed, unless that is the login page (not 'reached')."""
    try:
        hdrs = json.loads(row["resp_headers"] or "{}")
    except ValueError:
        return None
    loc = next((v for k, v in hdrs.items() if str(k).lower() == "location"), None) \
        if isinstance(hdrs, dict) else None
    if not isinstance(loc, str) or not loc:
        return None
    path = urlsplit(urljoin(row["url"] or "", loc)).path
    if _LOGIN_PATH.search(path) or (path.rstrip("/") or "/") in login_paths:
        return None
    return loc


def _marks(items) -> str:
    return ",".join("?" * len(items))


def compute_gaps(store: Store) -> Gaps:
    q = store.conn.execute
    ex, disc = _marks(EXERCISED_ORIGINS), _marks(DISCOVERY_SOURCES)
    g = Gaps()
    # Known only from discovery sources and never actually exercised.
    for r in q(f"""
        SELECT e.id, e.method, e.host, e.path_pattern, e.kind, e.graphql_op FROM endpoints e
        WHERE NOT EXISTS (SELECT 1 FROM exchanges x WHERE x.endpoint_id=e.id
                          AND x.origin IN ({ex}))
          AND NOT EXISTS (SELECT 1 FROM sources s WHERE s.item_kind='endpoint'
                          AND s.item_id=e.id AND s.source NOT IN ({disc}))
        ORDER BY e.id""", (*EXERCISED_ORIGINS, *DISCOVERY_SOURCES)):
        d = dict(r)
        d["sources"] = [s["source"] for s in q(
            "SELECT source FROM sources WHERE item_kind='endpoint' AND item_id=? ORDER BY source",
            (r["id"],))]
        g.unexercised_endpoints.append(d)
    # Forms whose submission was tried and failed, or never tried.
    for r in q("""SELECT id, state_id, method, purpose, action_endpoint_id, succeeded FROM forms
                  WHERE succeeded IS NOT 1 ORDER BY id"""):
        d = dict(r)
        d["attempted"] = r["succeeded"] is not None
        d.pop("succeeded")
        g.unsuccessful_forms.append(d)
    # Routes never reached: no allowed access and no state on that pattern.
    login_paths = None
    for r in q("""SELECT r.id, r.pattern, r.host, r.kind FROM routes r
                  WHERE NOT EXISTS (SELECT 1 FROM role_access a WHERE a.item_kind='route'
                                    AND a.item_id=r.id AND a.outcome='allowed')
                    AND NOT EXISTS (SELECT 1 FROM states s WHERE s.route_pattern=r.pattern)
                  ORDER BY r.id"""):
        if login_paths is None:
            login_paths = _login_paths(store)
        # Field fix 3: a route that answered with a redirect to another page was reached
        # (the browser just landed on the target's state).
        target = next((t for x in q(f"""
            SELECT x.url, x.resp_headers FROM exchanges x JOIN endpoints e ON e.id=x.endpoint_id
            WHERE e.host=? AND e.path_pattern=? AND upper(x.method)='GET'
              AND x.status BETWEEN 300 AND 399 AND x.origin IN ({ex}) ORDER BY x.id""",
            (r["host"], r["pattern"], *EXERCISED_ORIGINS))
            if (t := _redirect_target(x, login_paths))), None)
        if target is not None:
            g.reached_via_redirect.append({**dict(r), "location": target})
        else:
            g.unreached_routes.append(dict(r))
    prefix = "approval_failed:"
    failures = {r["key"][len(prefix):]: r["value"] for r in q(
        "SELECT key, value FROM meta WHERE key LIKE ?", (prefix + "%",))}
    for r in q("""SELECT id, role, kind, url, action_path, status, source FROM frontier
                  WHERE status IN ('pending','failed') ORDER BY id"""):
        d = dict(r)
        d["action_path"] = json.loads(d["action_path"] or "[]")
        g.unexplored_actions.append(d)
    # Approved actions that have not run yet, or ran/failed with a recorded reason.
    for r in q("""SELECT id, role, label, executed_event FROM approvals
                  WHERE decision='approve' ORDER BY id"""):
        if r["id"] in failures or r["executed_event"] is None:
            d = {"kind": "approval", "id": r["id"], "role": r["role"], "label": r["label"],
                 "status": "failed" if r["id"] in failures else "approved_pending"}
            if r["id"] in failures:
                d["reason"] = failures[r["id"]]
            g.unexplored_actions.append(d)
    for r in q("""SELECT id, role, category, label, reasons, decision FROM approvals
                  WHERE decision IN ('pending','deny') ORDER BY id"""):
        d = dict(r)
        d["reasons"] = json.loads(d["reasons"] or "[]")
        if d["id"] in failures:
            d["failure"] = failures[d["id"]]
        (g.pending_approvals if r["decision"] == "pending" else g.denied_approvals).append(d)
    return g


def current_counts(store: Store) -> dict[str, int]:
    q = store.conn.execute
    return {"states": q("SELECT count(*) FROM states").fetchone()[0],
            "endpoints": q("SELECT count(*) FROM endpoints").fetchone()[0],
            "routes": q("SELECT count(*) FROM routes").fetchone()[0]}


def current_pass(store: Store) -> int:
    try:
        return int(store.get_meta("pass") or 1)
    except ValueError:
        return 1


def new_in_pass(store: Store) -> dict[str, int]:
    base = {**_ZERO, **json.loads(store.get_meta(f"pass_{current_pass(store)}_baseline") or "{}")}
    cur = current_counts(store)
    return {k: max(cur[k] - base[k], 0) for k in _ZERO}


def record_activity(store: Store) -> None:
    """Called after every crawl/probe invocation (incl. --approved-only)."""
    store.set_meta("last_activity_ts", repr(time.time()))


def _ts(store: Store, key: str) -> float:
    try:
        return float(store.get_meta(key) or 0)
    except ValueError:
        return 0.0


def start_new_pass(store: Store) -> dict:
    """Close the current pass and open the next.

    Intended order: crawl/probe -> ``status --new-pass`` -> read ``new_in_prev_pass``
    (the delta of the pass just closed; zero means the double-back converged).
    Refuses (``advanced`` False, pass unchanged) when nothing has run since the current
    pass began and the counts equal its baseline, so repeated calls cannot fake a stop.
    """
    cur = current_pass(store)
    delta = new_in_pass(store)
    base = {**_ZERO, **json.loads(store.get_meta(f"pass_{cur}_baseline") or "{}")}
    active = _ts(store, "last_activity_ts") > _ts(store, f"pass_{cur}_baseline_ts")
    if not active and current_counts(store) == base:
        return {"advanced": False, "prev_pass": cur, "new_in_prev_pass": delta}
    n = cur + 1
    store.set_meta(f"pass_{n}_baseline", json.dumps(current_counts(store)))
    store.set_meta(f"pass_{n}_baseline_ts", repr(time.time()))
    store.set_meta("pass", str(n))
    return {"advanced": True, "prev_pass": cur, "new_in_prev_pass": delta}
