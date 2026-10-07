#!/usr/bin/env bash
# One-command setup + run on macOS/Linux. Opens http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"
[ -d backend/.venv ] || python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install -q -r backend/requirements.txt
if [ ! -f frontend/dist/index.html ]; then (cd frontend && npm install --no-audit --no-fund && npm run build); fi
[ -f .env ] || cp .env.example .env
exec backend/.venv/bin/python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
