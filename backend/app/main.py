"""FastAPI application entrypoint.

There is no login. This runs as a single local user (see deps.current_user_id),
which is the right shape for a tool you run on your own machine against your own
API keys: an account system would add a database of credentials to protect and
would gate nothing, since anyone who can reach the port can already read the
SQLite file next to it.

If you expose this beyond localhost, put an authenticating reverse proxy in
front of it -- the app itself performs no access control."""
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
from .routers import (
    admin,
    attributes,
    families,
    onboarding,
    profiles,
    search,
    settings,
)

app = FastAPI(title="AI Job Hunter API", version="2.0.0")

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

    # What this install can actually do, given the keys present. Printed before
    # anything else because a missing key here fails SILENTLY at search time --
    # see services/startup_report.py.
    from .services.startup_report import report

    report()

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


app.include_router(profiles.router)
app.include_router(attributes.router)
app.include_router(families.router)
app.include_router(onboarding.router)
app.include_router(search.router)
app.include_router(settings.router)

# Maintenance endpoints (the direct-employer crawl, the daily observation pass,
# the liveness re-check). Guarded by the ADMIN_TOKEN header when one is set --
# see routers/admin.py. They are separated from the routers above because they
# are operator actions, not part of using the app.
app.include_router(admin.router)
