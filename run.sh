#!/usr/bin/env bash
# RAT startup script: prepares the virtualenv and launches the FastAPI app
# (JSON API + static dashboard) on http://localhost:8000.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"
PORT="${PORT:-8000}"

if [ ! -d "venv" ]; then
  echo "[rat] creating virtualenv (venv/)"
  "$PYTHON_BIN" -m venv venv
fi

echo "[rat] installing dependencies"
./venv/bin/pip install --disable-pip-version-check -q -r requirements.txt

echo "[rat] starting server on http://localhost:${PORT}"
exec ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
