"""FastAPI application entrypoint. Closed-beta auth: an HTTP middleware reads the
Bearer token into a per-request ContextVar (see services/auth.py), the public
/login router mints tokens, and every data router is guarded by
require_authenticated. deps.current_user_id() reads that ContextVar."""
import asyncio
import sys

# uvicorn --reload on Windows can leave the process on the Selector event loop,
# which cannot spawn subprocesses at all -- that kills Playwright (full-page
# scraping) with a NotImplementedError deep in the search pipeline. Force
# Proactor before anything else runs; the policy is process-global so it also
# covers the BackgroundTasks worker thread that runs the search.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from .config import FRONTEND_ORIGINS
from .database import init_db
from .routers import admin, attributes, auth_router, families, onboarding, profiles, search, settings
from .services.auth import parse_token, require_authenticated, reset_current_user, set_current_user

app = FastAPI(title="Four in a Thousand API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _auth_context(request: Request, call_next):
    """Populate the current-user ContextVar from the Bearer token, per request.

    Does NOT reject anything itself -- enforcement is the require_authenticated
    dependency on the protected routers (and current_user_id() raising). Keeping
    rejection out of the middleware means every 401 is raised inside the app,
    where the CORS middleware still wraps it with the right headers. Runs in the
    request's async context so the value is copied into the threadpool that sync
    routes/dependencies execute in."""
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else None
    uid = parse_token(token) if token else None
    ctx_token = set_current_user(uid)
    try:
        return await call_next(request)
    finally:
        reset_current_user(ctx_token)


@app.on_event("startup")
def _startup():
    init_db()

    from .database import SessionLocal
    from .services.engine import reap_stale_search_runs

    db = SessionLocal()
    try:
        n = reap_stale_search_runs(db)
        if n:
            print(f"[startup] reaped {n} stale 'running' search run(s) from a previous process lifetime")
    finally:
        db.close()

    # Populate the shared ATS store with the curated baseline on first boot (only
    # if empty; runs in the background so it never blocks startup).
    from .services.seed import seed_baseline_if_empty

    seed_baseline_if_empty()


@app.get("/health")
def health():
    return {"status": "ok"}


# Public: login (mints tokens) + /me (self-guards via current_user_id()).
app.include_router(auth_router.router)

# Protected: a valid Bearer token is required for every route below.
_auth = [Depends(require_authenticated)]
app.include_router(profiles.router, dependencies=_auth)
app.include_router(attributes.router, dependencies=_auth)
app.include_router(families.router, dependencies=_auth)
app.include_router(onboarding.router, dependencies=_auth)
app.include_router(search.router, dependencies=_auth)
app.include_router(settings.router, dependencies=_auth)

# Owner-only analytics, guarded by the ADMIN_TOKEN header (not user auth).
app.include_router(admin.router)
