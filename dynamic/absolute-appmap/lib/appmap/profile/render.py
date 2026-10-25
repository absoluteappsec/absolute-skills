"""Render ``APP-PROFILE.md`` from a profile dict with the Jinja2 template."""
from __future__ import annotations

from importlib import resources

CAPS = {"features": 40, "entry_points": 5, "endpoints": 8, "forms": 8, "technologies": 60,
        "areas": 40, "api_groups": 30, "entities": 30, "fields": 15, "preconditions": 30,
        "records": 25, "seed": 20, "roles": 30, "grid": 40, "diff": 25, "could_not": 40}


def _cell(v) -> str:
    s = "" if v is None else str(v)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _cap(items, n: int) -> tuple[list, int]:
    items = list(items or [])
    return items[:n], max(len(items) - n, 0)


def _constraints(f: dict) -> str:
    parts = [f"{k}={v}" for k, v in (f.get("constraints") or {}).items()]
    if f.get("enum"):
        parts.append("enum: " + ", ".join(f["enum"][:8]))
    return "; ".join(parts)


def _context(p: dict) -> dict:
    names = p.get("feature_names") or {}
    feats = []
    for f in p.get("features") or []:
        n = names.get(f["key"]) or {}
        eps, eps_more = _cap(f.get("endpoints"), CAPS["endpoints"])
        routes, routes_more = _cap(f.get("routes"), CAPS["entry_points"])
        forms, forms_more = _cap(f.get("forms"), CAPS["forms"])
        feats.append({**f, "title": n.get("name") or f.get("name_suggestion") or f["key"],
                      "description": n.get("description") or "",
                      "how_it_works": n.get("how_it_works") or "",
                      "routes": routes, "routes_more": routes_more,
                      "endpoints": eps, "endpoints_more": eps_more,
                      "forms": forms, "forms_more": forms_more})
    features, features_more = _cap(feats, CAPS["features"])
    title = {f["key"]: f["title"] for f in feats}

    techs, techs_more = _cap(p.get("technologies"), CAPS["technologies"])
    layout = p.get("layout") or {}
    areas, areas_more = _cap(layout.get("areas"), CAPS["areas"])
    groups, groups_more = _cap(layout.get("api_groups"), CAPS["api_groups"])
    ents = []
    for e in p.get("data_model") or []:
        fields, more = _cap(e.get("fields"), CAPS["fields"])
        ents.append({**e, "fields": [{**f, "constraint_text": _constraints(f)} for f in fields],
                     "fields_more": more})
    entities, entities_more = _cap(ents, CAPS["entities"])

    setup = p.get("data_setup") or {}
    pre, pre_more = _cap(setup.get("preconditions"), CAPS["preconditions"])
    recs, recs_more = _cap(setup.get("created_records"), CAPS["records"])
    seeds, seeds_more = _cap(setup.get("seed_observations"), CAPS["seed"])
    accounts = p.get("accounts") or {}
    roles, roles_more = _cap(accounts.get("roles"), CAPS["roles"])

    cov = p.get("coverage") or {}
    grid_roles = sorted({r for row in (cov.get("features_x_roles") or {}).values() for r in row})
    grid_rows, grid_more = _cap(
        [{"feature": title.get(k, k), "cells": [row.get(r, "") for r in grid_roles]}
         for k, row in (cov.get("features_x_roles") or {}).items()], CAPS["grid"])

    diff = p.get("differences")
    diffs = None
    if diff:
        diffs = {}
        for key in ("technologies", "routes", "endpoints", "role_access"):
            diffs[key], diffs[key + "_more"] = _cap(diff.get(key), CAPS["diff"])
    cnd, cnd_more = _cap(p.get("could_not_determine"), CAPS["could_not"])
    return {
        "p": p, "production": any(e.get("contains_production_data")
                                  for e in p.get("environments") or []),
        "features": features, "features_more": features_more,
        "technologies": techs, "technologies_more": techs_more,
        "layout": layout, "areas": areas, "areas_more": areas_more,
        "api_groups": groups, "api_groups_more": groups_more,
        "entities": entities, "entities_more": entities_more,
        "setup": setup, "preconditions": pre, "preconditions_more": pre_more,
        "records": recs, "records_more": recs_more + (setup.get("created_records_more") or 0),
        "seeds": seeds, "seeds_more": seeds_more,
        "accounts": accounts, "roles": roles, "roles_more": roles_more,
        "grid_roles": grid_roles, "grid_rows": grid_rows, "grid_more": grid_more,
        "coverage": cov, "diffs": diffs,
        "could_not": cnd, "could_not_more": cnd_more,
    }


def _spacing(text: str) -> str:
    """Blank lines around headings, list starts and table ends (the template's block tags
    swallow some), and no runs of more than one blank line."""
    out: list[str] = []
    for line in text.split("\n"):
        prev = out[-1] if out else ""
        starts_list = line.startswith("- ") and not prev.startswith("- ")
        starts_block = line.startswith("#") or starts_list
        ends_table = prev.startswith("|") and not line.startswith("|")
        after_head = prev.startswith("#")
        if prev.strip() and line.strip() and (starts_block or ends_table or after_head):
            out.append("")
        elif not line.strip() and not prev.strip() and out:
            continue
        out.append(line)
    return "\n".join(out)


def render_app_profile(profile: dict) -> str:
    import jinja2

    src = resources.files("appmap").joinpath("templates", "app-profile.md.j2").read_text(
        encoding="utf-8")
    env = jinja2.Environment(autoescape=False, trim_blocks=True, lstrip_blocks=True,
                             keep_trailing_newline=True, undefined=jinja2.StrictUndefined)
    env.filters["cell"] = _cell
    return _spacing(env.from_string(src).render(**_context(profile)))
