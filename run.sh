#!/usr/bin/env bash
# Dev launcher (macOS/Linux/Git-Bash). First run creates a venv + installs deps.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit it before going to prod."
fi

echo "Starting API on http://127.0.0.1:8000 (docs at /docs)"
exec ./.venv/bin/python -m uvicorn app.main:app --reload --port 8000
