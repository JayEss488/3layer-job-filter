"""Re-check whether tracked listings still exist, and stamp dead_at.

    venv/Scripts/python scripts/recheck_liveness.py --limit 20 --dry-run
    venv/Scripts/python scripts/recheck_liveness.py

RUN --dry-run FIRST, ALWAYS, and read the output. `dead_reason` is
unrecoverable: there is no undo, and an early measurement of this same
classification logic turned 5 LIVE listings into "dead" by calling a bare
expired-listing regex that matched a bebee posting's own page furniture. The
guarded path (engine._classify_listing) is what this uses and it declines
correctly -- but the cost of being wrong is permanent, so look before writing.

WHAT IT IS FOR: a sighting from a board's search API proves a listing is still
INDEXED, not still LIVE. This is the half that produces a direct answer, and
`dead_at` is what closes the bracket `first_seen` opens -- without it there is
no way to ask how long a listing actually stays up, or to detect a listing that
came down and went back up.

Plain HTTP GETs only: no browser, no LLM, no API credits. Fail-open --
unverifiable is never treated as dead. Confirmed-dead rows fan out to jobs_seen
(so the pipeline's existing dead_reason filters exclude them for free) and any
already-shown, still-unreviewed card is moved to `ignored`, which is reversible
and stays visible in /my-roles' Deleted tab.

Schedulable daily alongside observe_listings.py, or via POST /admin/recheck.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.database import SessionLocal, init_db  # noqa: E402
from app.services import observe  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-check tracked listings for liveness.")
    ap.add_argument("--limit", type=int, default=observe.OBSERVE_RECHECK_MAX_PER_PASS,
                    help="max listings to fetch this pass (default %(default)s)")
    ap.add_argument("--dry-run", action="store_true",
                    help="classify and print, write nothing. Do this first.")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        result = observe.recheck_liveness(db, limit=args.limit, dry_run=args.dry_run)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
