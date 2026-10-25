import json
import shutil
import sqlite3
import stat

import yaml

from appmap.cli import main

SECTIONS = ["## Purpose", "## Main features", "## How it works", "## Technology stack",
            "## Layout", "## Data model & types", "## Data setup", "## Test accounts",
            "## Environments", "## Coverage orientation", "## Could not determine",
            "## Where to dig deeper"]


def _profile(capsys, *argv) -> tuple[int, dict]:
    rc = main(["profile", *argv])
    last = capsys.readouterr().out.strip().splitlines()[-1]
    assert last.startswith("APPMAP_STATUS ")
    return rc, json.loads(last.removeprefix("APPMAP_STATUS "))


def _copy_run(src, dst, *, production=False, identity=None):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("profile*", "APP-PROFILE.md"))
    if production:
        meta = json.loads((dst / "run.json").read_text())
        meta["contains_production_data"] = True
        (dst / "run.json").write_text(json.dumps(meta))
    if identity:
        con = sqlite3.connect(dst / "appmap.sqlite")
        con.row_factory = sqlite3.Row
        row = dict(con.execute("SELECT * FROM exchanges WHERE status=200 LIMIT 1").fetchone())
        row.pop("id")
        row.update(content_type="application/json", resp_body_path=None,
                   resp_body=json.dumps({"users": [{"email": identity}]}))
        cols = ",".join(row)
        con.execute(f"INSERT INTO exchanges ({cols}) VALUES ({','.join('?' * len(row))})",
                    list(row.values()))
        con.commit()
        con.close()
    return dst


def test_profile_ssr_run(ssr_built_run, tmp_path, capsys):
    run = _copy_run(ssr_built_run, tmp_path / "run")
    rc, st = _profile(capsys, "--run", str(run))
    assert rc == 0 and st["ok"] and st["command"] == "profile"
    assert st["status"] in ("profiled", "partial")
    assert st["counts"]["environments"] == 1
    assert st["counts"]["technologies"] > 0 and st["counts"]["features"] > 0
    assert st["counts"]["entities"] > 0
    assert "profile-names.yaml" in st["next"] and "re-run" in st["next"]
    for f in ("profile.json", "profile-names.yaml", "APP-PROFILE.md"):
        assert (run / f).is_file()
    assert not list(run.glob("*.tmp"))
    assert json.loads((run / "profile.json").read_text())["profile_schema_version"] == "1.0.0"
    md = (run / "APP-PROFILE.md").read_text()
    pos = [md.index(s) for s in SECTIONS]
    assert pos == sorted(pos)
    assert "Handling notice" not in md
    low = md.lower()
    for word in ("outdated", "vulnerab", "cve", "risk"):
        assert word not in low


def test_profile_two_runs_environments(ssr_built_run, tmp_path, capsys):
    a = _copy_run(ssr_built_run, tmp_path / "a")
    b = _copy_run(ssr_built_run, tmp_path / "b")
    rc, st = _profile(capsys, "--run", str(a), "--run", str(b), "--out", str(tmp_path / "out"))
    assert rc == 0 and st["counts"]["environments"] == 2
    assert (tmp_path / "out" / "APP-PROFILE.md").is_file()
    assert not (a / "APP-PROFILE.md").exists()
    assert "--out" in st["next"]


def test_profile_missing_sqlite_is_error(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    rc, st = _profile(capsys, "--run", str(tmp_path / "empty"))
    assert rc == 1 and not st["ok"] and st["status"] == "error"
    assert "appmap.sqlite" in st["errors"][0]


def test_profile_unwritable_out_dir_is_error(ssr_built_run, tmp_path, capsys):
    run = _copy_run(ssr_built_run, tmp_path / "run")
    run.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        rc, st = _profile(capsys, "--run", str(run))
    finally:
        run.chmod(stat.S_IRWXU)
    assert rc == 1 and not st["ok"] and st["status"] == "error"


def test_profile_does_not_modify_inputs(ssr_built_run, tmp_path, capsys):
    run = _copy_run(ssr_built_run, tmp_path / "run")
    before = (run / "appmap.sqlite").read_bytes()
    _profile(capsys, "--run", str(run))
    assert (run / "appmap.sqlite").read_bytes() == before


def test_production_notice_and_account_masking(ssr_built_run, tmp_path, capsys):
    run = _copy_run(ssr_built_run, tmp_path / "run", production=True,
                    identity="jane.doe@corp.example")
    rc, st = _profile(capsys, "--run", str(run))
    assert rc == 0
    md = (run / "APP-PROFILE.md").read_text()
    assert "Handling notice" in md and md.index("Handling notice") < md.index("## Purpose")
    assert "jane.doe@corp.example" not in md and "j***@corp.example" in md
    assert "jane.doe@corp.example" not in (run / "profile.json").read_text()

    rc, _ = _profile(capsys, "--run", str(run), "--include-observed-accounts")
    assert rc == 0
    assert "jane.doe@corp.example" in (run / "APP-PROFILE.md").read_text()


def test_rerun_keeps_names(ssr_built_run, tmp_path, capsys):
    run = _copy_run(ssr_built_run, tmp_path / "run")
    _profile(capsys, "--run", str(run))
    names = run / "profile-names.yaml"
    doc = yaml.safe_load(names.read_text())
    doc["purpose"] = "A project tracker for teams."
    doc["features"][0]["name"] = "Renamed Feature"
    doc["features"][0]["description"] = "Does a distinctive thing."
    names.write_text(yaml.safe_dump(doc))
    rc, st = _profile(capsys, "--run", str(run))
    assert rc == 0
    md = (run / "APP-PROFILE.md").read_text()
    assert "A project tracker for teams." in md
    assert "### Renamed Feature" in md and "Does a distinctive thing." in md
    assert yaml.safe_load(names.read_text())["features"][0]["name"] == "Renamed Feature"
    assert json.loads((run / "profile.json").read_text())["purpose"] == \
        "A project tracker for teams."


def test_names_emit_is_atomic(ssr_built_run, tmp_path, capsys, monkeypatch):
    import os

    from appmap.profile import names

    run = _copy_run(ssr_built_run, tmp_path / "run")
    _profile(capsys, "--run", str(run))
    path = run / "profile-names.yaml"
    path.write_text("purpose: keep me\nfeatures: []\nstale: []\n")

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    profile = {"features": [{"key": "ft-x", "name_suggestion": "X"}]}
    try:
        names.emit_profile_names(profile, path)
    except OSError:
        pass
    assert path.read_text() == "purpose: keep me\nfeatures: []\nstale: []\n"
    assert not list(run.glob("*.tmp"))


def test_multi_run_notice_and_differences(ssr_built_run, tmp_path, capsys):
    a = _copy_run(ssr_built_run, tmp_path / "a")
    b = _copy_run(ssr_built_run, tmp_path / "b")
    meta = json.loads((b / "run.json").read_text())
    meta["run_id"] = "second-env"
    (b / "run.json").write_text(json.dumps(meta))
    con = sqlite3.connect(b / "appmap.sqlite")
    con.execute("INSERT INTO routes (pattern, host, kind) VALUES ('/only-in-b', 'h', 'page')")
    con.execute("INSERT INTO endpoints (method, host, path_pattern, kind) "
                "VALUES ('GET', 'h', '/api/only-in-b', 'rest')")
    con.execute("UPDATE exchanges SET resp_headers = replace(resp_headers, "
                "'Werkzeug/3.1.9', 'Werkzeug/9.9.9')")
    con.execute("UPDATE role_access SET outcome='forbidden' WHERE role='alice' "
                "AND item_kind='route' AND rowid=(SELECT min(rowid) FROM role_access "
                "WHERE role='alice' AND item_kind='route' AND outcome='allowed')")
    con.commit()
    con.close()
    rc, st = _profile(capsys, "--run", str(a), "--run", str(b))
    assert rc == 0 and st["counts"]["environments"] == 2
    md = (a / "APP-PROFILE.md").read_text()
    first = json.loads((a / "run.json").read_text())["run_id"]
    notice = md[:md.index("## Purpose")]
    assert "2 environments" in notice and first in notice and "other_runs" in notice
    env = md[md.index("## Environments"):md.index("## Coverage orientation")]
    assert f"| `{first}` |" in env and "| `second-env` |" in env
    assert "Route `/only-in-b` only in second-env" in env
    assert "Endpoint `GET /api/only-in-b` only in second-env" in env
    assert "Technology Werkzeug" in env and "9.9.9" in env
    assert "Role alice" in env


def test_partial_profile_renders(ssr_built_run, tmp_path, capsys):
    run = _copy_run(ssr_built_run, tmp_path / "run")
    (run / "run.json").write_text("{not json")
    con = sqlite3.connect(run / "appmap.sqlite")
    con.execute("DROP TABLE forms")
    con.commit()
    con.close()
    rc, st = _profile(capsys, "--run", str(run))
    assert rc == 0 and st["status"] == "partial"
    md = (run / "APP-PROFILE.md").read_text()
    assert md.index("## Could not determine") < md.index("## Where to dig deeper")
    assert "run metadata" in md and "could not read" in md


def test_masked_label_only_when_masked(ssr_built_run, tmp_path, capsys):
    prod = _copy_run(ssr_built_run, tmp_path / "prod", production=True,
                     identity="jane.doe@corp.example")
    _profile(capsys, "--run", str(prod))
    assert "(masked)" in (prod / "APP-PROFILE.md").read_text()
    _profile(capsys, "--run", str(prod), "--include-observed-accounts")
    md = (prod / "APP-PROFILE.md").read_text()
    assert "jane.doe@corp.example" in md and "(masked)" not in md


def test_next_roundtrips_paths_with_spaces(ssr_built_run, tmp_path, capsys):
    import shlex

    run = _copy_run(ssr_built_run, tmp_path / "my run dir")
    out = tmp_path / "out dir"
    rc, st = _profile(capsys, "--run", str(run), "--out", str(out))
    words = shlex.split(st["next"])
    assert str(out / "profile-names.yaml") in words
    assert words[words.index("appmap"):][:4] == ["appmap", "profile", "--run", str(run)]
    assert words[-2:] == ["--out", str(out)]


def test_next_stays_until_features_are_judged(ssr_built_run, tmp_path, capsys):
    """Pre-filled suggestion names do not count: next stays until every feature has a
    non-suggestion name or a description."""
    run = _copy_run(ssr_built_run, tmp_path / "run")
    _profile(capsys, "--run", str(run))
    names = run / "profile-names.yaml"
    doc = yaml.safe_load(names.read_text())
    doc["purpose"] = "A project tracker."
    names.write_text(yaml.safe_dump(doc, sort_keys=False))
    _, st = _profile(capsys, "--run", str(run))
    assert st["next"] and "profile-names.yaml" in st["next"]
    for i, f in enumerate(doc["features"]):
        if i % 2:
            f["name"] = f"Judged {i}"           # renamed, no description
        else:
            f["description"] = f"About {i}."    # suggestion kept, but described
    names.write_text(yaml.safe_dump(doc, sort_keys=False))
    _, st = _profile(capsys, "--run", str(run))
    assert st["next"] is None


def test_technology_overrides_downgrade_only(ssr_built_run, tmp_path, capsys):
    run = _copy_run(ssr_built_run, tmp_path / "run")
    _profile(capsys, "--run", str(run))
    names = run / "profile-names.yaml"
    doc = yaml.safe_load(names.read_text())
    assert doc["technology_overrides"] == []
    techs = {t["name"]: t for t in json.loads((run / "profile.json").read_text())
             ["technologies"]}
    assert techs["Werkzeug"]["confidence"] == "confirmed"
    assert techs["CSRF token"]["confidence"] == "likely"
    doc["technology_overrides"] = [
        {"name": "Werkzeug", "category": "app_server", "confidence": "likely",
         "reason": "only the dev-server header seen [exchange 1]"},
        {"name": "CSRF token", "category": "auth", "confidence": "confirmed",
         "reason": "trying to upgrade"},
        {"name": "Nonexistent", "category": "auth", "confidence": "guessed", "reason": "x"},
        {"name": "Python", "confidence": "certain", "reason": "bad level"},
    ]
    names.write_text(yaml.safe_dump(doc, sort_keys=False))
    rc, st = _profile(capsys, "--run", str(run))
    assert rc == 0
    errs = " ".join(st["errors"])
    assert "CSRF token" in errs and "upgrade" in errs
    assert "Nonexistent" in errs and "certain" in errs
    techs = {t["name"]: t for t in json.loads((run / "profile.json").read_text())
             ["technologies"]}
    w = techs["Werkzeug"]
    assert w["confidence"] == "likely" and w["detected_confidence"] == "confirmed"
    assert w["confidence_reason"].startswith("only the dev-server header")
    assert techs["CSRF token"]["confidence"] == "likely"
    assert "detected_confidence" not in techs["CSRF token"]
    assert techs["Python"]["confidence"] == "confirmed"
    md = (run / "APP-PROFILE.md").read_text()
    row = next(ln for ln in md.splitlines() if ln.startswith("| Werkzeug |"))
    assert "likely (detected: confirmed; only the dev-server header seen [exchange 1])" in row
    # the overrides survive a re-run of the names emit
    assert yaml.safe_load(names.read_text())["technology_overrides"][0]["name"] == "Werkzeug"
