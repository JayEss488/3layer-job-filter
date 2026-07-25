"""Profiles = the dashboard tabs. Lists auto-create a default profile so the app
is never empty on first load."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user_id, get_profile_or_404
from ..models import Profile, Role
from ..schemas import AttributeOut, ProfileCreate, ProfileOut, ProfileUpdate, StatsOut
from ..services.analytics import log_event
from ..services.feedback_intel import interpret_search_feedback

router = APIRouter(prefix="/profiles", tags=["profiles"])


def _ensure_default(db: Session) -> None:
    uid = current_user_id()
    exists = db.execute(
        select(Profile.id).where(Profile.user_id == uid)
    ).first()
    if not exists:
        profile = Profile(user_id=uid, name="Profile 1", is_active=True)
        db.add(profile)
        db.commit()
        db.refresh(profile)
        # First profile for this user == effectively their first session.
        log_event(db, uid, profile.id, "profile_created", {"auto": True})


@router.get("", response_model=list[ProfileOut])
def list_profiles(db: Session = Depends(get_db)):
    _ensure_default(db)
    return db.execute(
        select(Profile)
        .where(Profile.user_id == current_user_id())
        .order_by(Profile.id)
    ).scalars().all()


@router.post("", response_model=ProfileOut, status_code=201)
def create_profile(body: ProfileCreate, db: Session = Depends(get_db)):
    count = len(
        db.execute(
            select(Profile.id).where(Profile.user_id == current_user_id())
        ).all()
    )
    uid = current_user_id()
    name = (body.name or f"Profile {count + 1}").strip()
    profile = Profile(user_id=uid, name=name, is_active=True)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    log_event(db, uid, profile.id, "profile_created", {"auto": False})
    return profile


@router.patch("/{profile_id}", response_model=ProfileOut)
def update_profile(
    body: ProfileUpdate,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    if body.name is not None:
        profile.name = body.name.strip()
    if body.is_active is not None:
        profile.is_active = body.is_active
    if body.intent_text is not None:
        # Free-text "what I'm looking for" -- stored durable judge context and the
        # input to target-role regeneration. Empty string clears it back to None.
        profile.intent_text = body.intent_text.strip() or None
    if body.search_feedback is not None:
        # Free-text feedback on recent search results -- fed to the final judge on
        # the next run (see snapshot.build_snapshot). Empty string clears it.
        profile.search_feedback = body.search_feedback.strip() or None
    if body.cv_summary is not None:
        # Manual edit of the evidence brief the gates/judge read (Memory page's
        # "What the AI reads about you" panel) -- a plain column never touched by
        # any regenerate path, so no staleness/lock concern here (contrast with
        # the header PATCH below). Empty string clears it.
        profile.cv_summary = body.cv_summary.strip() or None
    db.commit()
    db.refresh(profile)
    return profile


@router.post("/{profile_id}/interpret-feedback", response_model=list[AttributeOut])
def interpret_feedback(
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    """Reads the profile's current search_feedback and, if it states an
    actionable rule, creates new unconfirmed avoid/must_have attributes for
    it -- see services/feedback_intel.py. Called by the frontend right after
    a successful PATCH of search_feedback, kept as its own endpoint so the
    plain field-save above stays fast for its other callers (rename,
    intent_text)."""
    return interpret_search_feedback(db, profile.id)


@router.delete("/{profile_id}", status_code=204)
def delete_profile(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    remaining = db.execute(
        select(Profile.id).where(Profile.user_id == current_user_id())
    ).all()
    if len(remaining) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete your only profile")
    db.delete(profile)
    db.commit()


@router.get("/{profile_id}/stats", response_model=StatsOut)
def profile_stats(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    def count(*statuses: str) -> int:
        return (
            db.query(Role)
            .filter(Role.profile_id == profile.id, Role.status.in_(statuses))
            .count()
        )

    searched = (
        db.query(Role)
        .filter(Role.profile_id == profile.id, Role.status != "deleted")
        .count()
    )
    return StatsOut(searched=searched, saved=count("saved"), applied=count("applied"))
