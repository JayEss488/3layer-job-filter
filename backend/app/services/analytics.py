"""Lightweight usage analytics for the closed beta.

`log_event` appends one EventLog row at the existing action seams (login, search
kickoff, tick/cross/ignore/apply, profile creation). It is deliberately
best-effort: analytics must never break a user action, so any failure is swallowed
and rolled back rather than propagated. The owner reads it back via
routers/admin.py (GET /admin/analytics)."""
import json

from sqlalchemy.orm import Session

from ..models import EventLog


def log_event(
    db: Session,
    user_id: int,
    profile_id: int | None,
    event_type: str,
    payload: dict | None = None,
) -> None:
    """Append a usage event. Best-effort: never raises into the caller.

    Commits its own row so it survives even if the surrounding request rolls back,
    and so a failure here can't poison the caller's pending transaction."""
    try:
        db.add(
            EventLog(
                user_id=user_id,
                profile_id=profile_id,
                event_type=event_type,
                payload=json.dumps(payload) if payload else None,
            )
        )
        db.commit()
    except Exception:  # noqa: BLE001 -- analytics must never break a user action
        db.rollback()
