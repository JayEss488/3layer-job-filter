#!/usr/bin/env bash
# Start both halves of AI Job Hunter: the FastAPI backend on :8000 and the
# Next.js frontend on :3000. macOS and Linux; Windows users run start.bat.
#
#   ./start.sh              first run does the setup too
#   ./start.sh --no-setup   skip dependency install (faster restarts)
#
# Both servers run in the foreground of this one terminal, interleaving their
# logs. Ctrl-C stops both -- deliberately, rather than backgrounding them: the
# backend prints which API keys and models it found at boot, and a search prints
# its progress phase by phase, so the logs are where you look when a run does
# something surprising.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SETUP=1
[ "${1:-}" = "--no-setup" ] && SETUP=0

# Prefer a venv in the repo, then an already-active one, then the system python.
if [ -x "venv/bin/python" ]; then
    PY="$ROOT/venv/bin/python"
elif [ -x "venv/Scripts/python.exe" ]; then   # a venv made on Windows, run under WSL/Git Bash
    PY="$ROOT/venv/Scripts/python.exe"
elif [ -n "${VIRTUAL_ENV:-}" ]; then
    PY="$VIRTUAL_ENV/bin/python"
else
    PY="$(command -v python3 || command -v python || true)"
fi

if [ -z "$PY" ]; then
    echo "No Python found. Install Python 3.11+ and re-run." >&2
    exit 1
fi

if [ ! -f .env ]; then
    echo "!! No .env file found."
    echo "   cp .env.example .env    then add an AI key. Nothing will work without one."
    echo
fi

if [ "$SETUP" = "1" ]; then
    if [ ! -d venv ] && [ -z "${VIRTUAL_ENV:-}" ]; then
        echo "==> Creating virtualenv"
        "$PY" -m venv venv
        PY="$ROOT/venv/bin/python"
    fi
    echo "==> Installing Python dependencies"
    "$PY" -m pip install --quiet --upgrade pip
    "$PY" -m pip install --quiet -r requirements.txt
    "$PY" -m pip install --quiet -r backend/requirements.txt
    # Chromium is only needed for full-page scraping of listings whose API
    # snippet is too short to judge. A failure here is not fatal: the run falls
    # back to the snippet, so install it best-effort and carry on.
    echo "==> Installing Chromium for page scraping (first run only, ~150MB)"
    "$PY" -m playwright install chromium || \
        echo "   (skipped -- full-page scraping will fall back to snippets)"

    echo "==> Installing frontend dependencies"
    (cd frontend && npm install --silent)
fi

# One trap for both children. Without it, Ctrl-C kills this script and leaves
# uvicorn and next holding :8000 and :3000, so the next start fails with a port
# clash that looks like a bug in the app.
cleanup() { trap - INT TERM EXIT; kill 0 2>/dev/null || true; }
trap cleanup INT TERM EXIT

echo
echo "==> Backend  → http://127.0.0.1:8000  (API docs at /docs)"
(cd backend && "$PY" -m uvicorn app.main:app --reload --port 8000) &

echo "==> Frontend → http://localhost:3000   ← open this one"
(cd frontend && npm run dev) &

wait
