# Starts both halves of AI Job Hunter, each in its own PowerShell window:
# the FastAPI backend on :8000 and the Next.js frontend on :3000.
#
#   .\start.ps1              first run does the setup too
#   .\start.ps1 -NoSetup     skip dependency install (faster restarts)
#
# Or just double-click start.bat, which calls this with the execution policy
# bypassed.
param([switch]$NoSetup)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

# Prefer the repo's own venv; fall back to whatever `python` is on PATH.
$py = Join-Path $root "venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    $sys = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $sys) {
        Write-Host "No Python found. Install Python 3.11+ from python.org and re-run." -ForegroundColor Red
        Read-Host "Press Enter to close"
        exit 1
    }
    if (-not $NoSetup) {
        Write-Host "==> Creating virtualenv"
        & $sys.Source -m venv venv
    }
    if (Test-Path (Join-Path $root "venv\Scripts\python.exe")) {
        $py = Join-Path $root "venv\Scripts\python.exe"
    } else {
        $py = $sys.Source
    }
}

if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "!! No .env file found." -ForegroundColor Yellow
    Write-Host "   copy .env.example .env    then add an AI key. Nothing will work without one."
    Write-Host ""
}

if (-not $NoSetup) {
    Write-Host "==> Installing Python dependencies"
    & $py -m pip install --quiet --upgrade pip
    & $py -m pip install --quiet -r requirements.txt
    & $py -m pip install --quiet -r backend\requirements.txt

    # Chromium is only needed for full-page scraping of listings whose API
    # snippet is too short to judge. A failure is not fatal -- the run falls
    # back to the snippet -- so this is best-effort.
    Write-Host "==> Installing Chromium for page scraping (first run only, ~150MB)"
    try { & $py -m playwright install chromium }
    catch { Write-Host "   (skipped -- full-page scraping will fall back to snippets)" }

    Write-Host "==> Installing frontend dependencies"
    Push-Location frontend
    npm install --silent
    Pop-Location
}

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\backend'; & '$py' -m uvicorn app.main:app --reload --port 8000"
)

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\frontend'; npm run dev"
)

Write-Host ""
Write-Host "==> Backend  -> http://127.0.0.1:8000  (API docs at /docs)"
Write-Host "==> Frontend -> http://localhost:3000   <- open this one" -ForegroundColor Green
Write-Host ""
Write-Host "Both are starting in their own windows. Give them ~20 seconds, then open"
Write-Host "http://localhost:3000 in a browser. Close those windows to stop the app."
