import json
from datetime import datetime

from appmap.rundir import create_run_dir, open_run_dir, write_run_meta

NOW = datetime(2026, 10, 24, 9, 5, 1)


def test_run_dir_name(tmp_path, cfg):
    rd = create_run_dir(cfg, tmp_path, NOW)
    assert rd.root.name == "appmap-staging.example.com-20261024-090501"
    assert rd.run_id == "20261024-090501"
    assert rd.traffic_dir.is_dir() and rd.artifacts_dir.is_dir() and rd.bodies_dir.is_dir()
    assert rd.events_path.parent == rd.root


def test_open_run_dir_reads_run_id(tmp_path, cfg):
    rd = create_run_dir(cfg, tmp_path, NOW)
    write_run_meta(rd, cfg, status="running")
    again = open_run_dir(rd.root)
    assert again.run_id == rd.run_id and again.db_path == rd.db_path


def test_run_json_flags_production(tmp_path, prod_cfg):
    rd = create_run_dir(prod_cfg, tmp_path, NOW)
    write_run_meta(rd, prod_cfg, status="running")
    meta = json.load(open(rd.root / "run.json"))
    assert meta["contains_production_data"] is True
    assert meta["profile"] == "production"


def test_run_json_contents_and_no_secrets(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("APPMAP_USER_PASS", "hunter2")
    rd = create_run_dir(cfg, tmp_path, NOW)
    write_run_meta(rd, cfg, status="complete", extra={"note": "x"})
    text = (rd.root / "run.json").read_text()
    meta = json.loads(text)
    assert "hunter2" not in text
    assert meta["contains_production_data"] is False
    assert meta["status"] == "complete" and meta["note"] == "x"
    assert meta["run_id"] == rd.run_id
    assert meta["schema_version"] == "1.0.0" and meta["tool_version"]
    assert meta["config"]["base_urls"] == cfg.base_urls
    assert meta["config"]["roles"][0]["login"]["password_env"] == "APPMAP_USER_PASS"
    assert "authorization" in meta and "roles" in meta
    assert "started_at" in meta and "updated_at" in meta
    write_run_meta(rd, cfg, status="complete")
    assert json.loads((rd.root / "run.json").read_text())["started_at"] == meta["started_at"]
