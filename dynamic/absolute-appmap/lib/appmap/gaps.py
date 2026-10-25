"""Coverage gaps and pass tracking (orchestrator step 7: double back).

Pass tracking is derived, not accumulated: ``meta.pass`` (absent = 1) is the current pass
and ``meta.pass_<n>_baseline`` holds the states/endpoints/routes counts when pass n began
(absent for pass 1 = all zeros). ``new_in_pass`` is the current counts minus that baseline,
so crawl and probe need not write any per-pass data.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

from appmap.approvals import ApprovalQueue
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

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.to_dict().items()}


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
    for r in q("""SELECT r.id, r.pattern, r.host, r.kind FROM routes r
                  WHERE NOT EXISTS (SELECT 1 FROM role_access a WHERE a.item_kind='route'
                                    AND a.item_id=r.id AND a.outcome='allowed')
                    AND NOT EXISTS (SELECT 1 FROM states s WHERE s.route_pattern=r.pattern)
                  ORDER BY r.id"""):
        g.unreached_routes.append(dict(r))
    queue = ApprovalQueue(store, store.db_path.parent / "approvals.yaml")
    failures = queue.failures()
    for r in q("""SELECT id, role, kind, url, action_path, status, source FROM frontier
                  WHERE status IN ('pending','failed') ORDER BY id"""):
        d = dict(r)
        d["action_path"] = json.loads(d["action_path"] or "[]")
        g.unexplored_actions.append(d)
    # Approved actions that could not run are unexplored too (the reason is recorded).
    for r in q("SELECT id, role, label FROM approvals WHERE decision='approve' ORDER BY id"):
        if r["id"] in failures:
            g.unexplored_actions.append({"kind": "approval", "id": r["id"], "role": r["role"],
                                         "label": r["label"], "status": "failed",
                                         "reason": failures[r["id"]]})
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


def start_new_pass(store: Store) -> int:
    """Snapshot current counts as the baseline of the next pass and make it current."""
    n = current_pass(store) + 1
    store.set_meta(f"pass_{n}_baseline", json.dumps(current_counts(store)))
    store.set_meta("pass", str(n))
    return n
