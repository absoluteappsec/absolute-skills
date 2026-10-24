"""Run directory layout and run.json metadata."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from appmap import SCHEMA_VERSION, __version__
from appmap.config import RunConfig


@dataclass
class RunDir:
    root: Path
    run_id: str
    events_path: Path
    traffic_dir: Path
    artifacts_dir: Path
    bodies_dir: Path
    db_path: Path
    approvals_path: Path
    json_path: Path
    map_md_path: Path
    schema_md_path: Path
    log_path: Path


def _layout(root: Path, run_id: str) -> RunDir:
    return RunDir(
        root=root, run_id=run_id,
        events_path=root / "events.jsonl", traffic_dir=root / "traffic",
        artifacts_dir=root / "artifacts", bodies_dir=root / "bodies",
        db_path=root / "appmap.sqlite", approvals_path=root / "approvals.yaml",
        json_path=root / "appmap.json", map_md_path=root / "APP-MAP.md",
        schema_md_path=root / "SCHEMA.md", log_path=root / "crawl.log",
    )


def create_run_dir(cfg: RunConfig, parent: Path, now: datetime | None = None) -> RunDir:
    now = now or datetime.now()
    run_id = now.strftime("%Y%m%d-%H%M%S")
    host = (urlparse(cfg.base_urls[0]).hostname or "target").lower()
    rd = _layout(Path(parent) / f"appmap-{host}-{run_id}", run_id)
    for d in (rd.root, rd.traffic_dir, rd.artifacts_dir, rd.bodies_dir):
        d.mkdir(parents=True, exist_ok=True)
    return rd


def open_run_dir(path: Path) -> RunDir:
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"run directory not found: {root}")
    run_id = None
    try:
        run_id = json.loads((root / "run.json").read_text()).get("run_id")
    except (OSError, ValueError):
        pass
    if not run_id:  # fall back to the dir-name suffix YYYYMMDD-HHMMSS
        run_id = "-".join(root.name.rsplit("-", 2)[-2:])
    return _layout(root, run_id)


def write_run_meta(rd: RunDir, cfg: RunConfig, *, status: str,
                   extra: dict | None = None) -> None:
    path = rd.root / "run.json"
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    started = now
    try:
        started = json.loads(path.read_text()).get("started_at", now)
    except (OSError, ValueError):
        pass
    cfg_dict = cfg.to_redacted_dict()  # env var NAMES only, never values
    meta = {
        "run_id": rd.run_id,
        "tool_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "profile": cfg.profile,
        "contains_production_data": cfg.profile == "production",
        "authorization": cfg_dict["authorization"],
        "roles": [r.name for r in cfg.roles],
        "config": cfg_dict,
        "started_at": started,
        "updated_at": now,
        "status": status,
        **(extra or {}),
    }
    path.write_text(json.dumps(meta, indent=2, default=str) + "\n")
