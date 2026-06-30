"""Memory weight system (implementation notes section 3).

Feedback never retrains a model; it nudges per-attribute weights that bias the
engine's semantic + AI steps. feedback_log is append-only."""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import DELTAS, WEIGHT_MAX, WEIGHT_MIN
from ..models import FeedbackLog, ProfileAttribute, Role

# Types whose values are short keywords worth string-matching against a role.
_MATCHABLE_TYPES = {"past_role", "skill", "target_role", "seniority"}


def _clamp(w: float) -> float:
    return max(WEIGHT_MIN, min(WEIGHT_MAX, w))


def _role_haystack(role: Role) -> str:
    parts = [role.title or "", role.company or "", role.ai_analysis or ""]
    if isinstance(role.tags, list):
        parts.extend(str(t) for t in role.tags)
    return " ".join(parts).lower()


def match_role_to_attributes(
    db: Session, profile_id: int, role: Role
) -> list[ProfileAttribute]:
    """Start simple: keyword/tag overlap between the role and profile attributes.
    Can later upgrade to embedding similarity without touching callers."""
    haystack = _role_haystack(role)
    attrs = db.execute(
        select(ProfileAttribute).where(ProfileAttribute.profile_id == profile_id)
    ).scalars().all()

    matched = []
    for attr in attrs:
        if attr.type not in _MATCHABLE_TYPES:
            continue
        needle = attr.value.lower().strip()
        if not needle:
            continue
        # word-boundary-ish containment so "java" doesn't match "javascript" loosely
        if re.search(r"\b" + re.escape(needle) + r"\b", haystack):
            matched.append(attr)
    return matched


def apply_feedback(db: Session, profile_id: int, role: Role, action: str) -> None:
    """1) log it (append-only)  2) find matched attrs  3) nudge weights, clamped."""
    db.add(FeedbackLog(profile_id=profile_id, role_id=role.id, action=action))

    if action not in DELTAS:
        return
    delta = DELTAS[action]
    for attr in match_role_to_attributes(db, profile_id, role):
        attr.weight = _clamp(attr.weight + delta)


def clear_memory(db: Session, profile_id: int, wipe_log: bool = False) -> None:
    """Reset all weights for the profile to the default. Keep attributes."""
    from ..config import DEFAULT_WEIGHT

    attrs = db.execute(
        select(ProfileAttribute).where(ProfileAttribute.profile_id == profile_id)
    ).scalars().all()
    for attr in attrs:
        attr.weight = DEFAULT_WEIGHT
    if wipe_log:
        db.query(FeedbackLog).filter(FeedbackLog.profile_id == profile_id).delete()
