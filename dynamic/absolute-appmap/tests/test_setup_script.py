"""M5: scripts/setup.sh refuses a too-old Python with a clear message; the wrapper re-runs
setup until the venv's .appmap-ready sentinel exists."""
import os
import stat
import subprocess
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]


def _fake_python(tmp_path: Path, minor: int) -> Path:
    exe = tmp_path / "python3-fake"
    # A stand-in interpreter: answers the version probes like Python 3.<minor>.
    exe.write_text("#!/usr/bin/env bash\n"
                   'case "$*" in\n'
                   f'  *"sys.exit(0 if"*) exit {0 if minor >= 11 else 1} ;;\n'
                   f'  *"print("*) echo "3.{minor}" ;;\n'
                   "  *) exit 3 ;;\n"
                   "esac\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def test_setup_rejects_old_python(tmp_path):
    py = _fake_python(tmp_path, 10)
    proc = subprocess.run(["bash", str(PLUGIN / "scripts" / "setup.sh")], capture_output=True,
                          text=True, timeout=30,
                          env={**os.environ, "APPMAP_PYTHON": str(py),
                               "XDG_CACHE_HOME": str(tmp_path / "cache")})
    assert proc.returncode == 1
    assert "Python 3.11 or newer is required" in proc.stderr and "3.10" in proc.stderr
    assert not (tmp_path / "cache" / "absolute-appmap").exists()


def test_wrapper_reruns_setup_without_sentinel():
    text = (PLUGIN / "scripts" / "appmap").read_text()
    assert ".appmap-ready" in text
    setup = (PLUGIN / "scripts" / "setup.sh").read_text()
    # the sentinel is written only after the last install step
    assert setup.rstrip().splitlines()[-2].startswith('touch "$VENV/.appmap-ready"')
