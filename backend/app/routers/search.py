"""Search kickoff + role lifecycle. The search runs as a FastAPI BackgroundTask;
the frontend polls /search/status. Role actions feed the weight system."""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import MAX_CONCURRENT_SEARCHES, MAX_SEARCHES_PER_DAY
from ..database import get_db
from ..deps import get_profile_or_404, get_role_or_404
from ..models import Profile, ProfileAttribute, Role, SearchRun
from ..schemas import (
    ApplicationStatusIn,
    RoleOut,
    SearchStartOut,
    SearchStatusOut,
)
from ..services.analytics import log_event
from ..services.engine import run_search_task
from ..services.feedback import apply_feedback

router = APIRouter(tags=["search"])

# `offer` and `no_response` were added for the ghost-listing feedback loop, and
# `offer` is not decoration: before it, every terminal state here was negative,
# and a form whose only outcomes are bad is a form people don't fill in. This
# field had never been used past "pending" in its entire life.
#
# `no_response` is the ground truth the ghost rules would eventually like to be
# calibrated against -- and it is BIASED, which must travel with it wherever it
# is read. Most applications get no reply for entirely ordinary reasons, so it
# is usable only as a rate across many rows conditioned on a fired signal, never
# as proof about any one listing. It is also not final: the other values stay
# available so a late reply can correct it.
_VALID_APP_STATUS = {"pending", "interview", "offer", "rejected", "no_response"}

# Which usage event a role lifecycle action logs (see services/analytics.py).
_FEEDBACK_EVENT = {"tick": "role_tick", "cross": "role_cross", "ignore": "role_ignore"}


def _searches_today(db: Session, user_id: int) -> int:
    """Count of this USER's searches today. The daily cap is now per-user (a beta
    has ~50 users; a single global cap would let one exhaust everyone's quota)."""
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(SearchRun.id))
        .join(Profile, SearchRun.profile_id == Profile.id)
        .filter(Profile.user_id == user_id, SearchRun.started_at >= start)
        .scalar()
        or 0
    )


@router.post("/profiles/{profile_id}/search", response_model=SearchStartOut)
def start_search(
    background: BackgroundTasks,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    already_running = db.execute(
        select(SearchRun.id)
        .where(SearchRun.profile_id == profile.id, SearchRun.status == "running")
        .limit(1)
    ).first()
    if already_running:
        raise HTTPException(
            status_code=409,
            detail="A search is already running for this profile.",
        )

    used = _searches_today(db, profile.user_id)
    if used >= MAX_SEARCHES_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily search limit reached ({MAX_SEARCHES_PER_DAY}/day). Try again tomorrow.",
        )

    # Capacity guard (see config.MAX_CONCURRENT_SEARCHES): counted off the DB
    # rather than an in-process registry because start_search commits its
    # status="running" row before returning, so two near-simultaneous kickoffs
    # can't both slip through the check the way they could against a counter
    # only populated once the background task starts executing. Orphaned rows
    # can't wedge this shut -- they're reaped on shutdown and at startup.
    in_flight = db.execute(
        select(func.count(SearchRun.id)).where(SearchRun.status == "running")
    ).scalar() or 0
    if in_flight >= MAX_CONCURRENT_SEARCHES:
        raise HTTPException(
            status_code=503,
            detail="The server is busy running other searches right now. "
                   "Please try again in a few minutes.",
        )

    # "Run first search" confirms everything onboarding parsed -- except target_role,
    # where confirmed doubles as "pinned" (survives profile_intel regeneration). Bulk-
    # confirming those here would silently pin every AI-generated role after the very
    # first search ever run, defeating the whole pin/regenerate mechanic.
    db.query(ProfileAttribute).filter(
        ProfileAttribute.profile_id == profile.id,
        ProfileAttribute.confirmed.is_(False),
        ProfileAttribute.type != "target_role",
    ).update({ProfileAttribute.confirmed: True}, synchronize_session=False)

    run = SearchRun(profile_id=profile.id, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)
    log_event(db, profile.user_id, profile.id, "search_started", {"run_id": run.id})

    background.add_task(run_search_task, profile.id, run.id)
    return SearchStartOut(
        run_id=run.id,
        status="running",
        searches_remaining=MAX_SEARCHES_PER_DAY - used - 1,
    )


@router.get("/profiles/{profile_id}/search/status", response_model=SearchStatusOut | None)
def search_status(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    run = db.execute(
        select(SearchRun)
        .where(SearchRun.profile_id == profile.id)
        .order_by(SearchRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    return run


@router.post("/profiles/{profile_id}/search/cancel", response_model=SearchStatusOut)
def cancel_search(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    run = db.execute(
        select(SearchRun)
        .where(SearchRun.profile_id == profile.id, SearchRun.status == "running")
        .order_by(SearchRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="No running search to cancel")

    # Set status directly (rather than waiting for the background task to notice
    # cancel_requested) so the frontend gets instant feedback regardless of
    # whether/when the pipeline actually observes the flag at its next checkpoint.
    run.cancel_requested = True
    run.status = "cancelled"
    run.finished_at = datetime.utcnow()
    run.message = "Search cancelled."
    db.commit()
    db.refresh(run)
    return run


@router.get("/profiles/{profile_id}/roles", response_model=list[RoleOut])
def list_roles(
    status: str | None = None,
    include_provisional: bool = False,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    q = db.query(Role).filter(Role.profile_id == profile.id)
    if not include_provisional:
        # Mid-run "being verified..." placeholder rows (see
        # engine._upsert_provisional_rows). Only the /search page opts in;
        # every other consumer (/my-roles Inbox, stats) must never see them.
        # isnot(True) rather than == False so pre-migration NULLs pass too.
        q = q.filter(Role.provisional.isnot(True))
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        q = q.filter(Role.status.in_(statuses))
    else:
        q = q.filter(Role.status != "deleted")
    return q.order_by(Role.fit_rank.is_(None), Role.fit_rank, Role.id).all()


# ── Role lifecycle actions ──────────────────────────────────────────────────
def _act(db: Session, role: Role, status: str, feedback: str | None):
    role.status = status
    if feedback:
        apply_feedback(db, role.profile_id, role, feedback)
    db.commit()
    db.refresh(role)
    event = _FEEDBACK_EVENT.get(feedback or "")
    if event and role.profile:
        log_event(db, role.profile.user_id, role.profile_id, event, {"role_id": role.id})
    return role


@router.post("/roles/{role_id}/tick", response_model=RoleOut)
def tick_role(role: Role = Depends(get_role_or_404), db: Session = Depends(get_db)):
    return _act(db, role, "saved", "tick")


@router.post("/roles/{role_id}/cross", response_model=RoleOut)
def cross_role(role: Role = Depends(get_role_or_404), db: Session = Depends(get_db)):
    return _act(db, role, "crossed", "cross")


@router.post("/roles/{role_id}/move-to-ignored", response_model=RoleOut)
def ignore_role(role: Role = Depends(get_role_or_404), db: Session = Depends(get_db)):
    return _act(db, role, "ignored", "ignore")


@router.post("/roles/{role_id}/save", response_model=RoleOut)
def save_role(role: Role = Depends(get_role_or_404), db: Session = Depends(get_db)):
    """Re-save a role (e.g. from the Ignored tab). Counts as positive feedback."""
    return _act(db, role, "saved", "tick")


@router.post("/roles/{role_id}/apply", response_model=RoleOut)
def apply_role(role: Role = Depends(get_role_or_404), db: Session = Depends(get_db)):
    role.status = "applied"
    role.applied_at = datetime.utcnow()
    role.application_status = role.application_status or "pending"
    apply_feedback(db, role.profile_id, role, "apply")  # logged; strong positive
    db.commit()
    db.refresh(role)
    if role.profile:
        log_event(db, role.profile.user_id, role.profile_id, "role_apply", {"role_id": role.id})
    return role


@router.patch("/roles/{role_id}/application-status", response_model=RoleOut)
def set_application_status(
    body: ApplicationStatusIn,
    role: Role = Depends(get_role_or_404),
    db: Session = Depends(get_db),
):
    if body.application_status not in _VALID_APP_STATUS:
        raise HTTPException(status_code=422, detail="Invalid application status")
    role.application_status = body.application_status
    # Stamped ONCE, on the first transition away from "pending", and never
    # overwritten -- same invariant as JobSeen.dead_at and for the same reason.
    # The measurement is the interval applied_at -> response_at, and a later
    # correction (a "no response" that turns into an interview three weeks on)
    # must not silently rewrite when the employer first came back.
    if body.application_status != "pending" and role.response_at is None:
        role.response_at = datetime.utcnow()
    db.commit()
    db.refresh(role)
    # The only outcome data this app will ever have. Logged to EventLog rather
    # than inferred later from Role rows because EventLog carries user_id
    # directly, so /admin/analytics can roll it up across every beta user
    # without joining through profiles -- and because a status can be corrected,
    # where the event stream keeps what was reported when.
    if role.profile:
        days = ((role.response_at or datetime.utcnow()) - role.applied_at).days \
            if role.applied_at else None
        log_event(db, role.profile.user_id, role.profile_id, "application_outcome",
                  {"role_id": role.id, "status": body.application_status,
                   "days_since_applied": days})
    return role


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(role: Role = Depends(get_role_or_404), db: Session = Depends(get_db)):
    role.status = "deleted"  # hard-hide, keep the row for dedup history
    db.commit()


@router.post("/profiles/{profile_id}/roles/clear-all")
def clear_all_roles(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    """Bulk escape hatch for stale roles piling up in the Inbox/search view --
    soft-deletes every role that isn't a real decision (saved/applied), same
    "deleted" status the single-role delete uses (restorable from the Deleted
    tab). Deliberately leaves saved/applied untouched: those are decisions,
    not backlog. Skips provisional rows so this can't interfere with an
    in-flight run's placeholder cards."""
    n = (
        db.query(Role)
        .filter(
            Role.profile_id == profile.id,
            Role.status.in_(("new", "crossed")),
            Role.provisional.isnot(True),
        )
        .update({Role.status: "deleted"}, synchronize_session=False)
    )
    db.commit()
    return {"cleared": n}
