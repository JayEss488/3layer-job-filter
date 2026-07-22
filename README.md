# Four in a Thousand

AI job-matching that learns from your feedback. Rebuilt per `implementation_notes.md`,
UI modelled on `wireframes/`.

- **Backend** — FastAPI + SQLAlchemy (SQLite for now; swap `DATABASE_URL` to Postgres
  later with no code change). Wraps the existing search engine (`full_auto.py`) behind a
  single service boundary (`backend/app/services/engine.py`).
- **Frontend** — Next.js (App Router) + TanStack Query, styled to match the wireframes.
- **Single-user prototype** — `user_id` is hardcoded to `1` but present on every table,
  so multi-user auth is a drop-in later.

```
backend/         FastAPI app
  app/
    models.py        profiles, profile_attributes, roles, feedback_log, search_runs
    routers/         profiles, attributes, onboarding, search
    services/        snapshot (attrs->engine input), engine (wraps full_auto), parsing,
                     feedback (weights), confidence, llm
frontend/        Next.js app (onboarding / search / my-roles / dashboard)
full_auto.py     the existing 6-phase search engine — imported, never modified
legacy/          the old Flask prototype (archived)
```

## Prerequisites

- Python 3.11+ (a `venv/` already exists in the repo).
- **Node.js 18+** — *not currently installed on this machine.* Install from
  https://nodejs.org to run the frontend. The backend runs without it.
- API keys live in the repo-root `.env` (already populated: OpenAI, Reed, Adzuna, etc.).

## Run the backend

```bash
# from repo root, using the existing venv
venv/Scripts/python -m pip install -r backend/requirements.txt
# engine deps (crawl4ai/playwright/numpy) are in the root requirements.txt:
venv/Scripts/python -m pip install -r requirements.txt
venv/Scripts/python -m playwright install chromium    # first time only, for live search

cd backend
../venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```

API docs at http://127.0.0.1:8000/docs. The SQLite db (`backend/jobmatch.db`) and a
default "Profile 1" are created automatically on first request.

## Run the frontend

```bash
cd frontend
npm install
npm run dev        # http://localhost:3000
```

`frontend/.env.local` points at `http://127.0.0.1:8000`; change `NEXT_PUBLIC_API_URL`
if the backend runs elsewhere.

## How it fits together

1. **Onboarding** parses a CV / pasted text into normalised `profile_attributes`
   (separating *past* from *target* roles), shown as removable chips.
2. **Dashboard** edits that memory live (chips, seniority buttons, salary slider,
   location picker) and shows stats + a confidence score.
3. **Run search** builds a *profile snapshot* (weights applied) and hands it to the
   engine, which fetches → embeds → ranks → scrapes → evaluates and persists `roles`.
4. **Search** shows ranked cards; tick/cross feed the **weight system** (no retraining —
   per-attribute weights are nudged and bias future searches). Crossed cards sink.
5. **My Roles** holds saved / ignored / applied with an application-status toggle.

### Cost / behaviour guards (from the notes)
- Max **5 searches per profile per day** (`MAX_SEARCHES_PER_DAY`).
- Re-running a search: ticked stay **saved**, crossed become **deleted**, leftover new
  become **ignored**; every result is cached by `external_id` so repeats never reappear.
- A harsh-filter warning is surfaced when the semantic step yields fewer than 5 roles,
  while still showing the best available.
