"""Measure the visa-sponsor matcher against the live job store.

Read-only, offline, no API credits and no LLM: it re-runs services.sponsors over
every company name in `jobs_seen` and reports how many resolve to a licensed
sponsor. This is the number that decides whether the sponsorship filter is worth
having, and it is the regression bar for any change to the matcher.

WHAT TO LOOK AT
  * The per-source table. ATS vendors (gh/lever/ashby/workable/recruitee/
    personio/smartrecruiters) match far below adzuna/reed/jsearch because their
    rows identify the employer by an opaque vendor token ("tiger-analytics"), and
    a token only matches when it happens to normalise to the registered name.
    fetch_ats now carries CompanyATS.company through so the real name is used --
    but note the ceiling that leaves: seed_ats.py writes `(token, vendor, token)`,
    so `company` IS the token for 1,818 of the 1,820 registry rows today, and only
    direct-employer-crawled rows carry a genuine name. The "re-keyed" section
    below measures the gap, and it will only widen as the crawl grows. The real
    unlock would be backfilling CompanyATS.company from each vendor's own board
    metadata (Greenhouse and SmartRecruiters both expose a company name); until
    then this stage is bounded by the registry, not by the matcher.
  * The prefix-only hit list. Every entry there was matched on >= 2 shared
    leading tokens rather than a whole-name hit, so it is where a false positive
    would show up first. A generic single-word brand appearing here means the
    guard has been weakened.
  * The miss list. Recruitment agencies dominating it is expected and structural
    (see services/sponsors.py) -- not a matcher bug.

Run:  venv/Scripts/python scripts/audit_sponsor_match.py
      venv/Scripts/python scripts/audit_sponsor_match.py --samples 40
"""
from __future__ import annotations

import argparse
import collections
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.join(BASE_DIR, "backend"))

from app.services import sponsors  # noqa: E402

DB = os.path.join(BASE_DIR, "backend", "jobmatch.db")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--samples", type=int, default=25, help="names to print per list")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"No store at {args.db}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(args.db)
    rows = list(conn.execute("SELECT company, source FROM jobs_seen"))
    roles = list(conn.execute("SELECT company FROM roles WHERE provisional = 0"))
    ats = list(conn.execute("SELECT company FROM company_ats"))
    conn.close()

    print(f"Register: {sponsors.sponsor_count():,} normalised keys\n")

    blank = sum(1 for c, _ in rows if not (c or "").strip())
    named = [(c.strip(), (s or "").split(":")[0]) for c, s in rows if (c or "").strip()]
    uniq = collections.Counter(c for c, _ in named)
    verdicts = {c: sponsors.match(c) for c in uniq}

    print(f"jobs_seen: {len(rows):,} rows, {blank:,} with no company, "
          f"{len(uniq):,} unique companies")
    for label in ("exact", "prefix", None):
        n = sum(1 for v in verdicts.values() if v == label)
        print(f"  {str(label):8s} {n:6,} unique  ({n / len(uniq):5.1%})")
    hit_rows = sum(1 for c, _ in named if verdicts[c])
    print(f"  matched rows: {hit_rows:,}/{len(named):,} = {hit_rows / len(named):.1%}")

    print("\nBy source (rows):")
    per = collections.Counter()
    per_hit = collections.Counter()
    for c, board in named:
        per[board] += 1
        if verdicts[c]:
            per_hit[board] += 1
    for board, n in per.most_common():
        print(f"  {board:18s} {per_hit[board]:5,}/{n:5,} = {per_hit[board] / n:5.1%}")

    if roles:
        r_named = [(c or "").strip() for c in (r[0] for r in roles) if (c or "").strip()]
        r_hit = sum(1 for c in r_named if sponsors.match(c))
        print(f"\nSurfaced roles: {r_hit}/{len(roles)} match a licensed sponsor")

    if ats:
        a_named = [(c or "").strip() for c in (r[0] for r in ats) if (c or "").strip()]
        a_hit = sum(1 for c in a_named if sponsors.match(c))
        print(f"company_ats boards: {a_hit}/{len(ats)} = {a_hit / len(ats):.1%} "
              f"(the ceiling for a sponsor-priority ATS tier)")

    # What the ATS rows WILL look like once fetch_ats carries the registry name.
    # Existing store rows keep the token they were discovered with, so this join
    # is the only way to see the fix's effect without a fresh run: it re-keys each
    # ATS row to CompanyATS.company by token, exactly as new rows will arrive.
    by_token = {t: c for c, t in
                ((r[0], r[1]) for r in
                 sqlite3.connect(args.db).execute("SELECT company, token FROM company_ats"))}
    ats_rows = [(c, s) for c, s in rows if ":" in (s or "")]
    if ats_rows:
        before = sum(1 for c, _ in ats_rows if sponsors.match((c or "").strip()))
        after = 0
        for c, s in ats_rows:
            token = (s or "").split(":", 1)[1]
            name = by_token.get(token) or (c or "").strip()
            if sponsors.match(name):
                after += 1
        print(f"\nATS rows re-keyed to the registry company name:")
        print(f"  as stored (vendor token): {before:5,}/{len(ats_rows):,} = "
              f"{before / len(ats_rows):5.1%}")
        print(f"  with the real name:       {after:5,}/{len(ats_rows):,} = "
              f"{after / len(ats_rows):5.1%}")

    prefix_only = sorted(c for c, v in verdicts.items() if v == "prefix")
    misses = sorted(c for c, v in verdicts.items() if v is None)
    print(f"\nPrefix-only hits ({len(prefix_only)}) -- false positives surface here first:")
    for c in prefix_only[:args.samples]:
        print(f"  {c}")
    print(f"\nMisses ({len(misses)}), first {args.samples}:")
    for c in misses[:args.samples]:
        print(f"  {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
