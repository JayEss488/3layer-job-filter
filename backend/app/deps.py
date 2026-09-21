"""Shared route dependencies.

Everything scopes to current_user_id(). This app runs as a **single local user**
-- there is no login -- so that function returns a constant. It is kept as a
function, and every table keeps its `user_id` column, because that one function
is the entire auth seam: if multi-user auth is ever wanted again, it is the only
thing that has to start returning something else.
"""
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from .config import LOCAL_USER_ID
from .database import get_db
from .models import Profile, Role


def current_user_id() -> int:
    """The local user's id.

    A plain function, not a FastAPI dependency, because many helpers call it
    directly rather than receiving it as an injected parameter."""
    return LOCAL_USER_ID


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
