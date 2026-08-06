"""Run a pass of the direct-employer ATS-detection crawl.

This is the schedulable unit for services/direct_employer.py. It is a plain
script rather than an in-process scheduler on purpose -- see the notes at the
bottom of this docstring.

Usage:
    venv/Scripts/python scripts/crawl_direct_employers.py --limit 200
    venv/Scripts/python scripts/crawl_direct_employers.py --status
    venv/Scripts/python scripts/crawl_direct_employers.py --misses

Each pass takes the next `limit` unprobed domains from the generated seed list
(income-descending, so a partial pass always spends its budget on the biggest
employers left) and records what it found. It is resumable: re-running picks up
where the last pass stopped, and a domain probed inside CRAWL_RECHECK_DAYS is
skipped, so running this more often than that window is a no-op rather than a
re-crawl.

WHY NOT AN IN-PROCESS SCHEDULER
The recheck window is 120 days -- an employer changes ATS about that often. A
background thread waking up on a timer to do something needed a few times a
year, inside a single-instance box whose SQLite file is the app's only store, is
infrastructure risk with no matching payoff. If this ever wants automating,
point an external scheduler at the admin endpoint (POST /admin/crawl) rather
than adding a timer to the API process.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from app.database import init_db                      # noqa: E402
from app.services import direct_employer as de        # noqa: E402


def print_status() -> None:
    s = de.crawl_status()
    print(f"seed size      {s['seed_size']:,}")
    print(f"probed         {s['probed']:,}")
    print(f"remaining      {s['remaining']:,}")
    print(f"ATS found      {s['ats_found']:,}  ({s['hit_rate']:.1%} of probed)")
    if s["by_status"]:
        print("by status:")
        for k, v in sorted(s["by_status"].items(), key=lambda x: -x[1]):
            print(f"  {k:20} {v:,}")
    if s["by_vendor"]:
        print("by vendor:")
        for k, v in sorted(s["by_vendor"].items(), key=lambda x: -x[1]):
            print(f"  {k:20} {v:,}")
    y = s.get("yield") or {}
    if y:
        print(f"\ndownstream yield from {y['boards']} charity board(s):")
        print(f"  discovered       {y['discovered']:,}")
        print(f"  reached a gate   {y['gated']:,}")
        print(f"  surfaced         {y['shown']:,}")
        print(f"  saved/applied    {y['saved_or_applied']:,}")


def print_misses(top: int) -> None:
    """Which unsupported platforms the misses are actually using.

    This is the crawl's second output and arguably its more durable one: it
    ranks the candidate vendors to integrate next by how much UK coverage each
    would unlock, measured rather than guessed."""
    from collections import Counter

    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import DirectEmployerProbe

    db = SessionLocal()
    try:
        notes = db.execute(
            select(DirectEmployerProbe.note).where(
                DirectEmployerProbe.status == "no_ats",
                DirectEmployerProbe.note.like("unsupported ATS:%"),
            )
        ).scalars().all()
    finally:
        db.close()
    counter = Counter(n.split(":", 1)[1].strip() for n in notes if n)
    if not counter:
        print("No unsupported-ATS platforms recorded yet (run a crawl pass first).")
        return
    print(f"unsupported platforms seen on {sum(counter.values())} missed domains:")
    for host, n in counter.most_common(top):
        print(f"  {n:4}  {host}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=200,
                    help="domains to probe this pass (default 200)")
    ap.add_argument("--workers", type=int, default=None,
                    help=f"concurrent domains (default {de.CRAWL_WORKERS})")
    ap.add_argument("--status", action="store_true", help="print progress and exit")
    ap.add_argument("--misses", action="store_true",
                    help="rank the unsupported ATS platforms the misses use, and exit")
    args = ap.parse_args()

    init_db()
    if args.status:
        print_status()
        return 0
    if args.misses:
        print_misses(25)
        return 0

    print(f"Probing up to {args.limit} domains …")
    summary = de.crawl_uk_charities(limit=args.limit, workers=args.workers)
    if summary.get("probed") == 0:
        print(f"Nothing due: {summary.get('note')}")
        return 0
    print(f"  probed     {summary['probed']}")
    print(f"  ATS found  {summary['ats_found']}  ({summary['hit_rate']:.1%})")
    print(f"  by status  {summary['by_status']}")
    if summary["vendors"]:
        print(f"  vendors    {summary['vendors']}")
    print(f"  remaining  {summary['remaining']:,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
