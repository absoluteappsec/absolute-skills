#!/usr/bin/env bash
# Create the appmap venv and install the package + Chromium. Idempotent.
#   setup.sh        -> cache venv at ${XDG_CACHE_HOME:-~/.cache}/absolute-appmap/<version>/venv
#   setup.sh --dev  -> editable install with dev extras into ./.venv in the plugin dir
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$PLUGIN_DIR/lib/appmap/__init__.py")"

pick_python() {
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

if [ "${1:-}" = "--dev" ]; then
  VENV="$PLUGIN_DIR/.venv"
  [ -x "$VENV/bin/python" ] || "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install --quiet -e "$PLUGIN_DIR[dev]"
else
  BASE="${XDG_CACHE_HOME:-$HOME/.cache}/absolute-appmap"
  VENV="$BASE/$VERSION/venv"
  [ -x "$VENV/bin/python" ] || { mkdir -p "$BASE/$VERSION"; "$PY" -m venv "$VENV"; }
  "$VENV/bin/pip" install --quiet "$PLUGIN_DIR"
fi

"$VENV/bin/python" -m playwright install chromium
echo "appmap environment ready: $VENV"
