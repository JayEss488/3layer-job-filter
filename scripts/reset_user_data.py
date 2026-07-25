#!/usr/bin/env python3
"""Reset all user/profile data back to a fresh first-run (onboarding) state.

Clears every profile-scoped table so the app lands new users on /onboarding
again, while KEEPING:
  - job_embeddings  -- the user-independent, content-addressed embedding cache
                       (expensive to recompute; has no profile scope, so a reset
                       never needs to touch it -- this script just doesn't).
  - company_ats     -- the curated/harvested ATS token registry (regenerable but
                       slow; not user data).
  - users           -- the beta login accounts (see scripts/gen_beta_users.py).

Run backup + backfill FIRST (see DEPLOYMENT.md / the plan): archive the DB file
and run scripts/backfill_job_embeddings.py so any per-profile vectors are safely
in job_embeddings before jobs_seen is cleared.

Usage (from repo root, with the committed venv):
    venv/Scripts/python scripts/reset_user_data.py          # asks to confirm
    venv/Scripts/python scripts/reset_user_data.py --yes    # non-interactive
"""
import argparse
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))  # for the `app` package
sys.path.insert(0, REPO_ROOT)

from sqlalchemy import text  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402

# FK-safe order: children before parents. profiles last (most tables cascade off
# it, but explicit deletes are safest across SQLite/Postgres FK settings).
_CLEAR_ORDER = [
    "feedback_log",
    "event_log",
    "roles",
    "jobs_seen",
    "search_runs",
    "profile_attributes",
    "role_families",
    "settings",
    "profiles",
]
_KEEP = ["job_embeddings", "company_ats", "users"]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    init_db()  # ensure every table (incl. new users/event_log) exists first
    db = SessionLocal()
    try:
        counts = {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in _CLEAR_ORDER}
        keep_counts = {t: db.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar() for t in _KEEP}
        total = sum(counts.values())
        print("About to DELETE all rows from:")
        for t in _CLEAR_ORDER:
            print(f"  {t:20s} {counts[t]}")
        print("Keeping (untouched):")
        for t in _KEEP:
            print(f"  {t:20s} {keep_counts[t]}")

        if total == 0:
            print("\nNothing to clear -- already empty.")
            return
        if not args.yes:
            reply = input("\nType 'reset' to proceed: ").strip().lower()
            if reply != "reset":
                print("Aborted.")
                return

        for t in _CLEAR_ORDER:
            db.execute(text(f"DELETE FROM {t}"))
        db.commit()
        print(f"\nCleared {total} row(s). App will now land on /onboarding for each user.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
