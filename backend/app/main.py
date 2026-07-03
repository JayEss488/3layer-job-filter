"""FastAPI application entrypoint. Single-user prototype: no auth middleware yet,
but every route derives profile_id and scopes to CURRENT_USER_ID."""
import asyncio
import sys

# uvicorn --reload on Windows can leave the process on the Selector event loop,
# which cannot spawn subprocesses at all -- that kills Playwright (full-page
# scraping) with a NotImplementedError deep in the search pipeline. Force
# Proactor before anything else runs; the policy is process-global so it also
# covers the BackgroundTasks worker thread that runs the search.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import FRONTEND_ORIGINS
from .database import init_db
from .routers import attributes, onboarding, profiles, search, settings

app = FastAPI(title="Omni Board API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    init_db()
    # Populate the shared ATS store with the curated baseline on first boot (only
    # if empty; runs in the background so it never blocks startup).
    from .services.seed import seed_baseline_if_empty

    seed_baseline_if_empty()


@app.get("/health")
def health():
    return {"status": "ok"}


app.include_router(profiles.router)
app.include_router(attributes.router)
app.include_router(onboarding.router)
app.include_router(search.router)
app.include_router(settings.router)
