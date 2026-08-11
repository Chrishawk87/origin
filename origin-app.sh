#!/usr/bin/env bash
# Launch the Origin desktop app (native window). Sets up on first run.
set -euo pipefail
cd "$(dirname "$0")"

VENV=".venv"
if [ ! -d "$VENV" ]; then
  echo "Setting up Origin (first run — this takes a few minutes)…"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -r requirements.txt
  echo "Installing browser for click-and-retrieve…"
  "$VENV/bin/python" -m playwright install chromium || true
fi

# Self-contained brain: config + model, both idempotent
[ -f origin.config.yaml ] || cp origin.config.example.yaml origin.config.yaml
"$VENV/bin/python" -c "from origin.bootstrap import ensure_builtin_model; ensure_builtin_model()" || true

exec "$VENV/bin/python" run_app.py "$@"
