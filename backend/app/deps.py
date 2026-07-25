"""Shared route dependencies. Everything scopes to current_user_id() -- which now
returns the authenticated user (see services/auth.py) instead of a hardcoded
constant. That one function is the entire auth seam: every ownership check below
and in the routers goes through it."""
from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

from .database import get_db
from .models import Profile, Role
from .services.auth import require_current_user_id


def current_user_id() -> int:
    """The authenticated user's id (401 if the request carried no valid token).

    A plain function, not a FastAPI dependency, because many helpers call it
    directly. The value is populated per-request by the auth middleware into a
    ContextVar -- see services/auth.py for why."""
    return require_current_user_id()


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
