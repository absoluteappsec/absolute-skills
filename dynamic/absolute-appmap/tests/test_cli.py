import json

from appmap import cli
from appmap.cli import build_parser, emit_status, main
from appmap.paths import cache_dir, session_dir, venv_dir


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert "0.1.0" in capsys.readouterr().out


def test_emit_status_is_last_line_json(capsys):
    emit_status("status", ok=True, status="done", counts={"states": 3})
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert line.startswith("APPMAP_STATUS ")
    obj = json.loads(line.removeprefix("APPMAP_STATUS "))
    assert set(obj) == {"command", "ok", "status", "counts", "pending_approvals", "errors", "next"}


def test_cache_dir_honors_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert cache_dir() == tmp_path / "absolute-appmap"
    assert venv_dir("0.1.0") == tmp_path / "absolute-appmap" / "0.1.0" / "venv"
    assert session_dir("r1") == tmp_path / "absolute-appmap" / "sessions" / "r1"


def test_subcommands_registered_with_handlers():
    names = {"init", "auth", "crawl", "probe", "approvals", "build", "status", "query"}
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    assert set(sub.choices) == names == set(cli.HANDLERS)
    assert cli.LOCKED <= names
