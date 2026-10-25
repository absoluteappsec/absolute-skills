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


_SECTION_DEFAULTS = {"layout": {}, "features": [], "data_model": [], "data_setup": {},
                     "accounts": {"roles": [], "created_by_run": [], "observed": []},
                     "coverage": {}}


def _run_sections(run_dir: Path, rd, mask: bool) -> dict:
    """Every section of one run; a failing section is replaced by an empty default and
    reported in ``failed`` as (section, error class)."""
    from appmap.profile import sections as s
    from appmap.store import Store

    failed: list[tuple[str, str]] = []
    try:
        store = Store.open_readonly(rd.db_path)
        store.conn.execute("SELECT 1 FROM meta LIMIT 1")
    except Exception as e:  # noqa: BLE001 - unreadable store: report every section
        store = None
        opener = type(e).__name__

    def safe(name, fn, default):
        if store is None:
            failed.append((name, opener))
            return default
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one bad section must not sink the profile
            failed.append((name, type(e).__name__))
            return default

    try:
        d = _SECTION_DEFAULTS
        out = {
            "layout": safe("layout", lambda: s.layout(store, rd.run_id), d["layout"]),
            "features": safe("features", lambda: s.features(store, rd.run_id), d["features"]),
            "data_model": safe("data_model", lambda: s.data_model(store, rd.run_id, run_dir),
                               d["data_model"]),
            "data_setup": safe("data_setup", lambda: s.data_setup(store, rd.run_id),
                               d["data_setup"]),
            "accounts": safe("accounts", lambda: s.accounts(store, rd.run_id, mask=mask),
                             d["accounts"]),
            "coverage": safe("coverage", lambda: s.coverage(store, rd.run_id), d["coverage"]),
        }

        def basics():
            conn = store.conn
            return {
                "routes": [r["pattern"] for r in conn.execute(
                    "SELECT DISTINCT pattern FROM routes")],
                "endpoints": [{"method": e["method"], "path_pattern": e["path_pattern"]}
                              for e in conn.execute(
                                  "SELECT DISTINCT method, path_pattern, kind FROM endpoints")
                              if e["kind"] not in _NON_ENDPOINT_KINDS],
                "n_flows": conn.execute("SELECT count(*) FROM flows").fetchone()[0],
                "pending_frontier": conn.execute(
                    "SELECT count(*) FROM frontier WHERE status='pending'"
                ).fetchone()[0],
                "approved_unexecuted": conn.execute(
                    "SELECT count(*) FROM approvals WHERE decision='approve' "
                    "AND executed_event IS NULL").fetchone()[0],
            }
        b = safe("routes_and_flows", basics,
                 {"routes": [], "endpoints": [], "n_flows": None, "pending_frontier": 0,
                  "approved_unexecuted": 0})
    finally:
        if store is not None:
            store.close()
    facts = {"run_id": rd.run_id, "routes": b["routes"], "endpoints": b["endpoints"],
             "roles": out["accounts"]["roles"]}
    return {"sections": out, "facts": facts, "basics": b, "failed": failed}


def _run_gaps(run_dir: Path, rid: str, got: dict) -> tuple[list, list]:
    import json

    sections, b = got["sections"], got["basics"]
    cnd, warn = [], []
    for name, err in got["failed"]:
        cnd.append({"item": name, "run_id": rid, "reason": f"could not read: {err}"})
    if b["n_flows"] == 0:
        cnd.append({"item": "flows and value links", "run_id": rid,
                    "reason": "no flows recorded; 'appmap build' has not been run on this run"})
    for role in sections["accounts"]["roles"]:
        if role["auth_ok"] is False:
            cnd.append({"item": f"access for role '{role['name']}'", "run_id": rid,
                        "reason": f"authentication failed for role '{role['name']}'"})
    try:
        status = json.loads((run_dir / "run.json").read_text()).get("status")
    except (OSError, ValueError, AttributeError):
        status = None
    if status and status not in _DONE_STATUSES:
        cnd.append({"item": "complete crawl coverage", "run_id": rid,
                    "reason": f"crawl status is '{status}', not 'done'"})
    pend, appr = b["pending_frontier"], b["approved_unexecuted"]
    if pend or appr:
        parts = ([f"{pend} pending frontier items"] if pend else []) + \
                ([f"{appr} approved actions not yet executed"] if appr else [])
        cnd.append({"item": "complete crawl coverage", "run_id": rid,
                    "reason": "crawl not finished: " + ", ".join(parts)})
    if not any(e["source"] in ("openapi", "graphql") for e in sections["data_model"]) \
            and not any(n == "data_model" for n, _ in got["failed"]):
        warn.append(f"run {rid}: no API schema (OpenAPI/GraphQL) found; the data model is "
                    "inferred from observed traffic")
    return cnd, warn


def _environment(run_dir: Path, rd, could_not: list, warnings: list) -> dict:
    from appmap.profile.sections import environment

    try:
        return environment(run_dir)
    except Exception as e:  # noqa: BLE001 - missing/corrupt run.json
        reason = f"run.json unreadable ({type(e).__name__}); using the directory name"
        could_not.append({"item": "run metadata", "run_id": rd.run_id, "reason": reason})
        warnings.append(f"run {rd.run_id}: {reason}; production status unknown, so observed "
                        "accounts are masked")
        return {"run_id": rd.run_id, "base_urls": [], "hosts": [], "profile": None,
                "started_at": None, "tool_version": None, "contains_production_data": True,
                "metadata_missing": True}


def _technologies_safe(run_dir: Path, rid: str, could_not: list) -> list[dict]:
    try:
        return _technologies(run_dir)
    except Exception as e:  # noqa: BLE001
        could_not.append({"item": "technologies", "run_id": rid,
                          "reason": f"could not read: {type(e).__name__}"})
        return []


def build_profile(run_dirs: list[Path], *, include_observed_accounts: bool = False) -> dict:
    """Assemble the profile for one or more run directories (read-only).

    Top-level section keys describe the first run; with several runs, the same sections for
    the others are under ``other_runs[run_id]`` and ``differences`` compares the runs.
    """
    from appmap.rundir import open_run_dir

    if not run_dirs:
        raise ValueError("at least one run directory is required")
    dirs = [Path(d) for d in run_dirs]
    rds = [open_run_dir(d) for d in dirs]
    for d, rd in zip(dirs, rds):
        if not rd.db_path.is_file():
            raise FileNotFoundError(f"{rd.db_path} not found; is {d} an appmap run directory?")
    could_not, warnings = [], []
    envs = [_environment(d, rd, could_not, warnings) for d, rd in zip(dirs, rds)]
    mask = any(e["contains_production_data"] for e in envs) and not include_observed_accounts

    per_run, facts = [], []
    for d, rd in zip(dirs, rds):
        got = _run_sections(d, rd, mask)
        cnd, warn = _run_gaps(d, rd.run_id, got)
        could_not += cnd
        warnings += warn
        per_run.append(got["sections"])
        facts.append({**got["facts"], "technologies": _technologies_safe(d, rd.run_id, could_not)})

    merged = multirun.merge(facts)
    warnings += merged["warnings"]
    first = per_run[0]
    profile = {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "partial" if could_not else "profiled",
        "warnings": warnings,
        "observed_accounts_masked": mask,
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
