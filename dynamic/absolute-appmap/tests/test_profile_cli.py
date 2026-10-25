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
