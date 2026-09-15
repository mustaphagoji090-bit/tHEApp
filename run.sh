#!/usr/bin/env bash
# Start the app. First run creates a virtualenv and installs dependencies.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PORT="${PORT:-8000}"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  "$PYTHON" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
  echo "Dependencies installed."
fi

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "Created .env -- open it and paste in your API keys, then run this script again."
  exit 1
fi

echo "Starting on http://127.0.0.1:${PORT}"
exec ./.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"
