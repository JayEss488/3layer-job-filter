# Backend image for Four in a Thousand (FastAPI + the full_auto search engine).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 1. Install OS dependencies, curl, and Supercronic early (optimized layer caching)
ENV SUPERCRONIC_URL=https://github.com/aptible/supercronic/releases/download/v0.2.29/supercronic-linux-amd64 \
    SUPERCRONIC=supercronic-linux-amd64 \
    SUPERCRONIC_SHA1SUM=cd48d45c4a1013f570beb4ed8665b536884f19d6

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && curl -fsSLO "$SUPERCRONIC_URL" \
    && echo "${SUPERCRONIC_SHA1SUM}  ${SUPERCRONIC}" | sha1sum -c - \
    && chmod +x "$SUPERCRONIC" \
    && mv "$SUPERCRONIC" /usr/local/bin/supercronic \
    && rm -rf /var/lib/apt/lists/*

# 2. Install Python dependencies
COPY requirements.txt ./requirements.txt
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install -r requirements.txt -r backend/requirements.txt

# 3. Install Playwright Chromium dependencies
RUN python -m playwright install --with-deps chromium

# 4. Copy Application Code
COPY . .

# 5. Environment & Startup
ENV PORT=8000
EXPOSE 8000

# Supercronic and uvicorn share the one machine (and the one SQLite file on the
# Fly volume -- safe because database.py puts SQLite in WAL mode, so the cron
# writer and the API's readers never block each other).
#
# `exec` on uvicorn is load-bearing, not style. Without it `sh` stays PID 1 with
# uvicorn as a child, and a non-interactive shell installs no SIGTERM handler --
# which for PID 1 means the kernel DISCARDS the signal outright. Fly's shutdown
# would then be ignored for the whole of fly.toml's kill_timeout and end in
# SIGKILL, tearing the machine down mid-write and unmounting /data dirty
# ("recovering journal" on the next boot). That kill_timeout exists precisely so
# a running search can unwind and release the DB; `exec` is what lets the signal
# reach the process that has to act on it. Supercronic keeps running as a child
# of the exec'd process and goes away with the machine.
#
# -passthrough-logs so the cron jobs' own stdout/stderr reach `fly logs` as
# written, rather than being re-wrapped by supercronic's structured logger.
CMD ["sh", "-c", "supercronic -passthrough-logs /app/crontab & exec uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
