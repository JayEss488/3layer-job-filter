"""Onboarding/parsing: CV upload + free text -> attributes, AI suggestions,
and the confidence indicator."""
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_profile_or_404
from ..models import Profile, ProfileAttribute
from ..schemas import AttributeOut, ConfidenceOut, ParseTextIn, SuggestIn, SuggestOut
from ..services.confidence import confidence
from ..services.harvest import harvest_for_profile
from ..services.llm import STRONG_MODEL, llm_json
from ..services.parsing import extract_text_from_upload, parse_text_to_attributes, summarize_cv_text

router = APIRouter(tags=["onboarding"])


def _background_context(db: Session, profile_id: int) -> str:
    """Pull existing past_role/skill/experience/seniority so target-role
    suggestions can be derived from the candidate's background, not just
    whatever free-text context the caller happened to pass in."""
    attrs = db.execute(
        select(ProfileAttribute).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type.in_(
                ["past_role", "skill", "experience", "qualification", "seniority", "sector_target"]
            ),
        )
    ).scalars().all()
    by_type: dict[str, list[str]] = {}
    for a in attrs:
        by_type.setdefault(a.type, []).append(a.value)
    parts = []
    if by_type.get("past_role"):
        parts.append("Past roles: " + ", ".join(by_type["past_role"]))
    if by_type.get("qualification"):
        parts.append("Qualifications: " + "; ".join(by_type["qualification"]))
    if by_type.get("skill"):
        parts.append("Skills: " + ", ".join(by_type["skill"]))
    if by_type.get("experience"):
        parts.append("Experience: " + "; ".join(by_type["experience"]))
    if by_type.get("seniority"):
        parts.append("Seniority: " + ", ".join(by_type["seniority"]))
    if by_type.get("sector_target"):
        parts.append("Sector interests: " + "; ".join(by_type["sector_target"]))
    return "\n".join(parts)


def _target_role_context(db: Session, profile_id: int) -> str:
    """Prefer the raw CV/notes text over the compressed attribute bag: a
    normalised skill/past_role list loses leadership, project, and language
    signal that the original document still has."""
    profile = db.get(Profile, profile_id)
    if profile and profile.cv_text:
        return profile.cv_text[:20000]
    return _background_context(db, profile_id)


_TARGET_ROLE_GUIDANCE = (
    "Weigh the candidate's full picture -- leadership/project experience, applied "
    "use of tools, academic background, languages, communications/organisational "
    "work -- as heavily as any skills list. Do not default to generic titles that "
    "only match a skills-inventory section if stronger, more differentiated, "
    "better-evidenced titles fit their actual background. If the candidate has "
    "stated explicit sector, industry, or cause-targeting language (cover-letter "
    "angles, named industries/organisations they're drawn to), also propose titles "
    "reflecting those sectors specifically -- don't let a generic skills-first "
    "framing crowd those out."
)


def _suggest_target_roles(db: Session, profile_id: int) -> list[str]:
    """Suggest next-step target roles derived from the candidate's background
    (used when target_role is blank)."""
    background = _target_role_context(db, profile_id)
    if not background:
        return []
    data = llm_json(
        f"""Based on this candidate's background, suggest 4-6 short job titles they
could realistically target next in their job search.
{_TARGET_ROLE_GUIDANCE}
{background}
Return ONLY JSON: {{"suggestions": ["...", "..."]}}. Keep each value short (a job title).""",
        model=STRONG_MODEL,
    )
    suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
    return [str(s).strip() for s in suggestions if str(s).strip()][:6]


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
    profile.cv_summary = summarize_cv_text(text)
    created = parse_text_to_attributes(db, profile.id, text, source="cv_parsed")
    created += _preload_target_roles(db, profile.id, created)
    db.commit()
    # Grow the ATS company set for this profile's sectors, off-request. Skips its
    # own SerpAPI spend when the derived keywords are unchanged (see harvest.py).
    background.add_task(harvest_for_profile, profile.id)
    return created


@router.post("/profiles/{profile_id}/parse-text", response_model=list[AttributeOut])
def parse_text(
    body: ParseTextIn,
    background: BackgroundTasks,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    profile.cv_text = body.text[:40000]
    profile.cv_summary = summarize_cv_text(body.text)
    created = parse_text_to_attributes(db, profile.id, body.text, source="text_parsed")
    created += _preload_target_roles(db, profile.id, created)
    db.commit()
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


def _preload_target_roles(
    db: Session, profile_id: int, created: list[ProfileAttribute]
) -> list[ProfileAttribute]:
    """If parsing produced no target_role, generate some from the rest of the
    background and add them as unconfirmed attributes (editable chips)."""
    if any(a.type == "target_role" for a in created):
        return []
    suggestions = _suggest_target_roles(db, profile_id)
    new_attrs = []
    for value in suggestions:
        attr = ProfileAttribute(
            profile_id=profile_id,
            type="target_role",
            value=value,
            source="ai_suggested",
            confirmed=False,
        )
        db.add(attr)
        new_attrs.append(attr)
    db.flush()
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
    if body.type == "target_role":
        background = _target_role_context(db, profile.id)
        data = llm_json(
            f"""Suggest 4-6 short job titles this candidate could realistically target next.
{_TARGET_ROLE_GUIDANCE}
{background or 'No background on file yet.'}
{existing_line}
Suggest DIFFERENT, adjacent roles that broaden their options (e.g. a step up, a sideways
move into a related function, or a specialisation) — not the roles they already listed.
Return ONLY JSON: {{"suggestions": ["...", "..."]}}. Keep each value short (a job title).""",
            model=STRONG_MODEL,
        )
    else:
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
