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

THREE self-serve sign-in paths now live here, and they verify very differently:

* **Google** -- delegated to Google's `tokeninfo` endpoint (one outbound call).
* **Apple** -- verified LOCALLY against Apple's JWKS, because Apple publishes no
  equivalent endpoint. PyJWT does the RS256/exp/aud/iss work.
* **Email + password** -- no third party at all; the pbkdf2 primitives above.

The one thing all three share, and the thing to check first in any review here:
a token is only ever accepted when its audience is OUR client id. Both provider
paths would otherwise accept a perfectly valid token minted for someone else's
application, which is an authentication bypass rather than a validation nit.
"""
import hashlib
import hmac
import re
import secrets
import threading
from contextvars import ContextVar, Token

import httpx
from fastapi import Depends, HTTPException
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from ..config import (
    APPLE_CLIENT_ID,
    AUTH_SECRET,
    GOOGLE_CLIENT_ID,
    PASSWORD_MIN_LENGTH,
    TOKEN_MAX_AGE_SECONDS,
)

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
    """Constant-time check of a plaintext password against a stored hash.

    Returns False for a Google account, whose `password_hash` is the empty
    string: a pbkdf2 hex digest is always 64 characters, so it can never equal
    "". That is the sole thing standing between a Google account and POST
    /login, so the early return below is defensive rather than decorative --
    it also protects against a malformed/empty salt raising out of
    bytes.fromhex() and being caught as a 500 somewhere upstream."""
    if not expected_hash or not salt:
        return False
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    except ValueError:
        return False
    return hmac.compare_digest(dk.hex(), expected_hash)


def unusable_password() -> tuple[str, str]:
    """The (hash, salt) pair stored for an account that has no password.

    An empty hash with a real random salt. See verify_password above for why the
    empty hash is what makes the account unreachable through /login, and the
    User model docstring for why this is preferred over NULL."""
    return "", secrets.token_hex(16)


# ── Google sign-in ───────────────────────────────────────────────────────────
# Google's own OpenID issuers. Both spellings are valid and Google emits either.
_GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


class GoogleAuthError(Exception):
    """The credential did not verify. The message is safe to show a user."""


def verify_google_id_token(credential: str) -> dict:
    """Verify a Google Identity Services ID token and return its claims.

    Returns a dict with at least `sub`, `email`, `name`.

    Verification is delegated to Google's own `tokeninfo` endpoint rather than
    done locally against their JWKS. That is one outbound HTTPS call per
    sign-in, which is the wrong trade for a hot path and the right one here:
    sign-in happens once per user per month (TOKEN_MAX_AGE_SECONDS), and local
    verification means owning JWKS fetching, key-rotation caching and RS256
    validation -- three places to get subtly wrong, in the one part of the app
    where a subtle mistake is an authentication bypass rather than a bad job
    match. tokeninfo checks the signature, the issuer and the expiry for us.

    What tokeninfo does NOT check, and this function therefore must:

    * **`aud`**. tokeninfo will happily validate a token Google minted for a
      completely different application. Comparing it to our own client id is the
      only thing that stops an attacker presenting a valid token obtained from
      any other Google-integrated site and being logged in as that user here.
      Compared with hmac.compare_digest out of habit rather than necessity.
    * **`email_verified`**. An unverified email must not be stored as if it were
      confirmed -- it would be shown in the admin signup list as a contact
      address for someone who never proved they own it.

    `sub` is returned as the caller's join key; see the User model for why email
    is never used for that."""
    if not GOOGLE_CLIENT_ID:
        raise GoogleAuthError("Google sign-in is not configured on this server.")
    credential = (credential or "").strip()
    if not credential:
        raise GoogleAuthError("Missing Google credential.")

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(_TOKENINFO_URL, params={"id_token": credential})
    except httpx.HTTPError:
        # Network/DNS/timeout. Deliberately distinct wording from a rejected
        # token so a user retrying a transient failure isn't told their account
        # is invalid.
        raise GoogleAuthError("Could not reach Google to verify your sign-in. Please try again.")

    if resp.status_code != 200:
        raise GoogleAuthError("Google rejected that sign-in. Please try again.")

    try:
        claims = resp.json()
    except ValueError:
        raise GoogleAuthError("Google returned an unreadable response. Please try again.")

    aud = str(claims.get("aud") or "")
    if not hmac.compare_digest(aud, GOOGLE_CLIENT_ID):
        raise GoogleAuthError("That sign-in was issued for a different application.")

    if str(claims.get("iss") or "") not in _GOOGLE_ISSUERS:
        raise GoogleAuthError("That sign-in was not issued by Google.")

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise GoogleAuthError("Google did not return an account id.")

    # tokeninfo returns JSON strings, not booleans, for this field.
    verified = str(claims.get("email_verified", "")).lower() in {"true", "1"}
    email = str(claims.get("email") or "").strip().lower()

    return {
        "sub": sub,
        "email": email if verified else "",
        "email_verified": verified,
        "name": str(claims.get("name") or "").strip(),
    }


# ── Sign in with Apple ───────────────────────────────────────────────────────
_APPLE_ISSUER = "https://appleid.apple.com"
_APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"

# One PyJWKClient, created lazily and reused, because it CACHES Apple's signing
# keys. Building a fresh client per sign-in would fetch the JWKS every time --
# an outbound request on the auth path, and a hard dependency on Apple's key
# endpoint being reachable at that exact moment.
_apple_jwk_client = None
_apple_jwk_lock = threading.Lock()


class AppleAuthError(Exception):
    """The credential did not verify. The message is safe to show a user."""


def _get_apple_jwk_client():
    global _apple_jwk_client
    if _apple_jwk_client is None:
        with _apple_jwk_lock:
            if _apple_jwk_client is None:
                from jwt import PyJWKClient

                _apple_jwk_client = PyJWKClient(_APPLE_JWKS_URL, cache_keys=True)
    return _apple_jwk_client


def verify_apple_id_token(credential: str) -> dict:
    """Verify a Sign in with Apple ID token and return its claims.

    Returns a dict with at least `sub`, `email`, `email_verified`.

    Apple publishes NO tokeninfo-style endpoint, so unlike the Google path this
    cannot delegate: the token is verified locally against Apple's JWKS. PyJWT
    does the RS256 signature, the `exp` and -- given the two keyword arguments
    below -- the `aud` and `iss` checks, all of which are mandatory:

    * **`audience=APPLE_CLIENT_ID`**. Exactly the same trap as the Google path.
      Apple will sign a valid token for any Services ID; without pinning `aud`,
      a token obtained from any other Apple-integrated site would authenticate
      here. This is the single most important line in the function, and it is
      why an unset APPLE_CLIENT_ID DISABLES Apple sign-in rather than skipping
      the check.
    * **`issuer=_APPLE_ISSUER`**.

    Two Apple-specific facts callers must handle rather than assume away:

    * `email` is frequently Apple's PRIVATE RELAY address if the user chose
      "Hide My Email". That address is real and deliverable, and it is the only
      address we will ever get for them -- do not treat it as a placeholder.
    * There is NO name claim, ever. Apple returns the user's name once, in the
      authorization RESPONSE on first sign-up only, outside the token. The
      caller may pass it through separately; it is never trusted for identity.

    `email_verified` arrives as either a bool or the string "true", the same
    inconsistency Google's tokeninfo has. An unverified address is dropped for
    the same reason as there: it must not appear in the admin list as a contact
    address nobody has proved they own."""
    if not APPLE_CLIENT_ID:
        raise AppleAuthError("Apple sign-in is not configured on this server.")
    credential = (credential or "").strip()
    if not credential:
        raise AppleAuthError("Missing Apple credential.")

    import jwt as pyjwt

    try:
        signing_key = _get_apple_jwk_client().get_signing_key_from_jwt(credential)
    except Exception:
        # Network/DNS/timeout, or a token whose `kid` isn't in Apple's key set.
        # Deliberately distinct wording from a rejected token so a user retrying
        # a transient failure isn't told their account is invalid.
        raise AppleAuthError("Could not reach Apple to verify your sign-in. Please try again.")

    try:
        claims = pyjwt.decode(
            credential,
            signing_key.key,
            algorithms=["RS256"],
            audience=APPLE_CLIENT_ID,
            issuer=_APPLE_ISSUER,
            options={"require": ["exp", "iss", "aud", "sub"]},
        )
    except pyjwt.InvalidAudienceError:
        raise AppleAuthError("That sign-in was issued for a different application.")
    except pyjwt.ExpiredSignatureError:
        raise AppleAuthError("That Apple sign-in has expired. Please try again.")
    except Exception:
        raise AppleAuthError("Apple rejected that sign-in. Please try again.")

    sub = str(claims.get("sub") or "").strip()
    if not sub:
        raise AppleAuthError("Apple did not return an account id.")

    raw_verified = claims.get("email_verified", False)
    verified = raw_verified is True or str(raw_verified).lower() in {"true", "1"}
    email = str(claims.get("email") or "").strip().lower()

    return {
        "sub": sub,
        "email": email if verified else "",
        "email_verified": verified,
        "is_private_email": str(claims.get("is_private_email", "")).lower() in {"true", "1"},
    }


# ── Email + password sign-up ─────────────────────────────────────────────────
# Deliberately permissive: one @, something either side, a dot in the domain, no
# whitespace. A stricter pattern's only achievable outcome is rejecting a valid
# address (RFC 5322 permits far more than any regex people actually write), and
# a regex is the wrong instrument for the question anyway -- deliverability is
# now settled empirically, by whether the verification email's link ever gets
# clicked (services/mailer.py, User.email_verified). A pattern that guesses
# wrong rejects a real person at the sign-up form; an undeliverable address that
# gets through simply never verifies, which is visible and recoverable.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(?:\.[^@\s.]+)+$")


class EmailAuthError(Exception):
    """The email/password pair was rejected. The message is safe to show a user."""


def normalize_email(email: str) -> str:
    """Lowercased and stripped. Applied on BOTH registration and sign-in, or an
    address typed with a capital on one and not the other would fail to match
    its own account."""
    return (email or "").strip().lower()


def validate_password(password: str) -> None:
    """Raise EmailAuthError unless `password` is an acceptable new password.

    Length is the only password rule (see config.PASSWORD_MIN_LENGTH). The upper
    bound is not cosmetic: pbkdf2 hashes whatever it is handed, so an unbounded
    password field is a free CPU-exhaustion lever against an unauthenticated
    endpoint.

    Split out from validate_email_and_password so the password-reset route --
    which changes a password without being given an address -- enforces exactly
    the same rules. Two copies of a password policy is how the reset endpoint
    quietly ends up accepting a 3-character password."""
    if len(password or "") < PASSWORD_MIN_LENGTH:
        raise EmailAuthError(f"Please use a password of at least {PASSWORD_MIN_LENGTH} characters.")
    if len(password) > 1024:
        raise EmailAuthError("That password is too long.")


def validate_email_and_password(email: str, password: str) -> str:
    """Return the normalized email, or raise EmailAuthError."""
    email = normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise EmailAuthError("That doesn't look like an email address.")
    if len(email) > 254:  # the practical RFC limit
        raise EmailAuthError("That email address is too long.")
    validate_password(password)
    return email


# ── Email verification + password reset tokens ───────────────────────────────
# Both are STATELESS: an itsdangerous signature over a small payload, with no
# row anywhere recording that a link was issued. That is a deliberate choice
# over a tokens table, and it rests on each token carrying whatever makes it
# self-invalidating:
#
#   * verification -- carries the address it was issued for, so a token cannot
#     be replayed against an account whose email later differs, and re-clicking
#     is simply idempotent.
#   * reset -- carries a FINGERPRINT of the password it was issued against, so
#     using it changes the fingerprint and every outstanding link for that
#     account dies at once. Single-use falls out of the construction rather than
#     being enforced by a `used_at` column somebody has to remember to check,
#     and "I reset my password, now revoke the emails" is handled for free.
#
# Distinct `salt=` values per purpose are what stop a token minted for one being
# accepted by the other. A verification link that could be replayed as a
# password reset would be a full account takeover via an old email.
_verify_serializer = URLSafeTimedSerializer(AUTH_SECRET, salt="email-verify-v1")
_reset_serializer = URLSafeTimedSerializer(AUTH_SECRET, salt="password-reset-v1")


def _password_fingerprint(password_hash: str, salt: str) -> str:
    """A short, non-reversible tag for the account's CURRENT password.

    Truncated to 16 hex chars: it is compared against a value we minted
    ourselves and is never a secret, so it only needs to be long enough that a
    different password practically never collides with it. Hashing rather than
    embedding the stored hash keeps the (already-hashed) credential out of a
    string that travels through somebody's mail provider."""
    return hashlib.sha256(f"{password_hash}:{salt}".encode("utf-8")).hexdigest()[:16]


def make_email_verification_token(user_id: int, email: str) -> str:
    return _verify_serializer.dumps({"uid": int(user_id), "em": (email or "").strip().lower()})


def parse_email_verification_token(token: str, max_age: int) -> tuple[int, str] | None:
    """Return (user_id, email) from a valid, unexpired link, else None."""
    try:
        data = _verify_serializer.loads(token, max_age=max_age)
        return int(data["uid"]), str(data["em"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def make_password_reset_token(user_id: int, password_hash: str, salt: str) -> str:
    return _reset_serializer.dumps(
        {"uid": int(user_id), "pw": _password_fingerprint(password_hash, salt)}
    )


def parse_password_reset_token(token: str, max_age: int) -> tuple[int, str] | None:
    """Return (user_id, fingerprint) from a valid, unexpired link, else None.

    The caller must still compare the fingerprint against the account's current
    one -- that comparison is what makes the link single-use, and it cannot be
    done here without a DB session."""
    try:
        data = _reset_serializer.loads(token, max_age=max_age)
        return int(data["uid"]), str(data["pw"])
    except (BadSignature, SignatureExpired, KeyError, ValueError, TypeError):
        return None


def password_fingerprint_matches(token_fingerprint: str, password_hash: str, salt: str) -> bool:
    """Whether a reset token was issued against the password still on the account."""
    return hmac.compare_digest(
        token_fingerprint or "", _password_fingerprint(password_hash, salt)
    )


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
