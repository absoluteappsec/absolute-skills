"""Join per-run profile facts across runs (assumed: one application, several environments).

Technologies join on ``name + category``, routes on ``pattern``, endpoints on
``method + path_pattern``. Everything is namespaced by ``run_id``.
"""
from __future__ import annotations

from itertools import combinations

MIN_OVERLAP = 0.30
_CONFIDENCE_RANK = {"guessed": 0, "likely": 1, "confirmed": 2}
_EVIDENCE_PER_RUN = 3


def _overlap(a: set, b: set) -> float | None:
    """Share of the smaller set found in the other, so a partial crawl of the same app is
    not flagged; ``None`` when either set is empty."""
    if not a or not b:
        return None
    return len(a & b) / min(len(a), len(b))


def _merge_technologies(per_run: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    for run in per_run:
        for t in run.get("technologies") or []:
            m = merged.setdefault((t["name"], t["category"]), {
                "name": t["name"], "category": t["category"], "versions": [],
                "versions_by_environment": {}, "confidence": t["confidence"], "evidence": []})
            m["versions_by_environment"][run["run_id"]] = list(t.get("versions") or [])
            for v in t.get("versions") or []:
                if v not in m["versions"]:
                    m["versions"].append(v)
            if _CONFIDENCE_RANK[t["confidence"]] > _CONFIDENCE_RANK[m["confidence"]]:
                m["confidence"] = t["confidence"]
            m["evidence"].extend((t.get("evidence") or [])[:_EVIDENCE_PER_RUN])
    return list(merged.values())


def _presence(per_run: list[dict], items: dict[str, dict[object, dict]]) -> list[dict]:
    """Entries (from ``items``: run_id -> key -> payload) not present in every run."""
    run_ids = [r["run_id"] for r in per_run]
    keys: dict[object, dict] = {}
    for rid in run_ids:
        for k, payload in items[rid].items():
            keys.setdefault(k, payload)
    out = []
    for k in sorted(keys, key=str):
        present = [rid for rid in run_ids if k in items[rid]]
        if len(present) != len(run_ids):
            out.append({**keys[k], "present_in": present,
                        "missing_in": [r for r in run_ids if r not in present]})
    return out


def merge(per_run: list[dict]) -> dict:
    """``per_run`` items: ``{run_id, technologies, routes, endpoints, roles}``.

    Returns ``{technologies, differences, warnings}``; ``differences`` lists only what is
    not shared by every run.
    """
    run_ids = [r["run_id"] for r in per_run]
    technologies = _merge_technologies(per_run)

    tech_diff = []
    for t in technologies:
        vbe = t["versions_by_environment"]
        if len(vbe) != len(run_ids) or len({tuple(v) for v in vbe.values()}) > 1:
            tech_diff.append({"name": t["name"], "category": t["category"],
                              "versions_by_environment": vbe, "present_in": list(vbe),
                              "missing_in": [r for r in run_ids if r not in vbe]})

    routes = {r["run_id"]: {p: {"pattern": p} for p in r.get("routes") or []} for r in per_run}
    endpoints = {r["run_id"]: {(e["method"], e["path_pattern"]): {
        "method": e["method"], "path_pattern": e["path_pattern"]}
        for e in r.get("endpoints") or []} for r in per_run}

    role_names = sorted({ro["name"] for r in per_run for ro in r.get("roles") or []})
    role_diff = []
    for name in role_names:
        per = {}
        for r in per_run:
            ro = next((x for x in r.get("roles") or [] if x["name"] == name), None)
            if ro:
                per[r["run_id"]] = {"reachable_routes": ro["reachable_routes"],
                                    "reachable_endpoints": ro["reachable_endpoints"]}
        if len(per) != len(run_ids) or len({tuple(v.values()) for v in per.values()}) > 1:
            role_diff.append({"role": name, "per_environment": per})

    warnings = []
    for a, b in combinations(per_run, 2):
        ov = _overlap(set(a.get("routes") or []), set(b.get("routes") or []))
        if ov is not None and ov < MIN_OVERLAP:
            warnings.append(
                f"route-set overlap between runs {a['run_id']} and {b['run_id']} is "
                f"{round(ov * 100)}% (below 30%); they may not be the same application")

    return {
        "technologies": technologies,
        "differences": {"technologies": tech_diff, "routes": _presence(per_run, routes),
                        "endpoints": _presence(per_run, endpoints), "role_access": role_diff},
        "warnings": warnings,
    }
