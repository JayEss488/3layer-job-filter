# Backend image for Four in a Thousand (FastAPI + the full_auto search engine).
#
# This is the BACKEND only. The Next.js frontend deploys separately to Vercel --
# it cannot host this: the engine runs headless Chromium (Playwright/Crawl4AI),
# long-lived in-process background search tasks, and writes a stateful DB, none of
# which fit Vercel's serverless model. See DEPLOYMENT.md.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install Python deps first (better layer caching). Both files: root
# requirements.txt = engine (crawl4ai/playwright/numpy/openai), backend one = API.
COPY requirements.txt ./requirements.txt
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install -r requirements.txt -r backend/requirements.txt

# Chromium + its OS-level dependencies for Phase 5 full-page scraping.
RUN python -m playwright install --with-deps chromium

# App code (see .dockerignore for what's excluded -- frontend, venv, DBs, etc.)
COPY . .

# config.py inserts the repo root on sys.path so `full_auto` imports; --app-dir
# makes the `app` package importable. $PORT is provided by the host (Render/Fly).
ENV PORT=8000
EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --app-dir backend --host 0.0.0.0 --port ${PORT:-8000}"]
