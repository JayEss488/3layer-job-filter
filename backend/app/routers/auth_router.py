"""Login for the closed beta. Credentials are hand-assigned (see
scripts/gen_beta_users.py); there is no self-service signup or password reset --
those are deliberately out of scope for the beta."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import current_user_id
from ..models import User
from ..schemas import LoginIn, LoginOut, MeOut
from ..services.analytics import log_event
from ..services.auth import make_token, verify_password

router = APIRouter(tags=["auth"])


@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    """Public: exchange username+password for a signed Bearer token.

    Deliberately vague on failure (same message for unknown user and wrong
    password) so the endpoint can't be used to enumerate valid usernames."""
    username = (body.username or "").strip()
    user = db.execute(
        select(User).where(User.username == username)
    ).scalar_one_or_none()
    if not user or not verify_password(body.password or "", user.salt, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    log_event(db, user.id, None, "login")
    return LoginOut(token=make_token(user.id), user_id=user.id, username=user.username)


@router.get("/me", response_model=MeOut)
def me(db: Session = Depends(get_db)):
    """Who the caller is. Self-guards: current_user_id() raises 401 when the
    request carried no valid token, so this needs no router-level auth dep."""
    uid = current_user_id()
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return MeOut(user_id=user.id, username=user.username)
