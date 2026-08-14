"""One-off backfill: append the §requirements tick/cross block to role cards that
predate it.

The judge's requirements checklist has always been persisted, on
JobSeen.eval_analysis. What was missing was any way to SEE it: the card renders
Role.ai_analysis, which engine._compose_analysis flattens into a string at persist
time, so a role already on the page carries whatever markers existed on the day it
was surfaced and will never gain a new one -- for a saved or applied role, never at
all. Measured on this store when the block shipped: 199 roles with an analysis, 0
carrying §requirements.

That matters more than an ordinary cosmetic gap, because the block exists to answer
a specific complaint: filters_on is capped at 5 and is a requirement -> evidence
MAPPING, not an inventory, so a reader takes it for the list of requirements found
and concludes the judge missed the ones below the cut. On the listing that prompted
this work all 12 requirements were correctly on the checklist -- including the
Bachelors degree the card was reported as "missing" -- and five reached the page.
Those cards are still on screen; leaving them as they are leaves the complaint true
for every role already surfaced.

APPENDS, NEVER RE-COMPOSES. Re-running _compose_analysis from eval_analysis would be
the obvious approach and is wrong: that function also reads _cluster_label,
_ghost_signals and _scam_caution, which live on the run's in-memory job dict and are
NOT in eval_analysis, so a wholesale re-compose would silently drop the "Matched
via" note and, worse, the ghost and caution explanations -- and a ghost/caution chip
whose backing text has vanished is precisely the unexplainable accusation those
blocks exist to prevent. So this only ever adds one block to the end of an existing
string and leaves every byte before it alone.

No API calls, no LLM, no network -- it re-renders data already on the row. Safe to
re-run: a role whose analysis already contains the marker is skipped, so this is
idempotent and cannot double up.

Roles are matched to their store row by (profile_id, url), the same pairing
engine.py's own role/jobs_seen joins use.

Run:  venv/Scripts/python scripts/backfill_role_requirements_block.py [--dry-run]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from app.database import SessionLocal, init_db                # noqa: E402
from app.models import JobSeen, Role                          # noqa: E402
from app.services.engine import requirements_block            # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv
    init_db()
    db = SessionLocal()
    try:
        roles = (
            db.query(Role)
            # Soft-deleted rows (status "deleted", kept for the FeedbackLog
            # referent) never render, so there is nothing to gain by rewriting them.
            .filter(Role.ai_analysis.isnot(None), Role.status != "deleted")
            .all()
        )
        # One indexed pass over the store rows these roles point at, rather than a
        # query per role -- the same shape as engine._enrich_pre_gate's write-back.
        wanted = {(r.profile_id, r.url) for r in roles if r.url}
        seen: dict[tuple[int, str], str] = {}
        if wanted:
            for js in (
                db.query(JobSeen)
                .filter(JobSeen.profile_id.in_({p for p, _ in wanted}),
                        JobSeen.eval_analysis.isnot(None))
                .all()
            ):
                key = (js.profile_id, js.url)
                if key in wanted:
                    seen[key] = js.eval_analysis

        stats = {"updated": 0, "already": 0, "no_verdict": 0, "no_items": 0}
        for role in roles:
            if "§requirements" in (role.ai_analysis or ""):
                stats["already"] += 1
                continue
            raw = seen.get((role.profile_id, role.url or ""))
            if not raw:
                stats["no_verdict"] += 1
                continue
            try:
                entry = json.loads(raw)
            except Exception:
                stats["no_verdict"] += 1
                continue
            lines = requirements_block(entry.get("requirements"))
            if not lines:
                stats["no_items"] += 1
                continue
            role.ai_analysis = "\n".join([role.ai_analysis.rstrip(), *lines])
            stats["updated"] += 1

        if dry_run:
            db.rollback()
            print("(dry run -- nothing written)")
        else:
            db.commit()
        print(f"roles: {stats['updated']} gained a requirements block, "
              f"{stats['already']} already had one, "
              f"{stats['no_verdict']} had no stored verdict to read, "
              f"{stats['no_items']} had a verdict with an empty checklist")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
