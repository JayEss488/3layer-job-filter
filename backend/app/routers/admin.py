"""Maintenance endpoints: the direct-employer ATS crawl, the daily observation
pass, and the listing liveness re-check.

These are operator actions rather than part of using the app, which is why they
sit behind their own prefix. Each has a command-line equivalent under scripts/ --
the endpoints exist so an external scheduler (cron, Task Scheduler, a CI job) can
drive them over HTTP.

Guarded by ADMIN_TOKEN (sent as the `X-Admin-Token` header) when one is set. With
no token set they are OPEN, which is correct for a tool bound to localhost and
wrong the moment it is not: set ADMIN_TOKEN if this process is reachable by
anyone else.
"""
import hmac

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import ADMIN_TOKEN
from ..database import get_db

router = APIRouter(prefix="/admin", tags=["admin"])

# Map an EventLog.event_type slug to the per-user counter it increments.
_EVENT_FIELDS = {
    "login": "logins",
    # Self-serve arrivals. Kept separate from "login" so the rollup can answer
    # "how many NEW people" without subtracting one series from another -- a
    # returning user's sign-in is a login, their first ever is a signup.
    "signup": "signups",
    "signup_survey": "survey_answers",
    # Email-account lifecycle. `verifications` is the ONLY end-to-end evidence
    # that transactional mail is actually being delivered -- Resend accepting a
    # send says nothing about it reaching an inbox, and a spam-foldered
    # verification email looks exactly like users who couldn't be bothered.
    # Watch it against `signups` on email accounts, not on its own.
    "email_verified": "verifications",
    "password_reset": "password_resets",
    "search_started": "searches",
    "role_tick": "ticks",
    "role_cross": "crosses",
    "role_ignore": "ignores",
    "role_apply": "applies",
    "profile_created": "profiles_created",
    "beta_comment": "comments",
    # Ground truth for ghost-listing calibration -- the only outcome data this
    # app collects. Counted per user so it is visible whether anyone is actually
    # reporting outcomes at all, which is the thing most likely to quietly fail.
    "application_outcome": "outcomes",
    # The two in-run prompts, and the day-4 wrap-up survey. Counted per user for
    # the same reason as outcomes: a prompt that has silently stopped firing
    # looks identical to users who just never answer, and only the per-user
    # spread tells them apart.
    "feedback_response": "feedback",
    "exit_survey": "exit_surveys",
}

# Every per-user counter, in display order. Derived from _EVENT_FIELDS so adding
# an event type above is the ONLY edit needed -- the row builder and the totals
# fold both read this, rather than repeating the key list twice more.
_COUNTER_FIELDS = list(dict.fromkeys(_EVENT_FIELDS.values()))


def _check_admin(x_admin_token: str | None = Header(None)) -> None:
    """Allow the request through when no ADMIN_TOKEN is configured.

    Deliberately open-by-default: this app has no login, and demanding a token
    for the maintenance endpoints while every data route is open would be
    security theatre rather than security. Setting ADMIN_TOKEN turns it into a
    real check, which is what you want if the process is not on localhost."""
    if not ADMIN_TOKEN:
        return
    if not x_admin_token or not hmac.compare_digest(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/crawl")
def run_direct_employer_crawl(
    limit: int = 200,
    _: None = Depends(_check_admin),
    background: BackgroundTasks = None,  # type: ignore[assignment]
):
    """Kick off one pass of the direct-employer ATS-detection crawl.

    Owner-triggered (or driven by an EXTERNAL scheduler against this endpoint --
    see scripts/crawl_direct_employers.py for why there is no in-process timer).
    Returns immediately: a 200-domain pass is minutes of network wall time, far
    past any sane request timeout, so it runs as a BackgroundTask and progress
    is read back from GET /admin/crawl."""
    from ..services.direct_employer import crawl_uk_charities

    def _run() -> None:
        try:
            summary = crawl_uk_charities(limit=limit)
            print(f"[crawl] direct-employer pass done: {summary}")
        except Exception as e:  # never let a background crawl kill the process
            print(f"[crawl] direct-employer pass failed: {e!r}")

    background.add_task(_run)
    return {"started": True, "limit": limit}


@router.get("/crawl")
def direct_employer_crawl_status(_: None = Depends(_check_admin)):
    """Progress and hit rate for the direct-employer crawl."""
    from ..services.direct_employer import crawl_status

    return crawl_status()


@router.post("/observe")
def run_observation_pass(
    terms: int = 0,
    _: None = Depends(_check_admin),
    background: BackgroundTasks = None,  # type: ignore[assignment]
    db: Session = Depends(get_db),
):
    """Kick off one listing-observation pass -- the collection half of
    ghost-job detection (see services/observe.py).

    POINT AN EXTERNAL SCHEDULER AT THIS, DAILY. The evergreen/standing-ad rule
    divides days-seen by days-since-discovery, so a missed day inflates the
    denominator while the numerator stands still: gaps don't merely delay the
    signal, they suppress it. There is no in-process timer, for the same reason
    the direct-employer crawl has none.

    Costs board-API quota and zero OpenAI. Writes only listing_observations --
    never jobs_seen, never a Role, never a SearchRun, so it cannot consume
    anyone's daily search allowance. Returns immediately; read progress back
    from GET /observe."""
    from ..services import observe

    limit = terms or observe.OBSERVE_TERMS_PER_RUN

    def _run() -> None:
        from ..database import SessionLocal
        session = SessionLocal()
        try:
            print(f"[observe] pass done: {observe.run_pass(session, limit_terms=limit)}")
        except Exception as e:  # never let a background pass kill the process
            print(f"[observe] pass failed: {e!r}")
        finally:
            session.close()

    background.add_task(_run)
    return {"started": True, "terms": limit}


@router.get("/observe")
def observation_status(_: None = Depends(_check_admin), db: Session = Depends(get_db)):
    """Observation coverage.

    `distinct_observation_days` is the field to actually watch: a crawl that has
    silently stopped looks healthy in every other number, and it is the one
    failure mode whose cost cannot be recovered later."""
    from ..services import observe

    return observe.observe_status(db)


@router.post("/recheck")
def run_liveness_recheck(
    limit: int = 0,
    dry_run: bool = False,
    _: None = Depends(_check_admin),
    background: BackgroundTasks = None,  # type: ignore[assignment]
):
    """Re-check tracked listings for liveness and stamp dead_at.

    Plain HTTP GETs, no browser, no LLM, no API credits. Fail-open --
    unverifiable is never recorded as dead. A confirmed death fans out to
    jobs_seen (so the pipeline's existing dead_reason filters exclude it for
    free) and retires any already-shown, still-unreviewed card to `ignored`,
    which is reversible.

    Prefer `scripts/recheck_liveness.py --dry-run` the first time: dead_reason
    cannot be undone."""
    from ..services import observe

    n = limit or observe.OBSERVE_RECHECK_MAX_PER_PASS

    def _run() -> None:
        from ..database import SessionLocal
        session = SessionLocal()
        try:
            print(f"[recheck] pass done: "
                  f"{observe.recheck_liveness(session, limit=n, dry_run=dry_run)}")
        except Exception as e:
            print(f"[recheck] pass failed: {e!r}")
        finally:
            session.close()

    background.add_task(_run)
    return {"started": True, "limit": n, "dry_run": dry_run}
