"""The fixed open-beta window: two gates off one clock.

`User.beta_started_at` is day 0. Everything else is derived from it here, so
changing the window length in config applies to everyone already inside it --
which is why no expiry date is ever stored on the row.

**The two gates are independent and must stay that way.**

* **The wrap-up survey gate** (`needs_exit_survey`, from
  `EXIT_SURVEY_AFTER_DAYS`). Client-side, exactly like the sign-up survey: the
  frontend holds the user on /exit-survey until they answer, then releases them
  back into the app for the rest of their window. It is deliberately NOT a
  server rejection while the user is still inside their window -- see the
  routers/auth_router.py docstring: a server that 403s until a survey is
  answered also 403s the submission, and surfaces as an auth failure the
  frontend reads as a logged-out session.
* **The lapse gate** (`require_active_beta`, from `BETA_WINDOW_DAYS`).
  Server-side, a real 403, applied to every data router.

`needs_exit_survey` does not consult `expired`, and that is the load-bearing
part: a user who never logs in between day 4 and day 7 hits both gates at once,
and must still be shown the survey rather than a bare "access ended" wall. The
day-4 trigger exists precisely so collecting a response does not depend on
someone logging in on one specific day.

NULL `beta_started_at` means NO WINDOW -- never expires, never asked. That is
how the original beta testers stay exempt (see models.User).
"""
import math
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import BETA_WINDOW_DAYS, EXIT_SURVEY_AFTER_DAYS
from ..database import get_db
from ..models import FeedbackResponse, User
from ..services.auth import current_user_id_or_none

# The surface every wrap-up answer is stored under, and the key that decides
# whether this user has answered. Kept here rather than restated at each call
# site so the gate and the writer can never disagree about what "answered"
# means -- the same reason SOFT_GATE_AXES lives in one module.
EXIT_SURFACE = "exit"


def window_for(user: User | None) -> dict:
    """This account's beta window as plain values.

    Every field is None/False for an account with no window (`beta_started_at`
    NULL), which is the legacy exemption -- callers can render the result
    without special-casing it."""
    started = getattr(user, "beta_started_at", None) if user else None
    if not started:
        return {
            "started_at": None,
            "expires_at": None,
            "days_left": None,
            "expired": False,
            "survey_due_at": None,
            "past_survey_day": False,
        }

    expires_at = started + timedelta(days=BETA_WINDOW_DAYS)
    survey_due_at = started + timedelta(days=EXIT_SURVEY_AFTER_DAYS)
    now = datetime.utcnow()
    # Rounded UP, so someone with 6 hours left is told "1 day" rather than "0
    # days" while the app still works. Clamped at 0 for an already-expired
    # window: a negative number on a card reads as a bug, not as information.
    seconds_left = (expires_at - now).total_seconds()
    days_left = math.ceil(seconds_left / 86400) if seconds_left > 0 else 0

    return {
        "started_at": started,
        "expires_at": expires_at,
        "days_left": days_left,
        "expired": now >= expires_at,
        "survey_due_at": survey_due_at,
        "past_survey_day": now >= survey_due_at,
    }


def has_answered_exit_survey(db: Session, user_id: int) -> bool:
    """True once ANY wrap-up answer exists for this user.

    Any, not all: the free-text question is optional, so requiring a full set
    would leave a user who legitimately skipped it stuck on the page forever."""
    return db.execute(
        select(FeedbackResponse.id).where(
            FeedbackResponse.user_id == user_id,
            FeedbackResponse.surface == EXIT_SURFACE,
        ).limit(1)
    ).scalar_one_or_none() is not None


def needs_exit_survey(db: Session, user: User | None) -> bool:
    """True when this user should be held on /exit-survey.

    Independent of expiry on purpose -- see the module docstring."""
    if not user or not window_for(user)["past_survey_day"]:
        return False
    return not has_answered_exit_survey(db, user.id)


def beta_fields(db: Session, user: User | None) -> dict:
    """The beta half of a /me or login response, ready to splat into a schema."""
    w = window_for(user)
    return {
        "beta_expires_at": w["expires_at"],
        "beta_days_left": w["days_left"],
        "beta_expired": w["expired"],
        "needs_exit_survey": needs_exit_survey(db, user),
    }


# ── The lapse gate ───────────────────────────────────────────────────────────
def require_active_beta(db: Session = Depends(get_db)) -> None:
    """Router-level dependency: 403 once this account's window has lapsed.

    Sits alongside require_authenticated on the data routers (see main.py).
    Deliberately NOT on the auth router (/me, /signup/survey, /exit/survey must
    stay reachable so an expired user can still answer) nor on the feedback
    router (a prompt answer already typed must never be lost to a gate).

    An unauthenticated request is let through here -- require_authenticated is
    the dependency that rejects those, and duplicating that job would turn a
    missing token into a confusing "beta expired" message.

    The detail is a DICT carrying a machine-readable `code`, not a bare string.
    The frontend has to tell this apart from an ordinary 403 to route the user
    to the survey rather than showing a raw error, and a code in the body does
    that without a custom response header (which would also need adding to the
    CORS middleware's expose_headers)."""
    uid = current_user_id_or_none()
    if uid is None:
        return

    user = db.get(User, uid)
    if not window_for(user)["expired"]:
        return

    raise HTTPException(
        status_code=403,
        detail={
            "code": "beta_expired",
            "message": (
                f"Your {BETA_WINDOW_DAYS}-day beta access has ended. "
                "Thanks for testing Four in a Thousand."
            ),
        },
    )
