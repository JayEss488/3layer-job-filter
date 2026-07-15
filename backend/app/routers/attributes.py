"""Profile attributes = the editable memory. Grouped by type for the dashboard."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import ATTRIBUTE_TYPES, DEFAULT_WEIGHT, ENFORCEMENT_CHOICES
from ..database import get_db
from ..deps import current_user_id, get_profile_or_404
from ..models import Profile, ProfileAttribute, RoleFamily
from ..schemas import AttributeCreate, AttributeOut, AttributeUpdate
from ..services.feedback import clear_memory

router = APIRouter(tags=["attributes"])


def _check_enforcement(value: str | None) -> None:
    """"" is allowed and means "clear back to the type's default" -- see
    config.enforcement_for, which is why a null enforcement is meaningful."""
    if value and value not in ENFORCEMENT_CHOICES:
        raise HTTPException(status_code=422, detail=f"Unknown enforcement: {value}")


def _check_family(db: Session, profile_id: int, family_id: int | None) -> int | None:
    """Reject a family belonging to a different profile -- attaching a target
    role to someone else's family would silently move it out of this profile's
    clusters and into theirs."""
    if family_id is None:
        return None
    family = db.get(RoleFamily, family_id)
    if not family or family.profile_id != profile_id:
        raise HTTPException(status_code=404, detail="Role family not found")
    return family_id


def _get_attr_or_404(db: Session, attr_id: int) -> ProfileAttribute:
    attr = db.get(ProfileAttribute, attr_id)
    if not attr:
        raise HTTPException(status_code=404, detail="Attribute not found")
    profile = db.get(Profile, attr.profile_id)
    if not profile or profile.user_id != current_user_id():
        raise HTTPException(status_code=404, detail="Attribute not found")
    return attr


@router.get("/profiles/{profile_id}/attributes")
def list_attributes(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    rows = db.execute(
        select(ProfileAttribute)
        .where(ProfileAttribute.profile_id == profile.id)
        .order_by(ProfileAttribute.id)
    ).scalars().all()

    by_type: dict[str, list] = {t: [] for t in ATTRIBUTE_TYPES}
    for a in rows:
        by_type.setdefault(a.type, []).append(AttributeOut.model_validate(a))
    return {
        "by_type": by_type,
        "items": [AttributeOut.model_validate(a) for a in rows],
    }


@router.post("/profiles/{profile_id}/attributes", response_model=AttributeOut, status_code=201)
def add_attribute(
    body: AttributeCreate,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    if body.type not in ATTRIBUTE_TYPES:
        raise HTTPException(status_code=422, detail=f"Unknown attribute type: {body.type}")
    value = body.value.strip()
    if not value:
        raise HTTPException(status_code=422, detail="Value cannot be empty")
    _check_enforcement(body.enforcement)

    attr = ProfileAttribute(
        profile_id=profile.id,
        type=body.type,
        value=value,
        source=body.source,
        confirmed=body.confirmed,
        weight=body.weight if body.weight is not None else DEFAULT_WEIGHT,
        proficiency=body.proficiency,
        evidence_origin=body.evidence_origin,
        family_id=_check_family(db, profile.id, body.family_id),
        pinned=bool(body.pinned),
        enforcement=body.enforcement,
    )
    db.add(attr)
    db.commit()
    db.refresh(attr)
    return attr


@router.patch("/attributes/{attr_id}", response_model=AttributeOut)
def update_attribute(attr_id: int, body: AttributeUpdate, db: Session = Depends(get_db)):
    attr = _get_attr_or_404(db, attr_id)
    if body.value is not None:
        attr.value = body.value.strip()
    if body.confirmed is not None:
        attr.confirmed = body.confirmed
    if body.weight is not None:
        attr.weight = body.weight
    if body.proficiency is not None:
        attr.proficiency = body.proficiency or None  # "" clears it back to unset
    if body.evidence_origin is not None:
        attr.evidence_origin = body.evidence_origin or None  # "" clears it back to unset
    if body.family_id is not None:
        attr.family_id = _check_family(db, attr.profile_id, body.family_id)
    if body.pinned is not None:
        attr.pinned = body.pinned
    if body.enforcement is not None:
        _check_enforcement(body.enforcement)
        attr.enforcement = body.enforcement or None  # "" clears it back to the type default
    db.commit()
    db.refresh(attr)
    return attr


@router.delete("/attributes/{attr_id}", status_code=204)
def delete_attribute(attr_id: int, db: Session = Depends(get_db)):
    attr = _get_attr_or_404(db, attr_id)
    db.delete(attr)
    db.commit()


@router.post("/profiles/{profile_id}/clear-memory", status_code=204)
def clear_profile_memory(
    profile: Profile = Depends(get_profile_or_404),
    wipe_log: bool = False,
    db: Session = Depends(get_db),
):
    """Reset all weights to default (safer than deleting attributes)."""
    clear_memory(db, profile.id, wipe_log=wipe_log)
    db.commit()
