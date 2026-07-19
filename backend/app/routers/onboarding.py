"""Onboarding/parsing: CV upload + free text -> attributes, AI suggestions,
and the confidence indicator."""
import asyncio

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_profile_or_404
from ..models import Profile
from ..schemas import (
    AttributeOut,
    ConfidenceOut,
    ContextHeaderOut,
    ParseTextIn,
    SuggestIn,
    SuggestOut,
)
from ..services.confidence import confidence
from ..services.harvest import harvest_for_profile
from ..services.llm import llm_json
from ..services.parsing import (
    CVParseFailed,
    extract_text_from_upload,
    parse_text_to_attributes,
    summarize_cv_text,
)
from ..services.profile_intel import ensure_profile_intel, read_cached_intel

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
        requirements=intel.get("requirements") or [],
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
    profile.cv_text = text[:40000]
    # summarize_cv_text and parse_text_to_attributes each only need the raw text,
    # not each other's output, so run their (both STRONG_MODEL) LLM calls
    # concurrently instead of back-to-back -- cuts one of the three sequential
    # calls off the upload wait without changing either prompt/model.
    try:
        summary, created = await asyncio.gather(
            asyncio.to_thread(summarize_cv_text, text),
            asyncio.to_thread(parse_text_to_attributes, db, profile.id, text, source="cv_parsed"),
        )
    except CVParseFailed as e:
        raise HTTPException(status_code=502, detail=f"Couldn't parse that CV automatically: {e}") from e
    profile.cv_summary = summary
    db.commit()
    created += ensure_profile_intel(db, profile.id)
    # Grow the ATS company set for this profile's sectors, off-request. Skips its
    # own SerpAPI spend when the derived keywords are unchanged (see harvest.py).
    background.add_task(harvest_for_profile, profile.id)
    return created


@router.post("/profiles/{profile_id}/parse-text", response_model=list[AttributeOut])
async def parse_text(
    body: ParseTextIn,
    background: BackgroundTasks,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    profile.cv_text = body.text[:40000]
    try:
        summary, created = await asyncio.gather(
            asyncio.to_thread(summarize_cv_text, body.text),
            asyncio.to_thread(parse_text_to_attributes, db, profile.id, body.text, source="text_parsed"),
        )
    except CVParseFailed as e:
        raise HTTPException(status_code=502, detail=f"Couldn't parse that text automatically: {e}") from e
    profile.cv_summary = summary
    db.commit()
    created += ensure_profile_intel(db, profile.id)
    background.add_task(harvest_for_profile, profile.id)
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
    header, the cheap-gate requirements checklist, and (only if empty) a draft
    intent_text, all together."""
    return ensure_profile_intel(db, profile.id, force=True)


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


@router.get("/profiles/{profile_id}/confidence", response_model=ConfidenceOut)
def get_confidence(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    return confidence(db, profile.id)
