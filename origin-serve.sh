#!/usr/bin/env bash
# Serve Origin over the network (view it from your phone / another computer).
#   bash origin-serve.sh            # localhost only
#   bash origin-serve.sh --lan      # your home network (token auto-generated)
#   bash origin-serve.sh --online   # public https URL (needs cloudflared)
set -euo pipefail
cd "$(dirname "$0")"

[ -d .venv ] || { echo "Run 'bash install.sh' first."; exit 1; }
[ -f origin.config.yaml ] || cp origin.config.example.yaml origin.config.yaml
"./.venv/bin/python" -c "from origin.bootstrap import ensure_builtin_model; ensure_builtin_model()" || true

exec "./.venv/bin/python" -m origin.serve "$@"
