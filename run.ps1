# One-command setup + run on Windows (PowerShell). Opens http://127.0.0.1:8000
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path "backend\.venv")) { python -m venv backend\.venv }
backend\.venv\Scripts\python.exe -m pip install -q -r backend\requirements.txt
if (-not (Test-Path "frontend\dist\index.html")) {
    Push-Location frontend; npm install --no-audit --no-fund; npm run build; Pop-Location
}
if (-not (Test-Path ".env")) { Copy-Item .env.example .env }
backend\.venv\Scripts\python.exe -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
