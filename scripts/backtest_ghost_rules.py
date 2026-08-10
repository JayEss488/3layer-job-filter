"""Score every stored listing against the ghost rules, offline. Read-only.

    venv/Scripts/python scripts/backtest_ghost_rules.py
    venv/Scripts/python scripts/backtest_ghost_rules.py --samples 8

Costs nothing: no API, no LLM, no network, and it writes nothing.

WHAT THE BAR IS, and it is not the hit count. The risk of a free filter is a
silent FALSE POSITIVE, so the number that decides whether this ships is how many
roles the user actually engaged with get flagged -- the same standard
engine._pool_quality_prescreen was held to ("validated against the live store
before shipping... measure that, not the hit count"). Specifically:

    * ZERO roles the user APPLIED to may be flagged high. That is the hard bar.
    * Roles ever SHOWN should be flagged only rarely, and every one should be
      inspectable in the sample output below.

A store-wide fire count is deliberately NOT validation. Most of this store is US
ATS rows the country filter discards before a UK profile ever sees them, so a
rule can fire thousands of times store-wide and never once reach a card.

The cross-tab by posted_at_approx exists because that is the single most likely
way to get this wrong: an approximate date is an ATS updated_at, not a posting
date, and reading it as "posted" would make Greenhouse boards the largest ghost
population in the store with every hit a false positive.
"""
import argparse
import collections
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal  # noqa: E402
from app.models import JobSeen, ListingObservation, Role  # noqa: E402
from app.services import ghost as gh  # noqa: E402
from app.services.sources import ATS_KEYS, canonical_key  # noqa: E402


def _job_dict(r: JobSeen) -> dict:
    """The same shape engine._rows_to_dicts hands the pipeline."""
    return {
        "title": r.title, "company": r.company, "location": r.location,
        "url": r.url, "snippet": r.snippet, "full_text": r.full_text,
        "board": r.source,
        "_posted_at": r.posted_at.isoformat() if r.posted_at else None,
        "_expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "_posted_at_approx": bool(r.posted_at_approx),
        "_first_seen": r.first_seen.isoformat() if r.first_seen else None,
        "_seen_days": r.seen_days or 1,
        "_repost_key": r.repost_key,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", type=int, default=5,
                    help="example listings to print per rule")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        obs_first = db.execute(select(ListingObservation.first_seen)
                               .order_by(ListingObservation.first_seen.asc())
                               .limit(1)).scalar()
        obs_last = db.execute(select(ListingObservation.last_seen)
                              .order_by(ListingObservation.last_seen.desc())
                              .limit(1)).scalar()
        store_age = (obs_last - obs_first).days if (obs_first and obs_last) else 0

        ctx = gh.GhostContext(store_age_days=store_age)
        print(f"store age {store_age}d -- observation clock "
              f"{'OPEN' if ctx.observation_clock_open else 'CLOSED (longitudinal rules dormant)'}")

        rows = db.execute(select(JobSeen)).scalars().all()
        # Which listings the user ever actually saw or acted on.
        shown, applied, saved = set(), set(), set()
        for ext, status in db.execute(select(Role.external_id, Role.status)).all():
            if not ext:
                continue
            shown.add(ext)
            if status == "applied":
                applied.add(ext)
            elif status == "saved":
                saved.add(ext)

        levels = collections.Counter()
        by_rule = collections.Counter()
        by_rule_src = collections.defaultdict(collections.Counter)
        by_rule_approx = collections.defaultdict(collections.Counter)
        samples = collections.defaultdict(list)
        flagged_shown, flagged_applied, flagged_saved = [], [], []

        for r in rows:
            job = _job_dict(r)
            level, signals = gh.evaluate(job, ctx)
            levels[level or "none"] += 1
            src = canonical_key(r.source) or (r.source or "?")
            fam = "ats" if src in ATS_KEYS else src
            for s in signals:
                by_rule[s] += 1
                by_rule_src[s][fam] += 1
                by_rule_approx[s]["approx" if r.posted_at_approx else "definite"] += 1
                if len(samples[s]) < args.samples:
                    samples[s].append(
                        f"{(r.company or '-')[:22]:<22} {(r.title or '')[:44]:<44} "
                        f"posted={r.posted_at.date() if r.posted_at else '-'} src={r.source}")
            if level and r.identity_hash in shown:
                entry = (level, signals, r.company, r.title, r.source, r.url)
                flagged_shown.append(entry)
                if r.identity_hash in applied:
                    flagged_applied.append(entry)
                if r.identity_hash in saved:
                    flagged_saved.append(entry)

        total = len(rows)
        print(f"\n=== STORE-WIDE ({total} listings) -- NOT a validation number ===")
        for lv in ("high", "medium", "none"):
            n = levels[lv]
            print(f"  {lv:<8}{n:>7}  {n/total:>6.1%}")

        print("\n=== BY RULE ===")
        for rule, n in by_rule.most_common():
            tier = "DECISIVE" if rule in gh.DECISIVE else "ordinary"
            print(f"  {rule:<22}{n:>7}  {tier}")
            print(f"      by source: {dict(by_rule_src[rule].most_common(6))}")
            print(f"      posted_at: {dict(by_rule_approx[rule])}")

        print("\n=== SAMPLES ===")
        for rule in by_rule:
            print(f"  -- {rule} --")
            for s in samples[rule]:
                print(f"     {s}")

        print("\n=== THE ACTUAL BAR: roles the user saw or acted on ===")
        print(f"  roles ever shown            : {len(shown)}")
        print(f"  ...of which flagged         : {len(flagged_shown)}")
        print(f"  roles SAVED                 : {len(saved)}   flagged: {len(flagged_saved)}")
        print(f"  roles APPLIED to            : {len(applied)}   flagged: {len(flagged_applied)}")
        for lv, sig, co, ti, src, url in flagged_shown:
            print(f"     [{lv}] {','.join(sig):<38} {(co or '-')[:20]:<20} {(ti or '')[:40]}")
        high_applied = [e for e in flagged_applied if e[0] == "high"]
        verdict = "PASS" if not high_applied else "FAIL"
        print(f"\n  {verdict}: {len(high_applied)} applied-to role(s) flagged HIGH "
              f"(bar is 0)")
        return 0 if verdict == "PASS" else 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
