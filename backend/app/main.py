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
from .routers import (
    admin,
    attributes,
    auth_router,
    families,
    feedback,
    onboarding,
    profiles,
    search,
    settings,
)
from .services.auth import parse_token, require_authenticated, reset_current_user, set_current_user
from .services.beta import require_active_beta

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

    _install_shutdown_signal_handlers()


def _install_shutdown_signal_handlers() -> None:
    """Make SIGTERM/SIGINT reach the running search immediately.

    A search runs as an in-process BackgroundTask, and uvicorn waits for those
    to finish BEFORE firing the lifespan "shutdown" event -- so a shutdown hook
    alone runs only once the search is already over, which on a container host
    means never (the supervisor SIGKILLs first). Observed on Fly as the machine
    being torn down with the search still writing: `error umounting /data:
    EBUSY` on the way out, `recovering journal` on the way back in.

    So we chain onto uvicorn's own handler rather than replacing it: flip the
    cooperative-cancel flag (all a signal handler may safely do -- no DB, no
    locks), then delegate so uvicorn's normal graceful shutdown still happens.
    Installed from the startup event, which uvicorn runs inside its own
    capture_signals() block, so its handler is already in place to chain to.

    Best-effort: signal.signal only works on the main thread, and Windows has
    no SIGTERM to speak of -- neither case is fatal, it just falls back to the
    startup reaper."""
    import signal

    from .services.engine import request_process_shutdown

    def _make(previous):
        def _handler(signum, frame):
            request_process_shutdown()
            if callable(previous):
                previous(signum, frame)
        return _handler

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _make(signal.getsignal(sig)))
        except (ValueError, OSError, AttributeError) as e:
            print(f"[startup] could not hook {sig!r} for graceful search cancel: {e!r}")


@app.on_event("shutdown")
def _shutdown():
    """Ask any in-flight search to stop before the process goes away.

    A search runs as an in-process BackgroundTask, so it dies with the process
    no matter what -- this doesn't save the run, it just lets the worker unwind
    through the normal cancel path (releasing the SQLite file) instead of being
    killed mid-write. Without it, a container host tearing the machine down
    finds the volume still busy and the database unmounted uncleanly. The
    startup reaper still covers the hard-kill case, where nothing runs here."""
    from .database import SessionLocal
    from .services.engine import request_shutdown_cancel

    db = SessionLocal()
    try:
        n = request_shutdown_cancel(db)
        if n:
            print(f"[shutdown] signalled {n} in-flight search run(s) to stop")
    except Exception as e:  # never block shutdown on this
        print(f"[shutdown] could not signal in-flight searches: {e!r}")
    finally:
        db.close()


@app.get("/health")
def health():
    return {"status": "ok"}


# Public, and it MUST stay public: this router carries POST /auth/google, which
# is how an account comes into existence. Putting it behind require_authenticated
# would make sign-up require being signed in.
#
# The routes inside it that DO need a user (/me, /signup/survey, /exit/survey)
# self-guard by calling current_user_id(), which raises 401 when the request
# carried no valid token -- the same pattern /me has always used.
#
# It also MUST stay off the beta-window dependency below: /exit/survey has to be
# reachable by exactly the users whose window has lapsed, or the wrap-up survey
# becomes unanswerable by the people it exists to ask.
app.include_router(auth_router.router)

# Protected: a valid Bearer token AND an unlapsed beta window are required for
# every route below. require_active_beta is what makes "access lapses at day 7"
# real rather than cosmetic -- an expired user cannot start a search, edit a
# profile or read roles. See services/beta.py for why the wrap-up survey gate is
# deliberately NOT enforced here too.
_auth = [Depends(require_authenticated), Depends(require_active_beta)]
app.include_router(profiles.router, dependencies=_auth)
app.include_router(attributes.router, dependencies=_auth)
app.include_router(families.router, dependencies=_auth)
app.include_router(onboarding.router, dependencies=_auth)
app.include_router(search.router, dependencies=_auth)
app.include_router(settings.router, dependencies=_auth)

# Feedback is authed but NOT beta-gated: an answer the user has already typed
# must never be lost to a window that lapsed between the prompt appearing and
# the button being pressed.
app.include_router(feedback.router, dependencies=[Depends(require_authenticated)])

# Owner-only analytics, guarded by the ADMIN_TOKEN header (not user auth).
app.include_router(admin.router)
