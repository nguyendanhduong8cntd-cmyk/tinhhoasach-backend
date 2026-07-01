# Dev launcher (Windows PowerShell). First run creates a venv + installs deps.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv")) {
    Write-Host "Creating virtualenv..." -ForegroundColor Cyan
    python -m venv .venv
}
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example — edit it before going to prod." -ForegroundColor Yellow
}

Write-Host "Starting API on http://127.0.0.1:8000 (docs at /docs)" -ForegroundColor Green
& ".venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8000
