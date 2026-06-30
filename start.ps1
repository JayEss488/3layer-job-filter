# Starts the backend (uvicorn) and frontend (next dev) each in their own
# PowerShell window, so one command replaces the usual two-terminal dance.
# Usage:  ./start.ps1   (or right-click -> Run with PowerShell)

$root = $PSScriptRoot

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\backend'; ../venv/Scripts/python -m uvicorn app.main:app --reload --port 8000"
)

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "cd '$root\frontend'; npm run dev"
)

Write-Host "Backend starting on http://localhost:8000"
Write-Host "Frontend starting on http://localhost:3000"
