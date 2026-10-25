"""Profile acceptance: `appmap profile` on runs built from both fixture apps.

The runs come from the documented CLI sequence (``profile_helpers.ssr_built_run`` /
``spa_built_run``); the profile is produced the way the profile-running-application skill
does it - one subprocess per command, output to ``--out`` so the shared runs stay untouched,
then names filled in ``profile-names.yaml`` and a re-run.
"""
import copy
import json
import re
from pathlib import Path

import pytest
import yaml

from profile_helpers import ISSUED_SECRETS
from test_e2e import PASSWORDS, Agent, _b64_forms

OUTPUTS = ("profile.json", "APP-PROFILE.md", "profile-names.yaml")
REF_KINDS = {"exchange", "state", "file", "route", "endpoint", "flow", "form"}
JUDGMENT = re.compile(r"outdated|vulnerab|\bCVE\b", re.I)


def _profile(run: Path, out: Path, cache: Path) -> tuple[dict, dict, str]:
    st, _ = Agent(cache).run("profile", "--run", str(run), "--out", str(out))
    assert st["command"] == "profile" and st["status"] in ("profiled", "partial")
    return st, json.loads((out / "profile.json").read_text()), (out / "APP-PROFILE.md").read_text()


def _techs(profile: dict) -> dict[str, dict]:
    return {t["name"]: t for t in profile["technologies"]}


def _assert_evidence_refs(profile: dict, run_id: str) -> None:
    for t in profile["technologies"]:
        assert t["evidence"], t["name"]
        for ev in t["evidence"]:
            ref = ev["ref"]
            assert set(ref) == {"run_id", "kind", "id"}, ref
            assert ref["run_id"] == run_id and ref["kind"] in REF_KINDS
            assert len(ev["snippet"]) <= 200


def _assert_clean(out: Path, run: Path) -> None:
    """No fixture password, sid, CSRF token or JWT (verbatim or base64 at any alignment)
    and no judgment wording in any profile output."""
    secrets = ISSUED_SECRETS[run]
    assert secrets, "no session secret was captured from the fixture"
    raw = {s.encode() for s in set(PASSWORDS) | secrets if s}
    needles = {n: n for n in raw}
    for n in raw:
        for form in _b64_forms(n):
            needles[form] = n
    hits = []
    for name in OUTPUTS:
        data = (out / name).read_bytes()
        hits += [f"{name}: {n[:12]!r}... as {form[:16]!r}"
                 for form, n in needles.items() if form in data]
        assert not JUDGMENT.search(data.decode()), name
    assert not hits, hits


@pytest.fixture(scope="module")
def ssr(ssr_built_run, tmp_path_factory):
    out = tmp_path_factory.mktemp("ssr-profile")
    st, profile, md = _profile(ssr_built_run, out, tmp_path_factory.mktemp("cache"))
    return {"run": ssr_built_run, "out": out, "status": st, "profile": profile, "md": md}


@pytest.fixture(scope="module")
def spa(spa_built_run, tmp_path_factory):
    out = tmp_path_factory.mktemp("spa-profile")
    st, profile, md = _profile(spa_built_run, out, tmp_path_factory.mktemp("cache"))
    return {"run": spa_built_run, "out": out, "status": st, "profile": profile, "md": md}


def test_ssr_technologies(ssr):
    techs = _techs(ssr["profile"])
    assert {"Werkzeug", "Flask"} & set(techs), sorted(techs)
    python = techs["Python"]
    assert python["category"] == "language"
    # language is inferred (likely) unless a header names it outright (Werkzeug's Server
    # header carries "Python/x.y.z", which is authoritative -> confirmed)
    assert python["confidence"] == "likely" or any(
        e["kind"] == "header" and "Python/" in e["snippet"] for e in python["evidence"])
    assert techs["Cookie session"]["category"] == "auth"
    assert techs["CSRF token"]["category"] == "auth"
    assert "JWT bearer" not in techs
    _assert_evidence_refs(ssr["profile"], ssr["profile"]["environments"][0]["run_id"])
    assert "| Python | language |" in ssr["md"]


def test_spa_technologies(spa):
    techs = _techs(spa["profile"])
    assert techs["Vue"]["versions"] == ["3.4.38"] and techs["Vue"]["confidence"] == "confirmed"
    assert techs["vue-router"]["versions"] == ["4.4.3"]
    assert techs["vue-router"]["confidence"] == "confirmed"
    assert techs["JWT bearer"]["category"] == "auth"
    assert techs["GraphQL"]["category"] == "api_style"
    assert "Cookie session" not in techs
    _assert_evidence_refs(spa["profile"], spa["profile"]["environments"][0]["run_id"])
    assert "| Vue | frontend_framework | 3.4.38 | confirmed |" in spa["md"]


def _assert_sub_blocks(p: dict, md: str) -> None:
    """Blocks the template guards with .get() render whenever profile.json has the data,
    and only then - so a key typo in the template fails."""
    eps = p["layout"].get("entry_points")
    assert ("Entry points by role:" in md) == bool(eps)
    for role in eps or {}:
        assert f"- {role}: `" in md
    accounts = p["accounts"]
    assert ("Accounts created during the run:" in md) == bool(accounts["created_by_run"])
    for a in accounts["created_by_run"][:25]:
        assert f"`{a['username'] or a['email']}` via `{a['endpoint']}`" in md
    assert ("Identities observed in responses" in md) == bool(accounts["observed"])
    for o in accounts["observed"][:25]:
        assert f"- `{o['value']}`" in md
    assert ("Not exercised by the run:" in md) == bool(p["coverage"].get("unexercised"))
    for k, v in (p["coverage"].get("unexercised") or {}).items():
        assert f"{k.replace('_', ' ')} {v}" in md
    appr = p["coverage"]["approvals"]
    assert (f"Approval queue: {appr['pending']} pending, {appr['denied']} denied, "
            f"{appr['skipped']} skipped.") in md
    assert ("Form fixtures (`fixtures.yaml`) were used for this run." in md) == \
        bool(p["data_setup"]["fixtures_used"])
    assert ("Records created by the run:" in md) == bool(p["data_setup"]["created_records"])


def test_rendered_sub_blocks_ssr(ssr):
    p = ssr["profile"]
    assert p["layout"]["entry_points"] and p["coverage"]["unexercised"]
    assert p["data_setup"]["created_records"]
    _assert_sub_blocks(p, ssr["md"])


def test_rendered_sub_blocks_spa(spa):
    p = spa["profile"]
    assert p["accounts"]["observed"]  # /api/me style responses name the signed-in user
    _assert_sub_blocks(p, spa["md"])


def test_rendered_sub_blocks_absent_in_fixture_runs(ssr):
    """Created accounts and the fixtures line do not occur in the fixture runs: render the
    SSR profile with them added (and with the other blocks emptied) to cover both ways."""
    from appmap.profile.render import render_app_profile

    p = copy.deepcopy(ssr["profile"])
    p["accounts"]["created_by_run"] = [{"exchange_id": 1, "endpoint": "/register",
                                        "username": "appmap-test-1", "email": None,
                                        "ref": {"run_id": "r", "kind": "exchange", "id": 1}}]
    p["data_setup"]["fixtures_used"] = True
    _assert_sub_blocks(p, render_app_profile(p))
    p["accounts"]["created_by_run"] = []
    p["data_setup"]["fixtures_used"] = False
    p["layout"]["entry_points"] = {}
    p["coverage"]["unexercised"] = {}
    _assert_sub_blocks(p, render_app_profile(p))


def test_no_secrets_or_judgment_in_outputs(ssr, spa):
    for r in (ssr, spa):
        _assert_clean(r["out"], r["run"])


def test_skill_flow_names_round_trip(ssr, tmp_path_factory):
    """Skill steps 4-5: fill profile-names.yaml, re-run, the prose lands in the outputs."""
    out = ssr["out"]
    names = yaml.safe_load((out / "profile-names.yaml").read_text())
    assert set(names) == {"purpose", "features", "stale"}
    assert ssr["status"]["next"] and "profile-names.yaml" in ssr["status"]["next"]
    names["purpose"] = "Project tracker for a small team (states 1, 2)."
    for i, f in enumerate(names["features"]):
        assert set(f) >= {"key", "name", "suggestion", "description", "how_it_works"}
        f["name"] = f"Feature {i}"
        f["description"] = f"Described feature {i}."
        f["how_it_works"] = f"Step one, then step two (exchange {i})."
    (out / "profile-names.yaml").write_text(yaml.safe_dump(names, sort_keys=False))
    st, profile, md = _profile(ssr["run"], out, tmp_path_factory.mktemp("cache"))
    assert st["next"] is None
    assert profile["purpose"] == names["purpose"]
    assert len(profile["feature_names"]) == len(names["features"])
    assert names["purpose"] in md
    assert "### Feature 0" in md and "Step one, then step two (exchange 0)." in md
    _assert_clean(out, ssr["run"])
