"""Onboarding/parsing: CV upload + free text -> attributes, AI suggestions,
and the confidence indicator."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_profile_or_404
from ..models import Profile
from ..schemas import AttributeOut, ConfidenceOut, ParseTextIn, SuggestIn, SuggestOut
from ..services.confidence import confidence
from ..services.llm import llm_json
from ..services.parsing import extract_text_from_upload, parse_text_to_attributes

router = APIRouter(tags=["onboarding"])


@router.post("/profiles/{profile_id}/parse-cv", response_model=list[AttributeOut])
async def parse_cv(
    file: UploadFile = File(...),
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    raw = await file.read()
    text = extract_text_from_upload(file.filename or "", raw)
    if not text.strip():
        raise HTTPException(status_code=422, detail="Could not read any text from that file")
    created = parse_text_to_attributes(db, profile.id, text, source="cv_parsed")
    db.commit()
    return created


@router.post("/profiles/{profile_id}/parse-text", response_model=list[AttributeOut])
def parse_text(
    body: ParseTextIn,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    created = parse_text_to_attributes(db, profile.id, body.text, source="text_parsed")
    db.commit()
    return created


@router.post("/profiles/{profile_id}/suggest", response_model=SuggestOut)
def suggest(
    body: SuggestIn,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    """AI suggestion chips for a given attribute type (e.g. related target roles)."""
    data = llm_json(
        f"""Suggest 4-6 short values for the '{body.type}' field of a job-search profile.
Context: {body.context or 'none'}
Return ONLY JSON: {{"suggestions": ["...", "..."]}}. Keep each value short (a title or skill)."""
    )
    suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
    return SuggestOut(suggestions=[str(s).strip() for s in suggestions if str(s).strip()][:6])


@router.get("/profiles/{profile_id}/confidence", response_model=ConfidenceOut)
def get_confidence(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    return confidence(db, profile.id)
