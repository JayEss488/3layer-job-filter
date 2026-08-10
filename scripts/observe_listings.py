"""One pass of the scheduled listing-observation crawl.

    venv/Scripts/python scripts/observe_listings.py
    venv/Scripts/python scripts/observe_listings.py --status
    venv/Scripts/python scripts/observe_listings.py --terms 6

POINT A SCHEDULER AT THIS, DAILY. That is not a suggestion about tidiness --
full_auto.EVERGREEN_SEEN_DENSITY measures the fraction of days since discovery
on which a listing was seen again, so a missed day inflates the denominator
while the numerator stands still. Gaps don't delay the signal, they suppress it.
Check `--status` weekly: a crawl that has silently stopped looks identical to a
healthy one in every field except `distinct_observation_days`, and it is the
one failure that costs time nothing can recover.

WHY THERE IS NO IN-PROCESS SCHEDULER: same reasoning as
scripts/crawl_direct_employers.py -- a timer thread inside the single-instance
box whose SQLite file is the app's only store is infrastructure risk with no
matching payoff. The schedulable units are this script and POST /admin/observe.

COST: board-API quota only (Reed + Adzuna by default). No OpenAI call of any
kind. It never writes jobs_seen, never creates a Role or a SearchRun row, and
therefore cannot consume the daily search cap.
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
    ap = argparse.ArgumentParser(description="Run one listing-observation pass.")
    ap.add_argument("--terms", type=int, default=observe.OBSERVE_TERMS_PER_RUN,
                    help="max search terms this pass (default %(default)s)")
    ap.add_argument("--status", action="store_true",
                    help="print observation coverage and exit, changing nothing")
    args = ap.parse_args()

    init_db()
    db = SessionLocal()
    try:
        if args.status:
            print(json.dumps(observe.observe_status(db), indent=2))
            return 0
        result = observe.run_pass(db, limit_terms=args.terms)
        print(json.dumps(result, indent=2))
        return 0 if result.get("ok") else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
