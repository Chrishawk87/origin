#!/usr/bin/env bash
# One-command installer for Origin (macOS & Linux servers).
# Sets up a free local brain (Ollama), the browser, and all dependencies.
set -euo pipefail
cd "$(dirname "$0")"

echo "◍  Installing Origin…"
echo

# 1) Python 3.10+
if ! command -v python3 >/dev/null 2>&1; then
  echo "✗ Python 3 not found. Install Python 3.10+ from https://www.python.org/downloads/ and re-run."
  exit 1
fi
echo "✓ Python: $(python3 --version)"

# 2) Virtual env + dependencies
echo "→ Creating environment and installing dependencies (this takes a few minutes)…"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt
echo "✓ Dependencies installed"

# 3) Browser for click-and-retrieve
echo "→ Installing the browser (Chromium)…"
./.venv/bin/python -m playwright install chromium || echo "  (skipped — you can run 'playwright install chromium' later)"

# 4) Config (self-contained built-in brain by default)
if [ ! -f origin.config.yaml ]; then
  cp origin.config.example.yaml origin.config.yaml
  echo "✓ Created origin.config.yaml (coordinator = builtin llama.cpp — self-contained)"
fi

# 5) Download the built-in model (one-time, ~1.9GB) so first chat is instant
echo "→ Fetching the built-in AI model…"
./.venv/bin/python -c "from origin.bootstrap import ensure_builtin_model; ensure_builtin_model()" || \
  echo "  (model will download on first launch if this didn't complete)"

# 6) Health check
./.venv/bin/python -m origin --doctor || true

echo
echo "──────────────────────────────────────────────"
echo "  Done. Start Origin:"
echo "    ./origin-app.sh          # desktop app window"
echo "    ./.venv/bin/python -m origin   # terminal version"
echo "──────────────────────────────────────────────"
