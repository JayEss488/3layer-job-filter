"""Measure how many already-surfaced roles are still live.

Read-only, offline, no API credits and no LLM: it re-runs engine._classify_listing
(one plain HTTP GET, the same check the pipeline uses) over the Role rows this
app has already shown, and reports the dead / alive / unverifiable split.

This is the number that justifies verifying final picks at all. It is also the
way to check the browser escalation is earning its keep: run once without
--browser to see how many hosts refuse a plain request, then once with it to see
how many of those turn into a definite answer.

NOTE it writes nothing. dead_reason is unrecoverable and this is a measurement
tool, not a cleanup pass -- so a listing it calls dead here is only marked dead
when a real run re-verifies it.

Run:  venv/Scripts/python scripts/audit_listing_liveness.py
      venv/Scripts/python scripts/audit_listing_liveness.py --limit 40 --browser
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from concurrent.futures import ThreadPoolExecutor  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import Role  # noqa: E402
from app.services import engine as eng  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=60, help="roles to check")
    ap.add_argument("--status", default=None, help="only this Role.status")
    ap.add_argument("--browser", action="store_true",
                    help="escalate unverifiable rows to the headless browser")
    args = ap.parse_args()

    import full_auto

    db = SessionLocal()
    q = db.query(Role).filter(Role.provisional == False, Role.url.isnot(None))  # noqa: E712
    if args.status:
        q = q.filter(Role.status == args.status)
    roles = q.order_by(Role.created_at.desc()).limit(args.limit).all()
    db.close()
    if not roles:
        print("No surfaced roles with a URL in the store.")
        return 0

    jobs = [{"url": r.url, "title": r.title, "company": r.company,
             "board": r.source, "_role": r} for r in roles]
    print(f"Checking {len(jobs)} surfaced role(s) …\n")

    with ThreadPoolExecutor(max_workers=eng.VERIFY_MAX_WORKERS) as ex:
        states = list(ex.map(lambda j: eng._classify_listing(full_auto, j), jobs))

    verdicts = {id(j): (s, d) for j, (s, d, _ld) in zip(jobs, states)}
    unverifiable = [j for j in jobs if verdicts[id(j)][0] == "unverifiable"]

    if args.browser and unverifiable:
        print(f"escalating {len(unverifiable)} unverifiable row(s) to the browser …")
        got = asyncio.run(eng._verify_via_browser(full_auto, unverifiable))
        for j in unverifiable:
            if id(j) in got:
                verdicts[id(j)] = got[id(j)]

    counts = collections.Counter(v[0] for v in verdicts.values())
    total = len(jobs)
    for state in ("alive", "dead", "unverifiable"):
        n = counts.get(state, 0)
        print(f"  {state:14s} {n:4d}  ({n / total:5.1%})")

    reasons = collections.Counter(v[1] for v in verdicts.values() if v[0] == "dead")
    if reasons:
        print("\nDeath reasons:")
        for reason, n in reasons.most_common():
            print(f"  {reason:28s} {n}")

    print("\nDead listings (these are live cards in the app right now):")
    for j in jobs:
        if verdicts[id(j)][0] == "dead":
            r = j["_role"]
            print(f"  [{r.status}] {r.title} — {r.company or '?'} ({verdicts[id(j)][1]})")
            print(f"      {r.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
