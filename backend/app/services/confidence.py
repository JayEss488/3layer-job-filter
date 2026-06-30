"""Profile completeness score (implementation notes section 6). Not ML."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import CONFIDENCE_REQUIRED, TYPE_LABELS
from ..models import ProfileAttribute


def _types_present(db: Session, profile_id: int) -> set[str]:
    rows = db.execute(
        select(ProfileAttribute.type)
        .where(ProfileAttribute.profile_id == profile_id)
        .distinct()
    ).all()
    return {r[0] for r in rows}


def _build_tip(missing: list[str]) -> str:
    if not missing:
        return "Your profile looks ready. Run a search."
    labels = [TYPE_LABELS.get(t, t) for t in missing]
    if len(labels) == 1:
        return f"Add {labels[0]} to improve results."
    return "Add " + ", ".join(labels[:-1]) + f" and {labels[-1]} to improve results."


def confidence(db: Session, profile_id: int) -> dict:
    present = _types_present(db, profile_id)
    score = sum(w for t, w in CONFIDENCE_REQUIRED.items() if t in present)
    missing = [t for t in CONFIDENCE_REQUIRED if t not in present]
    return {"score": round(score * 100), "missing": missing, "tip": _build_tip(missing)}
