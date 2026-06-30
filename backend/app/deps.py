"""Shared route dependencies. Today everything is scoped to CURRENT_USER_ID; when
auth arrives, current_user_id() becomes the only thing that changes."""
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from .config import CURRENT_USER_ID
from .database import get_db
from .models import Profile, Role


def current_user_id() -> int:
    return CURRENT_USER_ID


def get_profile_or_404(profile_id: int, db: Session = Depends(get_db)) -> Profile:
    profile = db.get(Profile, profile_id)
    if not profile or profile.user_id != current_user_id():
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


def get_role_or_404(role_id: int, db: Session = Depends(get_db)) -> Role:
    role = db.get(Role, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    # ensure the role belongs to a profile owned by the current user
    if not role.profile or role.profile.user_id != current_user_id():
        raise HTTPException(status_code=404, detail="Role not found")
    return role
