"""Role families: the user-editable clusters the search pipeline runs per-stream.

Thin, like the other routers -- the seeding/ordering logic lives in
services/families.py."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import FAMILY_TIER_CHOICES, FAMILY_TIER_DEFAULT, MAX_USER_ROLE_FAMILIES
from ..database import get_db
from ..deps import current_user_id, get_profile_or_404
from ..models import Profile, ProfileAttribute, RoleFamily
from ..schemas import AttributeOut, FamilyCreate, FamilyOut, FamilyUpdate
from ..services.families import ensure_families, list_families, regenerate_family

router = APIRouter(tags=["families"])


def _get_family_or_404(db: Session, family_id: int) -> RoleFamily:
    family = db.get(RoleFamily, family_id)
    if not family:
        raise HTTPException(status_code=404, detail="Role family not found")
    profile = db.get(Profile, family.profile_id)
    if not profile or profile.user_id != current_user_id():
        raise HTTPException(status_code=404, detail="Role family not found")
    return family


@router.get("/profiles/{profile_id}/families", response_model=list[FamilyOut])
def get_families(
    profile: Profile = Depends(get_profile_or_404), db: Session = Depends(get_db)
):
    """Seeds families from any still-ungrouped target roles before returning
    (see services/families.ensure_families). That's a write on a GET, which is
    deliberate and bounded: it's idempotent, it's the lazy migration path for
    profiles that predate the table and for roles a fresh CV parse just added,
    and after the first call it does no work at all. The alternative -- an
    explicit seed endpoint -- just moves the same write behind a call the client
    would have to make first anyway."""
    return ensure_families(db, profile.id)


@router.post("/profiles/{profile_id}/families", response_model=FamilyOut, status_code=201)
def add_family(
    body: FamilyCreate,
    profile: Profile = Depends(get_profile_or_404),
    db: Session = Depends(get_db),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Family name cannot be empty")
    if body.tier and body.tier not in FAMILY_TIER_CHOICES:
        raise HTTPException(status_code=422, detail=f"Unknown tier: {body.tier}")
    existing = list_families(db, profile.id)
    if len(existing) >= MAX_USER_ROLE_FAMILIES:
        raise HTTPException(
            status_code=422,
            detail=f"You can have at most {MAX_USER_ROLE_FAMILIES} role families",
        )
    family = RoleFamily(
        profile_id=profile.id,
        name=name,
        tier=body.tier or FAMILY_TIER_DEFAULT,
        position=max((f.position for f in existing), default=-1) + 1,
    )
    db.add(family)
    db.commit()
    db.refresh(family)
    return family


@router.patch("/families/{family_id}", response_model=FamilyOut)
def update_family(family_id: int, body: FamilyUpdate, db: Session = Depends(get_db)):
    family = _get_family_or_404(db, family_id)
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Family name cannot be empty")
        family.name = name
    if body.tier is not None:
        if body.tier not in FAMILY_TIER_CHOICES:
            raise HTTPException(status_code=422, detail=f"Unknown tier: {body.tier}")
        family.tier = body.tier
    if body.position is not None:
        family.position = body.position
    db.commit()
    db.refresh(family)
    return family


@router.post("/families/{family_id}/regenerate", response_model=list[AttributeOut])
def regenerate_family_roles(family_id: int, db: Session = Depends(get_db)):
    """Manual "regenerate" for ONE family: refreshes its un-pinned target roles
    from its title + the candidate's background (see
    services/families.regenerate_family). Pinned roles in this family, and every
    other family, are left untouched."""
    family = _get_family_or_404(db, family_id)
    return regenerate_family(db, family)


@router.delete("/families/{family_id}", status_code=204)
def delete_family(family_id: int, db: Session = Depends(get_db)):
    """Removes the family AND its target roles.

    Orphaning them instead (family_id -> NULL) reads as the safer option but
    isn't: the card is the only place a target role is rendered, so an orphan
    would be invisible while still driving discovery, and the next
    ensure_families call would re-seed it into a brand-new card -- i.e. deleting
    a card would make its roles silently come back. The card's ✕ means "stop
    searching for this stream"; each role keeps its own ✕ for narrower edits."""
    family = _get_family_or_404(db, family_id)
    members = db.execute(
        select(ProfileAttribute).where(ProfileAttribute.family_id == family.id)
    ).scalars().all()
    for attr in members:
        db.delete(attr)
    db.delete(family)
    db.commit()
