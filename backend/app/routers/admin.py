"""Owner-only usage analytics. Guarded by a shared secret (the ADMIN_TOKEN env
var, sent as the `X-Admin-Token` header), NOT by user login -- there is no admin
user account in the beta. Reads the EventLog stream (services/analytics.py) and
rolls it up per user. If ADMIN_TOKEN is unset the endpoint is locked (403)."""
import hmac
import json
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import ADMIN_TOKEN, BETA_WINDOW_DAYS
from ..database import get_db
from ..models import EventLog, FeedbackResponse, Profile, SearchRun, SignupSurvey, User
from ..services.beta import has_answered_exit_survey, window_for

router = APIRouter(prefix="/admin", tags=["admin"])

# Map an EventLog.event_type slug to the per-user counter it increments.
_EVENT_FIELDS = {
    "login": "logins",
    # Self-serve arrivals. Kept separate from "login" so the rollup can answer
    # "how many NEW people" without subtracting one series from another -- a
    # returning user's sign-in is a login, their first ever is a signup.
    "signup": "signups",
    "signup_survey": "survey_answers",
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
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Analytics endpoint is disabled (ADMIN_TOKEN not set).")
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


@router.get("/signups")
def signups(_: None = Depends(_check_admin), db: Session = Depends(get_db)):
    """Every account with its sign-up survey answers, newest first.

    This is the "shown via admin checking function" half of self-serve sign-up:
    with no manual approval step in the flow, this endpoint is the ONLY place
    anyone finds out who has arrived and what they said. Read it with:

        curl -H "X-Admin-Token: $ADMIN_TOKEN" https://<host>/admin/signups

    `answered: false` is deliberately reported rather than filtered out. The
    survey is enforced only in the frontend (see routers/auth_router.py for why),
    so an unanswered row is a real signal -- either someone bypassed the page or
    the gate is broken -- and silently omitting those rows would make a broken
    gate look like low sign-up volume.

    `method` is the sign-up path: "google", "apple", "email", or "legacy" for a
    hand-assigned beta credential. Legacy accounts appear with no answers; they
    were never asked. `email_verified` is False for every "email" account by
    design -- see the field's comment below."""
    surveys = {
        s.user_id: s
        for s in db.execute(select(SignupSurvey)).scalars().all()
    }
    users = db.execute(select(User).order_by(User.created_at.desc(), User.id.desc())).scalars().all()

    rows = []
    for u in users:
        s = surveys.get(u.id)
        w = window_for(u)
        rows.append({
            "user_id": u.id,
            "username": u.username,
            "email": u.email or "",
            "display_name": u.display_name or "",
            # From auth_provider, not inferred from which columns are populated:
            # an email+password account and a legacy hand-assigned one are
            # byte-identical in shape, and calling both "password" would hide
            # every self-serve email signup inside the legacy count. NULL is a
            # legacy account (see models.User).
            "method": u.auth_provider or "legacy",
            # False for every email+password account -- there is no verification
            # email in this deployment (config.EMAIL_SIGNUP_ENABLED). Reported so
            # an address nobody has proved they own is never read here as a
            # confirmed contact address.
            "email_verified": bool(u.email_verified),
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
            "answered": s is not None,
            # Q1: what matters most right now (config.SIGNUP_PRIORITY_CHOICES).
            "priority": s.priority if s else None,
            # Q2: used another AI job search tool before (outside chatbots)?
            "used_ai_tool": s.used_ai_tool if s else None,
            "answered_at": s.created_at.isoformat() if s and s.created_at else None,
            # ── Beta window ──────────────────────────────────────────────────
            # All null/False for a legacy account, which is the exemption
            # working rather than missing data.
            "beta_started_at": w["started_at"].isoformat() if w["started_at"] else None,
            "beta_expires_at": w["expires_at"].isoformat() if w["expires_at"] else None,
            "beta_days_left": w["days_left"],
            "beta_expired": w["expired"],
            "exit_survey_due": w["past_survey_day"],
            "exit_survey_answered": has_answered_exit_survey(db, u.id),
        })

    # Every account that was ASKED the sign-up survey, i.e. every self-serve
    # account whatever provider it came in through. This used to be
    # `method == "google"` because that was the only self-serve path; leaving it
    # that way would have made survey_outstanding silently ignore Apple and
    # email signups, so a broken gate on those paths would look like nobody
    # using them. Kept in sync with auth_router._needs_survey by construction --
    # both key on auth_provider being set.
    self_serve_rows = [r for r in rows if r["method"] != "legacy"]
    answered = [r for r in rows if r["answered"]]
    priority_counts = defaultdict(int)
    for r in answered:
        priority_counts[r["priority"]] += 1

    # Of the accounts actually ASKED the wrap-up survey (past day
    # EXIT_SURVEY_AFTER_DAYS, window not NULL) -- the honest denominator, same
    # principle as survey_outstanding below.
    exit_asked = [r for r in rows if r["exit_survey_due"]]

    return {
        "totals": {
            "accounts": len(rows),
            "self_serve": len(self_serve_rows),
            "legacy_password": len(rows) - len(self_serve_rows),
            # Which of the three self-serve paths people actually take. This is
            # the only measurement of whether adding Apple and email was worth
            # it -- before them, everyone who wouldn't use Google left at the
            # first screen and was never counted anywhere.
            "by_method": {m: sum(1 for r in rows if r["method"] == m)
                          for m in sorted({r["method"] for r in rows})},
            "survey_answered": len(answered),
            # Of the accounts that were ASKED (every self-serve account) -- the
            # honest denominator for whether the /welcome gate is working.
            "survey_outstanding": sum(1 for r in self_serve_rows if not r["answered"]),
            # Beta window. `legacy_no_window` is the count of exempt accounts;
            # it should equal the number of testers who predate the switch, and
            # a number climbing above that means beta_started_at is not being
            # stamped on sign-up.
            "beta_window_days": BETA_WINDOW_DAYS,
            "in_window": sum(1 for r in rows if r["beta_started_at"] and not r["beta_expired"]),
            "expired": sum(1 for r in rows if r["beta_expired"]),
            "legacy_no_window": sum(1 for r in rows if not r["beta_started_at"]),
            "exit_survey_asked": len(exit_asked),
            "exit_survey_answered": sum(1 for r in exit_asked if r["exit_survey_answered"]),
        },
        "priority_counts": dict(priority_counts),
        "used_ai_tool_yes": sum(1 for r in answered if r["used_ai_tool"]),
        "used_ai_tool_no": sum(1 for r in answered if r["used_ai_tool"] is False),
        "signups": rows,
    }


class BetaWindowAction(BaseModel):
    # "extend"  -- push the window out by `days` from now
    # "restart" -- start a fresh window from now
    # "clear"   -- remove the window entirely (makes the account exempt)
    action: str
    days: int = BETA_WINDOW_DAYS


@router.post("/users/{user_id}/beta")
def set_beta_window(
    user_id: int,
    body: BetaWindowAction,
    _: None = Depends(_check_admin),
    db: Session = Depends(get_db),
):
    """Adjust one account's beta window. The escape hatch, and it is required.

    Access lapsing at day 7 is a real 403 across every data router, so without
    this there is no way to give a tester more time, un-expire someone locked
    out by a mistake, or reopen an account to chase a bug they reported. All
    three are ordinary things to need during a beta.

    `clear` sets beta_started_at to NULL, which is the same "no window" state
    the original testers are in -- permanent access, never asked the wrap-up
    survey again."""
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    action = (body.action or "").strip()
    now = datetime.utcnow()
    if action == "clear":
        user.beta_started_at = None
    elif action == "restart":
        user.beta_started_at = now
    elif action == "extend":
        # Move day 0 forward so the window ends `days` from now. Works whether
        # the account is mid-window, already expired, or has no window at all.
        user.beta_started_at = now + timedelta(days=body.days) - timedelta(days=BETA_WINDOW_DAYS)
    else:
        raise HTTPException(status_code=422, detail="action must be one of: extend, restart, clear")

    db.commit()
    db.refresh(user)
    w = window_for(user)
    return {
        "user_id": user.id,
        "username": user.username,
        "action": action,
        "beta_started_at": w["started_at"].isoformat() if w["started_at"] else None,
        "beta_expires_at": w["expires_at"].isoformat() if w["expires_at"] else None,
        "beta_days_left": w["days_left"],
        "beta_expired": w["expired"],
    }


@router.get("/analytics")
def analytics(_: None = Depends(_check_admin), db: Session = Depends(get_db)):
    """Per-user activity rollup + headline totals, newest sign-ups first."""
    users = db.execute(select(User).order_by(User.id)).scalars().all()

    # counts and last-activity per (user, event_type), one grouped query
    grouped = db.execute(
        select(
            EventLog.user_id,
            EventLog.event_type,
            func.count(EventLog.id),
            func.max(EventLog.created_at),
        ).group_by(EventLog.user_id, EventLog.event_type)
    ).all()

    counts: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    last_active: dict[int, datetime] = {}
    for user_id, event_type, n, last in grouped:
        field = _EVENT_FIELDS.get(event_type)
        if field:
            counts[user_id][field] += int(n)
        if last and (user_id not in last_active or last > last_active[user_id]):
            last_active[user_id] = last

    cutoff = datetime.utcnow() - timedelta(days=7)
    per_user = []
    totals = defaultdict(int)
    active_7d = 0
    for u in users:
        c = counts.get(u.id, {})
        la = last_active.get(u.id)
        w = window_for(u)
        row = {
            "user_id": u.id,
            "username": u.username,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_active": la.isoformat() if la else None,
            # Where this account is in its beta window. None/False throughout for
            # a legacy account with no window -- see models.User.beta_started_at.
            "beta_days_left": w["days_left"],
            "beta_expired": w["expired"],
            **{k: c.get(k, 0) for k in _COUNTER_FIELDS},
        }
        per_user.append(row)
        for k in _COUNTER_FIELDS:
            totals[k] += row[k]
        if la and la >= cutoff:
            active_7d += 1

    # never-logged-in users are the most useful signal at the top
    per_user.sort(key=lambda r: (r["last_active"] is not None, r["last_active"] or "", r["user_id"]))

    # Beta-tester product feedback (bugs/suggestions about the app, see
    # routers/profiles.py::submit_comment) -- newest first, with the actual
    # text so the owner can just read them here rather than digging through
    # the raw event_log table. Username joined in directly since this list is
    # small (a beta's worth of comments, not the full event stream).
    usernames = {u.id: u.username for u in users}
    comment_rows = db.execute(
        select(EventLog.user_id, EventLog.profile_id, EventLog.payload, EventLog.created_at)
        .where(EventLog.event_type == "beta_comment")
        .order_by(EventLog.created_at.desc())
    ).all()
    comments = []
    for user_id, profile_id, payload, created_at in comment_rows:
        try:
            text = json.loads(payload).get("text", "") if payload else ""
        except (TypeError, ValueError):
            text = ""
        comments.append({
            "user_id": user_id,
            "username": usernames.get(user_id, f"user #{user_id}"),
            "profile_id": profile_id,
            "text": text,
            "created_at": created_at.isoformat() if created_at else None,
        })

    runs, run_stats = _runs_report(db, usernames)
    feedback = _feedback_report(db, usernames)

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "totals": {
            "users": len(users),
            "active_last_7d": active_7d,
            "searches": totals["searches"],
            "ticks": totals["ticks"],
            "crosses": totals["crosses"],
            "applies": totals["applies"],
            "outcomes": totals["outcomes"],
            "logins": totals["logins"],
            "comments": len(comments),
            "feedback_responses": len(feedback),
            "exit_surveys": totals["exit_surveys"],
        },
        "users": per_user,
        "comments": comments,
        # Roles found per run -- both the raw list and the summary, because
        # "runs average 7" and "one run found 12 and three found 1" are very
        # different situations that the average alone cannot distinguish.
        "runs": runs,
        "run_stats": run_stats,
        "feedback": feedback,
    }


def _runs_report(db: Session, usernames: dict[int, str]) -> tuple[list[dict], dict]:
    """Every search run with how many roles it surfaced, newest first.

    `roles_found` is SearchRun.result_count, which the engine already writes at
    the end of a successful run -- there is no need to count Role rows, and
    counting them would give a DIFFERENT number in both directions (picks the
    user already saved in an earlier run are counted but not re-persisted, while
    retained quick-scored leftovers are persisted but not counted).

    STATUS IS REPORTED ALONGSIDE IT and must stay that way: result_count is only
    written on the `done` path, so a cancelled or errored run reads 0 and would
    otherwise look like a run that searched properly and found nothing.

    SearchRun has no user_id, so this joins through Profile -- the same join
    routers/search.py::_searches_today uses for the per-user daily cap."""
    rows = db.execute(
        select(
            SearchRun.id,
            SearchRun.profile_id,
            SearchRun.status,
            SearchRun.result_count,
            SearchRun.started_at,
            SearchRun.finished_at,
            Profile.user_id,
        )
        .join(Profile, SearchRun.profile_id == Profile.id)
        .order_by(SearchRun.id.desc())
    ).all()

    runs = []
    for run_id, profile_id, status, result_count, started, finished, user_id in rows:
        runs.append({
            "run_id": run_id,
            "user_id": user_id,
            "username": usernames.get(user_id, f"user #{user_id}"),
            "profile_id": profile_id,
            "status": status,
            "roles_found": int(result_count or 0),
            "started_at": started.isoformat() if started else None,
            "finished_at": finished.isoformat() if finished else None,
            "duration_s": round((finished - started).total_seconds(), 1)
            if started and finished else None,
        })

    # Summary over COMPLETED runs only -- folding in the zeros from cancelled and
    # errored runs would drag the average toward a number no run ever produced.
    found = [r["roles_found"] for r in runs if r["status"] == "done"]
    stats = {
        "runs_total": len(runs),
        "runs_done": len(found),
        "runs_cancelled": sum(1 for r in runs if r["status"] == "cancelled"),
        "runs_error": sum(1 for r in runs if r["status"] == "error"),
        "runs_running": sum(1 for r in runs if r["status"] == "running"),
        "roles_found_avg": round(statistics.mean(found), 1) if found else None,
        "roles_found_median": statistics.median(found) if found else None,
        "roles_found_min": min(found) if found else None,
        "roles_found_max": max(found) if found else None,
        # How many done runs produced each count. This is the shape that answers
        # "does a run find 2 roles or 12", which an average cannot.
        "roles_found_distribution": {
            str(n): found.count(n) for n in sorted(set(found))
        },
        # The failure worth seeing on its own: a run that completed normally and
        # still surfaced nothing.
        "runs_done_with_zero": sum(1 for n in found if n == 0),
    }
    return runs, stats


def _feedback_report(db: Session, usernames: dict[int, str]) -> list[dict]:
    """Every feedback answer from all three surfaces, newest first.

    One list rather than one per surface: the point of the shared store is that
    filtering by `question_id` or `user_id` needs no new tooling, and splitting
    it here would put that back."""
    rows = db.execute(
        select(FeedbackResponse).order_by(FeedbackResponse.id.desc())
    ).scalars().all()

    out = []
    for r in rows:
        answer: object = r.answer
        # Multi-select answers are stored JSON-encoded; decode so the consumer
        # never has to know which questions are lists.
        if r.answer.startswith("["):
            try:
                answer = json.loads(r.answer)
            except (TypeError, ValueError):
                pass
        out.append({
            "id": r.id,
            "user_id": r.user_id,
            "username": usernames.get(r.user_id, f"user #{r.user_id}"),
            "profile_id": r.profile_id,
            "run_id": r.run_id,
            "surface": r.surface,
            "question_id": r.question_id,
            "answer": answer,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })
    return out
