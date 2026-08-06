"""One-off backfill for the distance/place-name and normalised-salary columns.

Both features compute at PERSIST time (engine._role_location_fields /
_role_salary_fields), so a role that was already on the page before they existed
carries nulls and would keep showing a raw postcode and an unparsed salary
string until it happened to be re-surfaced by a future run -- which, for a saved
or applied role, may be never.

Everything here is derived from data already on the row (its `location` and
`salary_text`) plus the profile's own stated place. No API calls, no LLM, no
network: it is exactly the same pure functions the pipeline runs, applied
retroactively. Safe to re-run -- rows that already have a value are skipped.

Run:  venv/Scripts/python scripts/backfill_role_geo_salary.py [--dry-run]
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.config import WORK_TYPE_VALUES                      # noqa: E402
from app.database import SessionLocal, init_db               # noqa: E402
from app.models import ProfileAttribute, Role                # noqa: E402
from app.services import geo, salary                         # noqa: E402


def origin_for_profile(db, profile_id: int) -> str:
    """The candidate's stated place -- the first `location` row that isn't a
    work-type token. Mirrors snapshot.build_snapshot's split; deliberately not
    imported from there, which does a great deal more work than this needs."""
    rows = (
        db.query(ProfileAttribute)
        .filter(ProfileAttribute.profile_id == profile_id,
                ProfileAttribute.type == "location")
        .all()
    )
    for a in rows:
        if (a.value or "").lower().strip() not in WORK_TYPE_VALUES:
            return a.value
    return ""


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        origins: dict[int, tuple[float, float] | None] = {}
        stats = {"labelled": 0, "distanced": 0, "salaried": 0}

        for role in db.query(Role).all():
            if role.location_label is None:
                label = geo.pretty_location(role.location or "")
                if label:
                    role.location_label = label
                    stats["labelled"] += 1
            if role.distance_miles is None:
                if role.profile_id not in origins:
                    place = origin_for_profile(db, role.profile_id)
                    origins[role.profile_id] = geo.resolve(place) if place else None
                origin = origins[role.profile_id]
                point = geo.resolve(role.location or "") if origin else None
                if origin and point:
                    role.distance_miles = round(geo.haversine_miles(origin, point))
                    stats["distanced"] += 1
            if role.salary_period is None and (role.salary_text or "").strip():
                parsed = salary.parse_salary(role.salary_text)
                if parsed:
                    role.salary_min = parsed["min"]
                    role.salary_max = parsed["max"]
                    role.salary_period = parsed["period"]
                    role.salary_currency = parsed["currency"]
                    stats["salaried"] += 1

        # jobs_seen is deliberately NOT backfilled. It has no salary field to
        # parse: the boards' structured figures were discarded at discovery time
        # before the columns existed, leaving only the description -- and pay
        # parsed out of a description is mostly not pay. Tried once against this
        # store: it produced 4,389 "salaries", of which a 15-row audit found 12
        # wrong (employee counts, requisition numbers, years of experience,
        # signing bonuses). Those figures feed screen_gate's salary axis and
        # rank_gate's HARD DOWNGRADE (e), so writing them would be worse than
        # the blank they replace. Existing rows simply re-acquire salary the
        # next time discovery sees them; see services/salary.py's docstring.

        if dry_run:
            db.rollback()
            print("(dry run -- nothing written)")
        else:
            db.commit()
        print(f"roles: {stats['labelled']} location labels, {stats['distanced']} distances, "
              f"{stats['salaried']} salaries parsed")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
