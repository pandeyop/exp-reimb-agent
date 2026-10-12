# Expense & Reimbursement Agent — single-service image (FastAPI serves the API and the built React UI).
# Built and run by Render as one free Web Service; also runs locally with `docker run -p 8000:8000`.

# ---- Stage 1: build the React/Vite frontend ------------------------------------------------------
FROM node:20-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python runtime ---------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Public demo defaults: no API key needed or consumed. Set EXTRACTION_MODE=auto and
    # ANTHROPIC_API_KEY as server-side environment variables to enable live Claude extraction.
    EXTRACTION_MODE=offline \
    # Writable, ephemeral storage for SQLite and uploads (Render's free tier has no persistent disk).
    DATA_DIR=/tmp/expense-agent \
    # Keeps the demo's 60-day/future-date rules deterministic for the bundled September 2026 receipts.
    REFERENCE_DATE=2026-10-07 \
    PORT=8000

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install -r backend/requirements.txt

# Same layout as the repository, so settings.py resolves every path without overrides:
#   /app/backend/app, /app/backend/config, /app/demo_receipts (+ fixtures.json), /app/frontend/dist
COPY backend/app backend/app
COPY backend/config backend/config
COPY demo_receipts demo_receipts
COPY --from=frontend /build/frontend/dist frontend/dist

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p "$DATA_DIR" && chown -R appuser "$DATA_DIR"
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/api/health', timeout=4)"

# One worker only: document processing uses an in-process thread pool and lock, and SQLite is local.
CMD ["sh", "-c", "exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
