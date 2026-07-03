"""One-time baseline seeding of the shared ATS company store.

On startup, if `company_ats` is empty, populate it with the live-validated
curated baseline (seed_ats.CANDIDATES) so the company-board discovery tier isn't
empty before any per-profile harvest has run. Runs in a daemon thread so boot
isn't blocked by the ~90 validation calls, and is idempotent across restarts:
once the store has any rows it does nothing.
"""
from __future__ import annotations

import threading

from sqlalchemy import func, select

from ..database import SessionLocal
from ..models import CompanyATS


def _run_seed() -> None:
    # Lazy import: seed_ats pulls in full_auto (heavy), so only touch it on the
    # background thread, and only once we've decided a seed is actually needed.
    try:
        import seed_ats  # repo root; importable via config's sys.path insert

        seed_ats.seed_curated()
    except Exception as e:  # best-effort: never let a boot-time seed crash the app
        print(f"[seed] baseline ATS seed failed: {e!r}")


def seed_baseline_if_empty() -> None:
    """Kick off the baseline seed in a daemon thread iff `company_ats` is empty."""
    db = SessionLocal()
    try:
        count = db.execute(select(func.count(CompanyATS.id))).scalar_one()
    finally:
        db.close()
    if count:
        return
    print("[seed] company_ats empty -> seeding curated baseline in background…")
    threading.Thread(target=_run_seed, name="ats-baseline-seed", daemon=True).start()
