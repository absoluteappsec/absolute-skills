"""scripts/validate.py: structural checks over the plugin's skills, commands and manifest."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VALIDATE = ROOT / "scripts" / "validate.py"
_IGNORE = shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__", "tests", "docs",
                                 "*.egg-info")


def run_validate(root: Path | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, str(VALIDATE)] + ([str(root)] if root else [])
    return subprocess.run(args, capture_output=True, text=True, timeout=60)


@pytest.fixture
def plugin_copy(tmp_path) -> Path:
    dst = tmp_path / "absolute-appmap"
    shutil.copytree(ROOT, dst, ignore=_IGNORE)
    return dst


def _append(path: Path, text: str) -> None:
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8")


def test_validate_passes_on_repo():
    res = run_validate()
    assert res.returncode == 0, res.stdout + res.stderr
    assert res.stdout.strip().splitlines()[-1] == "OK: validated 8 skill(s) and plugin.json"


def test_validate_passes_on_copy(plugin_copy):
    res = run_validate(plugin_copy)
    assert res.returncode == 0, res.stdout + res.stderr


def test_validate_catches_unknown_subcommand(plugin_copy):
    _append(plugin_copy / "skills" / "crawl-application" / "SKILL.md",
            "\nThen run `scripts/appmap explode --run <run>`.\n")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "explode" in res.stdout


def test_validate_catches_unknown_flag(plugin_copy):
    _append(plugin_copy / "skills" / "crawl-application" / "SKILL.md",
            "\nRun `appmap crawl --run <run> --resume`.\n")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "--resume" in res.stdout


def test_validate_catches_unknown_query_recipe(plugin_copy):
    _append(plugin_copy / "skills" / "query-app-map" / "SKILL.md",
            "\nRun `appmap query --run <run> everything`.\n")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "everything" in res.stdout


def test_validate_catches_mcp_reference(plugin_copy):
    _append(plugin_copy / "skills" / "authenticate-roles" / "SKILL.md",
            "\nUse mcp__playwright__browser_navigate to open the login page.\n")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "mcp__" in res.stdout


def test_validate_catches_schema_drift(plugin_copy):
    _append(plugin_copy / "skills" / "query-app-map" / "references" / "schema.md", "\ndrift\n")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "schema.md" in res.stdout


def test_validate_catches_bad_description_start(plugin_copy):
    p = plugin_copy / "skills" / "build-app-map" / "SKILL.md"
    text = p.read_text(encoding="utf-8")
    p.write_text(text.replace("description: Use ", "description: Helps ", 1), encoding="utf-8")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "Use when" in res.stdout


def test_validate_catches_missing_reference(plugin_copy):
    _append(plugin_copy / "skills" / "probe-endpoints" / "SKILL.md",
            "\n- `references/nowhere.md` — missing.\n")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "references/nowhere.md" in res.stdout


def test_validate_catches_name_mismatch(plugin_copy):
    p = plugin_copy / "skills" / "review-approvals" / "SKILL.md"
    p.write_text(p.read_text(encoding="utf-8").replace("name: review-approvals",
                                                       "name: approvals", 1), encoding="utf-8")
    res = run_validate(plugin_copy)
    assert res.returncode == 1
    assert "review-approvals" in res.stdout
