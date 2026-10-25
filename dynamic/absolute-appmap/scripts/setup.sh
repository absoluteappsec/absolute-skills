#!/usr/bin/env bash
# Create the appmap venv and install the package + Chromium. Idempotent.
#   setup.sh        -> cache venv at ${XDG_CACHE_HOME:-~/.cache}/absolute-appmap/<version>/venv
#   setup.sh --dev  -> editable install with dev extras into ./.venv in the plugin dir
# APPMAP_PYTHON=/path/to/python3.x overrides the interpreter choice. The venv gets a
# .appmap-ready sentinel only once everything (package + Chromium) is installed, so an
# interrupted setup is resumed by the wrapper's next call.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PLUGIN_DIR/lib/appmap/__init__.py")"

pick_python() {
  if [ -n "${APPMAP_PYTHON:-}" ]; then echo "$APPMAP_PYTHON"; return; fi
  # Prefer 3.12/3.11 when the default python3 is too new (>=3.14) for playwright wheels.
  local default_minor
  default_minor="$(python3 -c 'import sys; print(sys.version_info.minor)' 2>/dev/null || echo 0)"
  if [ "$default_minor" -ge 14 ] || [ "$default_minor" -lt 11 ]; then
    for c in python3.12 python3.11 /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11; do
      if command -v "$c" >/dev/null 2>&1; then echo "$c"; return; fi
    done
  fi
  echo python3
}

PY="$(pick_python)"
if ! "$PY" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 11) else 1)' 2>/dev/null; then
  found="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "none")"
  echo "appmap setup: Python 3.11 or newer is required (found: $PY -> $found)." >&2
  echo "Install python3.12 (or 3.11), or set APPMAP_PYTHON=/path/to/python3.12." >&2
  exit 1
fi

if [ "${1:-}" = "--dev" ]; then
  VENV="$PLUGIN_DIR/.venv"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet -e "$PLUGIN_DIR[dev]"
else
  BASE="${XDG_CACHE_HOME:-$HOME/.cache}/absolute-appmap"
  VENV="$BASE/$VERSION/venv"
  [ -x "$VENV/bin/python" ] || { mkdir -p "$BASE/$VERSION"; "$PY" -m venv "$VENV"; }
  rm -f "$VENV/.appmap-ready"
  "$VENV/bin/pip" install --quiet "$PLUGIN_DIR"
  # the in-tree build leaves build/ and lib/*.egg-info behind: both are git-ignored
fi

"$VENV/bin/python" -m playwright install chromium
touch "$VENV/.appmap-ready"
echo "appmap environment ready: $VENV"
