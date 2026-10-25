"""Deterministic application profile built from an app-map run directory."""
PROFILE_SCHEMA_VERSION = "1.0.0"

from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402

from appmap.profile import multirun, names  # noqa: E402,F401

_DONE_STATUSES = ("done", "complete")  # run.json says "complete" once build has run
_NON_ENDPOINT_KINDS = ("static", "page")


def _technologies(run_dir: Path) -> list[dict]:
    from appmap.profile.evidence import iter_evidence
    from appmap.profile.fingerprint import detect, load_rules

    return [{"name": t.name, "category": t.category, "versions": t.versions,
             "confidence": t.confidence, "evidence": t.evidence}
            for t in detect(iter_evidence(run_dir), load_rules()) if t.evidence]


def _run_sections(run_dir: Path, rd, mask: bool) -> dict:
    from appmap.profile import sections as s
    from appmap.store import Store

    store = Store.open_readonly(rd.db_path)
    try:
        conn = store.conn
        out = {
            "layout": s.layout(store, rd.run_id),
            "features": s.features(store, rd.run_id),
            "data_model": s.data_model(store, rd.run_id, run_dir),
            "data_setup": s.data_setup(store, rd.run_id),
            "accounts": s.accounts(store, rd.run_id, mask=mask),
            "coverage": s.coverage(store, rd.run_id),
        }
        facts = {
            "run_id": rd.run_id,
            "routes": [r["pattern"] for r in conn.execute("SELECT DISTINCT pattern FROM routes")],
            "endpoints": [{"method": e["method"], "path_pattern": e["path_pattern"]}
                          for e in conn.execute(
                              "SELECT DISTINCT method, path_pattern, kind FROM endpoints")
                          if e["kind"] not in _NON_ENDPOINT_KINDS],
            "roles": out["accounts"]["roles"],
        }
        n_flows = conn.execute("SELECT count(*) FROM flows").fetchone()[0]
    finally:
        store.close()
    return {"sections": out, "facts": facts, "n_flows": n_flows}


def _run_gaps(run_dir: Path, rid: str, sections: dict, n_flows: int) -> tuple[list, list]:
    import json

    cnd, warn = [], []
    if not n_flows:
        cnd.append({"item": "flows and value links", "run_id": rid,
                    "reason": "no flows recorded; 'appmap build' has not been run on this run"})
    for role in sections["accounts"]["roles"]:
        if role["auth_ok"] is False:
            cnd.append({"item": f"access for role '{role['name']}'", "run_id": rid,
                        "reason": f"authentication failed for role '{role['name']}'"})
    try:
        status = json.loads((run_dir / "run.json").read_text()).get("status")
    except (OSError, ValueError):
        status = None
    if status and status not in _DONE_STATUSES:
        cnd.append({"item": "complete crawl coverage", "run_id": rid,
                    "reason": f"crawl status is '{status}', not 'done'"})
    if not any(e["source"] in ("openapi", "graphql") for e in sections["data_model"]):
        warn.append(f"run {rid}: no API schema (OpenAPI/GraphQL) found; the data model is "
                    "inferred from observed traffic")
    return cnd, warn


def build_profile(run_dirs: list[Path], *, include_observed_accounts: bool = False) -> dict:
    """Assemble the profile for one or more run directories (read-only).

    Top-level section keys describe the first run; with several runs, the same sections for
    the others are under ``other_runs[run_id]`` and ``differences`` compares the runs.
    """
    from appmap.profile.sections import environment
    from appmap.rundir import open_run_dir

    if not run_dirs:
        raise ValueError("at least one run directory is required")
    dirs = [Path(d) for d in run_dirs]
    rds = [open_run_dir(d) for d in dirs]
    envs = [environment(d) for d in dirs]
    mask = any(e["contains_production_data"] for e in envs) and not include_observed_accounts

    per_run, facts, could_not, warnings = [], [], [], []
    for d, rd in zip(dirs, rds):
        got = _run_sections(d, rd, mask)
        cnd, warn = _run_gaps(d, rd.run_id, got["sections"], got["n_flows"])
        could_not += cnd
        warnings += warn
        per_run.append(got["sections"])
        techs = _technologies(d)
        facts.append({**got["facts"], "technologies": techs})

    merged = multirun.merge(facts)
    warnings += merged["warnings"]
    first = per_run[0]
    profile = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "partial" if could_not else "profiled",
        "warnings": warnings,
        "could_not_determine": could_not,
        "environments": envs,
        "technologies": merged["technologies"],
        **first,
        "differences": merged["differences"] if len(dirs) > 1 else None,
        "purpose": None,
        "feature_names": {},
    }
    if len(dirs) > 1:
        profile["other_runs"] = {rd.run_id: sec for rd, sec in zip(rds[1:], per_run[1:])}
    return profile
