#!/usr/bin/env bash
# Convenience launcher: creates/uses a local venv and runs the hub.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -d "$VENV" ]; then
  echo "Creating virtual environment…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r requirements.txt
fi

exec "$VENV/bin/python" -m origin "$@"
