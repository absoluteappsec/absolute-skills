"""Filesystem locations for the plugin's cache, venv and saved sessions."""
import os
from pathlib import Path


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "absolute-appmap"


def venv_dir(version: str) -> Path:
    return cache_dir() / version / "venv"


def session_dir(run_id: str) -> Path:
    return cache_dir() / "sessions" / run_id
