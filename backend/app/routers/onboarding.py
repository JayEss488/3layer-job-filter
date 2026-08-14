"""Onboarding/parsing: CV upload + free text -> attributes and AI suggestions."""
import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..config import CV_PARSE_TIMEOUT_SECONDS
from ..database import get_db
from ..deps import get_profile_or_404
from ..models import Profile
from ..schemas import (
    AttributeOut,
    ContextHeaderOut,
    ContextHeaderUpdate,
    ParseTextIn,
    SuggestIn,
    SuggestOut,
)
from ..services import formation
from ..services.families import ensure_families, top_up_new_families_bg
from ..services.harvest import harvest_for_profile
from ..services.llm import llm_json
from ..services.parsing import CVParseFailed, extract_text_from_upload
from ..services.profile_intel import (
    candidate_requirements_display,
    ensure_profile_intel,
    read_cached_intel,
    set_header_locked,
)

router = APIRouter(tags=["onboarding"])


@router.get("/profiles/{profile_id}/context-header", response_model=ContextHeaderOut)
def get_context_header(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    """The distilled "who this candidate is" header the final judge is given,
    plus the CV summary underneath it, read-only.

    Surfaced so the Memory tab can show what the AI actually reads about the
    candidate rather than leaving it a black box -- it's generated from the
    memory on that page, so seeing it is how you tell whether an edit landed.
    Pure cache read (see profile_intel.read_cached_intel): empty until
    profile_intel has run at least once, e.g. before the first search."""
    intel = read_cached_intel(db, profile.id)
    return ContextHeaderOut(
        header=intel.get("header") or "",
        requirements=candidate_requirements_display(db, profile.id),
        cv_summary=profile.cv_summary or "",
    )


@router.patch("/profiles/{profile_id}/context-header", response_model=ContextHeaderOut)
def update_context_header(
    body: ContextHeaderUpdate,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    """Manual edit of the AI-generated header from the Memory page. Locks it
    (see profile_intel.set_header_locked) so the next search or profile edit
    doesn't silently regenerate over it -- cv_summary is edited via the plain
    PATCH /profiles/{id} (ProfileUpdate.cv_summary) instead, since that field
    is never touched by any regenerate path."""
    set_header_locked(db, profile.id, body.header)
    intel = read_cached_intel(db, profile.id)
    return ContextHeaderOut(
        header=intel.get("header") or "",
        requirements=candidate_requirements_display(db, profile.id),
        cv_summary=profile.cv_summary or "",
    )


@router.post("/profiles/{profile_id}/parse-cv", response_model=list[AttributeOut])
async def parse_cv(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    raw = await file.read()
    text = extract_text_from_upload(file.filename or "", raw)
    if not text.strip():
        raise HTTPException(status_code=422, detail="Could not read any text from that file")
    return await _run_formation(background, profile, db, text, "cv_parsed", "CV")


@router.post("/profiles/{profile_id}/parse-text", response_model=list[AttributeOut])
async def parse_text(
    body: ParseTextIn,
    background: BackgroundTasks,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    if not body.text.strip():
        raise HTTPException(status_code=422, detail="No text to parse")
    return await _run_formation(background, profile, db, body.text, "text_parsed", "text")


async def _run_formation(
    background: BackgroundTasks,
    profile: Profile,
    db: Session,
    text: str,
    source: str,
    noun: str,
):
    """Shared CV-upload / text-paste body: run the two formation LLM calls in
    parallel (services/formation.py), then persist everything -- structured
    attributes, cv_summary, the seeded role families (+ their target roles), a
    drafted intent, and the profile-intel cache -- in one commit. Families are
    seeded here (before the response returns) rather than lazily on a later GET
    /families, so the attribute rows the frontend refetches already carry their
    family_id and the cards render with their role chips immediately.

    intent_text is read (and cv_text staged) before the LLM calls; the calls are
    DB-free, so nothing flushes until persist_formation runs its single commit."""
    intent_text = profile.intent_text
    profile.cv_text = text[:40000]
    try:
        extract_data, understand_data = await asyncio.wait_for(
            formation.run_formation_calls(text, intent_text),
            timeout=CV_PARSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        # An honest, visible failure beats a spinner that never resolves. The
        # abandoned threads are DB-free (formation's module docstring), so they
        # touch nothing on their way out; nothing has been committed at this
        # point either, so the profile is exactly as it was. Retrying is the
        # right advice and empirically works -- see CV_PARSE_TIMEOUT_SECONDS.
        raise HTTPException(
            status_code=504,
            detail=(f"Reading that {noun} took longer than "
                    f"{CV_PARSE_TIMEOUT_SECONDS} seconds and was stopped. "
                    "Nothing was changed — please try again."),
        ) from None
    try:
        created = await asyncio.to_thread(
            formation.persist_formation, db, profile.id, text, source, extract_data, understand_data
        )
    except CVParseFailed as e:
        raise HTTPException(
            status_code=502, detail=f"Couldn't parse that {noun} automatically: {e}"
        ) from e
    # Grow the ATS company set for this profile's sectors, off-request. Skips its
    # own SerpAPI spend when the derived keywords are unchanged (see harvest.py).
    background.add_task(harvest_for_profile, profile.id)
    # Top up each seeded family's title reserve pool toward
    # FAMILY_TITLE_RESERVE_TARGET, off-request -- see
    # families.top_up_new_families_bg for why this must not run inline here.
    background.add_task(top_up_new_families_bg, profile.id)
    return created


@router.post("/profiles/{profile_id}/harvest-ats")
def harvest_ats(
    background: BackgroundTasks,
    force: bool = False,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    """Manually (re-)harvest ATS boards for this profile's sectors. Runs
    off-request; `force=true` re-harvests even if the keywords are unchanged."""
    background.add_task(harvest_for_profile, profile.id, force)
    return {"status": "scheduled", "profile_id": profile.id, "force": force}


@router.post("/profiles/{profile_id}/regenerate-target-roles", response_model=list[AttributeOut])
def regenerate_target_roles(
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    """Manual "regenerate now" override -- forces profile_intel to re-run even if
    its cached signature is unchanged. Refreshes target roles, the final-judge
    header, and (only if empty) a draft intent_text, all together.

    The fresh target_role rows profile_intel creates start with no family_id
    (it has no notion of family boundaries) -- ensure_families must run
    synchronously right here, same as parse_cv/parse_text do, so the roles are
    correctly (re-)grouped into the profile's EXISTING family/families before
    the response returns, rather than sitting ungrouped until some later GET
    happens to trigger it (which used to risk a stale/ungrouped read from a
    search kicked off in between, and could reseed a surprise extra family)."""
    new_attrs = ensure_profile_intel(db, profile.id, force=True)
    ensure_families(db, profile.id)
    return new_attrs


@router.post("/profiles/{profile_id}/suggest", response_model=SuggestOut)
def suggest(
    body: SuggestIn,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    """AI suggestion chips for a given attribute type (e.g. related target roles).

    body.context carries the values already on the profile for this field. We feed
    those to the model as an explicit exclusion list so the suggestions are genuinely
    NEW, adjacent options rather than a re-run of what the user already added."""
    existing = [v.strip() for v in (body.context or "").split(",") if v.strip()]
    existing_line = (
        "The candidate ALREADY has these on their profile — do NOT repeat any of them, "
        "and do NOT suggest trivial rewordings of them:\n" + "\n".join(f"- {v}" for v in existing)
        if existing else "Nothing on this field yet."
    )
    # target_role is no longer a suggest()-able type -- it's generated (and
    # regenerated) entirely by profile_intel.ensure_profile_intel now.
    data = llm_json(
        f"""Suggest 4-6 short NEW values for the '{body.type}' field of a job-search profile.
{existing_line}
Suggest complementary, adjacent options that add breadth — not repeats or rewordings of
what is already there.
Return ONLY JSON: {{"suggestions": ["...", "..."]}}. Keep each value short (a title or skill)."""
    )
    suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
    # Belt-and-braces: drop any that still collide with an existing value (the
    # frontend also de-dupes, but this keeps repeats out of the API response).
    existing_lower = {v.lower() for v in existing}
    cleaned = [
        s for s in (str(x).strip() for x in suggestions)
        if s and s.lower() not in existing_lower
    ]
    return SuggestOut(suggestions=cleaned[:6])
