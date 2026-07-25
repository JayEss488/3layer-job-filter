"""Login/session primitives for the closed beta.

Design (see CLAUDE.md's auth notes): the single chokepoint the whole app already
funnels through is deps.current_user_id(). It used to return a hardcoded constant;
now it returns the authenticated user's id, which we make available to every
sync route/helper via a ContextVar populated by an HTTP middleware
(main.py::_auth_context).

Why a ContextVar and not a plain FastAPI dependency: dozens of call sites call
current_user_id() as an ordinary function (deps.get_profile_or_404,
attributes._get_attr_or_404, the profiles routes, ...), not as an injected
parameter. A middleware sets the ContextVar in the request's async context BEFORE
the route runs; Starlette copies that context into the threadpool it runs sync
routes/dependencies in, so every one of those plain reads sees the right user.
(Only WRITES from a worker thread wouldn't propagate back -- we never write there.)

Passwords: pbkdf2_hmac(sha256) with a per-user salt, stdlib only (no bcrypt dep).
Tokens: itsdangerous signed + timestamped, sent as `Authorization: Bearer <token>`.
"""
import hashlib
import hmac
import secrets
from contextvars import ContextVar, Token

from fastapi import Depends, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import AUTH_SECRET, TOKEN_MAX_AGE_SECONDS

_PBKDF2_ITERATIONS = 240_000
_serializer = URLSafeTimedSerializer(AUTH_SECRET, salt="beta-login-v1")

# Populated per-request by the auth middleware; None when unauthenticated.
_current_user: ContextVar[int | None] = ContextVar("_current_user", default=None)


# ── Password hashing ─────────────────────────────────────────────────────────
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """Return (hex_hash, hex_salt). Generates a fresh salt when none is given."""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return dk.hex(), salt


def verify_password(password: str, salt: str, expected_hash: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return hmac.compare_digest(dk.hex(), expected_hash)


# ── Tokens ───────────────────────────────────────────────────────────────────
def make_token(user_id: int) -> str:
    return _serializer.dumps({"uid": int(user_id)})


def parse_token(token: str) -> int | None:
    """Return the user id encoded in a valid, unexpired token, else None."""
    try:
        data = _serializer.loads(token, max_age=TOKEN_MAX_AGE_SECONDS)
        return int(data["uid"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


# ── Per-request current-user ContextVar ──────────────────────────────────────
def set_current_user(user_id: int | None) -> Token:
    return _current_user.set(user_id)


def reset_current_user(token: Token) -> None:
    _current_user.reset(token)


def current_user_id_or_none() -> int | None:
    return _current_user.get()


def require_current_user_id() -> int:
    """The authenticated user's id, or 401. Used by deps.current_user_id()."""
    uid = _current_user.get()
    if uid is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return uid


def require_authenticated() -> int:
    """Router-level dependency: reject any request without a valid token.

    Applied at include_router() time to every protected router so blanket auth
    holds even for routes that don't otherwise resolve a profile/role (e.g. the
    global settings endpoints). Reads the ContextVar the middleware set."""
    return require_current_user_id()
