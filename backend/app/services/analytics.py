"""Lightweight usage analytics for the closed beta.

`log_event` appends one EventLog row at the existing action seams (login, search
kickoff, tick/cross/ignore/apply, profile creation). It is deliberately
best-effort: analytics must never break a user action, so any failure is swallowed
and rolled back rather than propagated. The owner reads it back via
routers/admin.py (GET /admin/analytics).

`record_feedback` is the write side of the beta feedback store -- one row per
answered question from any of the three surfaces (sign-up, in-run prompts,
wrap-up survey). It sits here rather than in its own module because it is the
same kind of thing at the same seams, and because every caller that records
feedback also wants to log_event alongside it.

The two differ in one important way: log_event is best-effort and swallows
failures, because losing an analytics row must never break a user action.
record_feedback DOES raise, because the user pressed a button expecting their
answer to be saved and silently dropping it would show them a success state for
something that did not happen."""
import json

from sqlalchemy.orm import Session

from ..models import EventLog, FeedbackResponse


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


def record_feedback(
    db: Session,
    user_id: int,
    surface: str,
    question_id: str,
    answer: str | list[str],
    profile_id: int | None = None,
    run_id: int | None = None,
) -> FeedbackResponse | None:
    """Store one answer. Returns the row, or None for a blank answer.

    A blank answer is dropped rather than stored as an empty string: the
    wrap-up survey's free-text question is optional, and an empty row would be
    indistinguishable in the admin readout from someone who wrote nothing
    meaningful -- it would inflate the response count with silence.

    Multi-select answers are JSON-encoded so `answer` stays one text column for
    every question shape; the admin readout decodes on the way out.

    Does NOT commit -- the caller batches several answers into one transaction
    (a survey is three questions) and commits once."""
    if isinstance(answer, list):
        if not answer:
            return None
        value = json.dumps(answer)
    else:
        value = (answer or "").strip()
        if not value:
            return None

    row = FeedbackResponse(
        user_id=user_id,
        profile_id=profile_id,
        run_id=run_id,
        surface=surface,
        question_id=question_id,
        answer=value,
    )
    db.add(row)
    return row
