"""In-product feedback prompts (the days 1-6 surface).

Two questions, both Y/N with a conditional "what went wrong" box:

* **results_quality** -- "Are results what they should be?", fired by the first
  cross or apply on a run.
* **setup_ok** -- "Did setup work how it should?", fired as soon as CV parsing
  finishes.

Answers land in the same `feedback_responses` store as the sign-up and wrap-up
surveys, so GET /admin/analytics reads all three from one place.

**Why the trigger rules live here rather than in the client.** The client knows
when a cross happened and which run is on screen, but it cannot know whether
this user already answered -- localStorage is per-device, so a user who answered
on their laptop would be asked again on their phone, and a prompt that reappears
after you have answered it reads as the app losing your input. GET /feedback/due
answers that from the store itself.

This router is authed but NOT beta-gated (see main.py): an answer the user has
already typed must never be lost to a window that lapsed between the prompt
appearing and the button being pressed.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user_id, get_profile_or_404
from ..models import FeedbackResponse, Profile, SearchRun
from ..schemas import FeedbackDueOut, FeedbackIn, FeedbackPromptOut
from ..services.analytics import log_event, record_feedback

router = APIRouter(tags=["feedback"])

IN_RUN_SURFACE = "in_run"

# The questions this router will accept. An unknown question_id is rejected
# rather than stored: the whole value of the store is that a question_id means
# one thing forever, and a typo'd slug silently creating a new "question" is how
# that stops being true.
QUESTION_IDS = {
    "results_quality",
    "results_quality_detail",
    "setup_ok",
    "setup_ok_detail",
}

# The results prompt is held back until the user's SECOND completed run, so the
# very first search -- the one that decides whether they stay at all -- is not
# interrupted by a survey. Set to 1 to prompt from the first run instead.
RESULTS_PROMPT_MIN_RUNS = 2


def _answered_question_ids(db: Session, user_id: int, run_id: int | None = None) -> set[str]:
    """Which in-run questions this user has already answered (optionally for one run)."""
    q = select(FeedbackResponse.question_id).where(
        FeedbackResponse.user_id == user_id,
        FeedbackResponse.surface == IN_RUN_SURFACE,
    )
    if run_id is not None:
        q = q.where(FeedbackResponse.run_id == run_id)
    return {r for r in db.execute(q).scalars().all()}


@router.get("/feedback/due", response_model=FeedbackDueOut)
def feedback_due(
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    """Which prompts this profile should currently show.

    `profile_id` is a query param resolved through get_profile_or_404, so it is
    ownership-checked like every other profile-scoped route."""
    uid = current_user_id()

    # Completed runs for this profile, newest first. Only "done" counts: a
    # cancelled or errored run produced nothing to have an opinion about.
    runs = db.execute(
        select(SearchRun.id)
        .where(SearchRun.profile_id == profile.id, SearchRun.status == "done")
        .order_by(SearchRun.id.desc())
    ).scalars().all()

    results = FeedbackPromptOut()
    if len(runs) >= RESULTS_PROMPT_MIN_RUNS:
        latest = runs[0]
        # Scoped to THIS run: the question is about the results of the run in
        # front of them, so it is legitimately worth asking again on a later
        # run even though they answered on an earlier one.
        if "results_quality" not in _answered_question_ids(db, uid, run_id=latest):
            results = FeedbackPromptOut(due=True, run_id=latest)

    # Setup is a one-off: it asks about onboarding, which happens once. Scoped
    # to the user across every run and profile rather than per-run.
    setup = FeedbackPromptOut(due="setup_ok" not in _answered_question_ids(db, uid))

    return FeedbackDueOut(results_quality=results, setup_ok=setup)


@router.post("/feedback", status_code=204)
def submit_feedback(body: FeedbackIn, db: Session = Depends(get_db)):
    """Record one prompt answer.

    Returns 204: the client already knows what it sent, and the prompt collapses
    on success rather than rendering anything from the response.

    Unlike log_event, a failure here is NOT swallowed -- the user pressed a
    button expecting their answer to be saved, and a silent drop would show them
    a thank-you for something that did not happen."""
    uid = current_user_id()
    question_id = (body.question_id or "").strip()
    if question_id not in QUESTION_IDS:
        raise HTTPException(
            status_code=422,
            detail=f"question_id must be one of: {', '.join(sorted(QUESTION_IDS))}",
        )

    # Ownership check when a profile is named. The prompts always send one, but
    # it stays optional in the schema because the answer is worth keeping even
    # if the client cannot say which profile it came from.
    profile_id = body.profile_id
    if profile_id is not None:
        owner = db.execute(
            select(Profile.user_id).where(Profile.id == profile_id)
        ).scalar_one_or_none()
        if owner != uid:
            raise HTTPException(status_code=404, detail="Profile not found")

    row = record_feedback(
        db,
        uid,
        IN_RUN_SURFACE,
        question_id,
        body.answer,
        profile_id=profile_id,
        run_id=body.run_id,
    )
    if row is None:
        # A blank answer -- nothing to store. Not an error: the "what failed?"
        # box is optional even after answering No.
        return
    db.commit()
    log_event(db, uid, profile_id, "feedback_response", {"question_id": question_id})
