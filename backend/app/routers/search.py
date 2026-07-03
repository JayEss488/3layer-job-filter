"""Search kickoff + role lifecycle. The search runs as a FastAPI BackgroundTask;
the frontend polls /search/status. Role actions feed the weight system."""
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import MAX_SEARCHES_PER_DAY
from ..database import get_db
from ..deps import get_profile_or_404, get_role_or_404
from ..models import Profile, ProfileAttribute, Role, SearchRun
from ..schemas import (
    ApplicationStatusIn,
    RoleOut,
    SearchStartOut,
    SearchStatusOut,
)
from ..services.engine import run_search_task
from ..services.feedback import apply_feedback

router = APIRouter(tags=["search"])

_VALID_APP_STATUS = {"pending", "interview", "rejected"}


def _searches_today(db: Session, profile_id: int) -> int:
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(func.count(SearchRun.id))
        .filter(SearchRun.profile_id == profile_id, SearchRun.started_at >= start)
        .scalar()
        or 0
    )


@router.post("/profiles/{profile_id}/search", response_model=SearchStartOut)
def start_search(
    background: BackgroundTasks,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    used = _searches_today(db, profile.id)
    if used >= MAX_SEARCHES_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily search limit reached ({MAX_SEARCHES_PER_DAY}/day). Try again tomorrow.",
        )

    # "Run first search" confirms everything onboarding parsed.
    db.query(ProfileAttribute).filter(
        ProfileAttribute.profile_id == profile.id,
        ProfileAttribute.confirmed.is_(False),
    ).update({ProfileAttribute.confirmed: True}, synchronize_session=False)

    run = SearchRun(profile_id=profile.id, status="running")
    db.add(run)
    db.commit()
    db.refresh(run)

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


@router.get("/profiles/{profile_id}/roles", response_model=list[RoleOut])
def list_roles(
    status: str | None = None,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    q = db.query(Role).filter(Role.profile_id == profile.id)
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
    db.commit()
    db.refresh(role)
    return role


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(role: Role = Depends(get_role_or_404), db: Session = Depends(get_db)):
    role.status = "deleted"  # hard-hide, keep the row for dedup history
    db.commit()
