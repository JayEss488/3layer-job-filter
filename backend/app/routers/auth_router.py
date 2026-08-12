"""Sign-in and the post-signup survey.

FOUR ways in. Three are self-serve and offered side by side on the homepage;
the fourth is legacy and reachable only from the footer:

* **POST /auth/google** -- verifies a Google Identity Services ID token.
* **POST /auth/apple** -- verifies a Sign in with Apple ID token.
* **POST /auth/email/register** + **POST /auth/email/login** -- an email address
  and a password, no third-party account required. Backed by four more routes
  that are what make it a peer of the providers rather than a fallback:
  **/auth/email/verify**, **/auth/email/resend**, **/auth/password/forgot** and
  **/auth/password/reset**. Before those existed nobody had proved they owned
  the address they signed up with, and a forgotten password was permanent
  lockout with no recovery path in the product at all.
* **POST /login** -- the original hand-assigned beta credentials
  (scripts/gen_beta_users.py). Kept working so the first cohort isn't locked out
  by the switch; the frontend only surfaces it from the homepage footer.
  Deliberately NOT given a reset flow: those accounts have no verified address
  to send one to, so a reset would email whoever happens to occupy whatever
  address is on the row.

All three self-serve paths create the account on first sight and return the same
Bearer token every other route already expects. There is no approval step:
"instant access" is the product decision these endpoints implement, so nothing
here can block a brand-new user from reaching the app.

Why THREE providers rather than just Google: every user who does not have (or
does not want to use) a Google account is lost at the first screen, and there is
no way to see how many that is -- they never reach a page that could tell us.
Apple and email cost one verification path each; that is the cheapest thing in
this codebase relative to what it protects against.

Accounts are never linked across providers and `email` is never an identity.
See models.User for why -- linking on an unverified email is an account
takeover, not a convenience.

The survey gate lives here rather than in a middleware on purpose. A middleware
rejecting every request until the survey is answered would also reject the
survey submission itself, and would turn a product nudge into an auth failure
(401/403) that the frontend's token-expiry handling would misread as a logged-out
session. Instead the two survey endpoints report state, the login responses carry
`needs_survey`, and the frontend holds the user on /welcome. A determined user
can bypass it with devtools -- that is an acceptable outcome for two optional-in-
spirit questions, and vastly preferable to any chance of locking a paying-attention
user out of the app they just signed up for.
"""
import json
import re
import secrets
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (
    EMAIL_VERIFICATION_REQUIRED,
    EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS,
    EXIT_SURVEY_FEATURE_CHOICES,
    EXIT_SURVEY_SPEED_CHOICES,
    PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS,
    SIGNUP_PRIORITY_CHOICES,
)
from ..database import get_db
from ..deps import current_user_id
from ..models import FeedbackResponse, SignupSurvey, User
from ..schemas import (
    AppleAuthIn,
    EmailAuthIn,
    ExitSurveyIn,
    ExitSurveyOut,
    ForgotPasswordIn,
    GoogleAuthIn,
    LoginIn,
    LoginOut,
    MeOut,
    MessageOut,
    ResetPasswordIn,
    SurveyIn,
    SurveyOut,
    TokenIn,
)
from ..services import mailer
from ..services.analytics import log_event, record_feedback
from ..services.auth import (
    AppleAuthError,
    EmailAuthError,
    GoogleAuthError,
    hash_password,
    make_email_verification_token,
    make_password_reset_token,
    make_token,
    normalize_email,
    parse_email_verification_token,
    parse_password_reset_token,
    password_fingerprint_matches,
    unusable_password,
    validate_email_and_password,
    validate_password,
    verify_apple_id_token,
    verify_google_id_token,
    verify_password,
)
from ..services.beta import EXIT_SURFACE, beta_fields

router = APIRouter(tags=["auth"])


# ── Helpers ──────────────────────────────────────────────────────────────────
def _needs_survey(db: Session, user: User) -> bool:
    """True when this account should be held on /welcome.

    Only SELF-SERVE accounts are ever asked -- Google, Apple or email+password.
    A hand-assigned beta account predates the survey and its holder has usually
    already answered the same questions by other means; asking them on next
    login would read as the app breaking, not as onboarding.

    Keyed on `auth_provider`, not on `google_sub`. Those were equivalent while
    Google was the only self-serve path, and stopped being when email+password
    arrived: an email account has a real password hash and no subject id, which
    is byte-identical in shape to a legacy account. NULL still means legacy (see
    models.User and database._migrate_auth_provider)."""
    if not user.auth_provider:
        return False
    return db.execute(
        select(SignupSurvey.id).where(SignupSurvey.user_id == user.id)
    ).scalar_one_or_none() is None


def _replace_feedback(db: Session, user_id: int, surface: str) -> None:
    """Drop this user's existing answers for one surface, ready to re-record.

    Both surveys are idempotent by overwrite rather than 409 (see
    submit_survey's docstring) -- someone who double-clicks or navigates back
    should not get an error, and should not end up with two contradictory sets
    of answers in the store either. Does not commit; the caller does."""
    db.query(FeedbackResponse).filter(
        FeedbackResponse.user_id == user_id,
        FeedbackResponse.surface == surface,
    ).delete(synchronize_session=False)


def _unique_username(db: Session, preferred: str) -> str:
    """A free `username`, derived from the Google email's local part.

    `username` is NOT NULL + UNIQUE and is what the existing UI (Nav, admin
    analytics) displays, so a Google account still needs one. Collisions are
    expected -- two people at different domains can both be `jsmith` -- and are
    resolved with a numeric suffix rather than by falling back to the email,
    which would put the full address in the app header.

    Never used as an identity: the account is found by `google_sub`, so a suffix
    here changes nothing about who the user is."""
    base = re.sub(r"[^a-z0-9._-]+", "", (preferred or "").lower()).strip("._-") or "user"
    base = base[:24]
    candidate = base
    n = 1
    while db.execute(select(User.id).where(User.username == candidate)).scalar_one_or_none() is not None:
        n += 1
        candidate = f"{base}{n}"
        if n > 9999:  # pathological; fall back to something certainly free
            candidate = f"{base}-{secrets.token_hex(4)}"
            break
    return candidate


# ── Shared account plumbing for the three self-serve paths ───────────────────
def _login_out(db: Session, user: User, *, is_new: bool = False) -> LoginOut:
    """The one response shape every sign-in path returns. Shared so a field
    added to LoginOut cannot be wired up on two of the three paths and silently
    missed on the third -- which for `needs_survey` would mean a whole provider's
    users skipping the survey gate with nothing anywhere reporting it."""
    return LoginOut(
        token=make_token(user.id),
        user_id=user.id,
        username=user.username,
        needs_survey=_needs_survey(db, user),
        email=user.email or "",
        display_name=user.display_name or "",
        is_new=is_new,
        email_verified=bool(user.email_verified),
        auth_provider=user.auth_provider or "",
        **beta_fields(db, user),
    )


def _new_self_serve_user(db: Session, *, provider: str, email: str,
                         email_verified: bool, display_name: str = "",
                         google_sub: str = "", apple_sub: str = "",
                         password: str = "") -> User:
    """Create an account for any of the three self-serve paths.

    `beta_started_at` is stamped HERE and only here, so day 0 of the fixed
    window is "the account was created" for every provider equally. Pre-existing
    accounts keep NULL and stay exempt -- see models.User.beta_started_at, and
    do not move this into the migration.

    A provider account gets the empty-hash sentinel (see auth.unusable_password)
    rather than a NULL, which is what makes it unreachable through any
    password endpoint whatever is sent."""
    from datetime import datetime

    user = User(
        username=_unique_username(db, email.split("@")[0] if email else provider),
        auth_provider=provider,
        google_sub=google_sub or None,
        apple_sub=apple_sub or None,
        email=email or None,
        email_verified=bool(email_verified),
        display_name=display_name or None,
        beta_started_at=datetime.utcnow(),
    )
    if password:
        user.password_hash, user.salt = hash_password(password)
    else:
        user.password_hash, user.salt = unusable_password()
    db.add(user)
    db.flush()  # assign user.id before log_event / token minting
    return user


def _finish_login(db: Session, user: User, is_new: bool) -> LoginOut:
    from datetime import datetime

    user.last_login_at = datetime.utcnow()
    log_event(db, user.id, None, "signup" if is_new else "login")
    db.commit()
    return _login_out(db, user, is_new=is_new)


def _provider_http_error(e: Exception) -> HTTPException:
    """503 when the server simply isn't configured -- that is our problem, not a
    bad credential, and a 401 would tell the user to try different details that
    would fail identically."""
    code = 503 if "not configured" in str(e) else 401
    return HTTPException(status_code=code, detail=str(e))


# ── Outbound mail ────────────────────────────────────────────────────────────
# Every send goes through BackgroundTasks, so the HTTP response is never waiting
# on Resend. That matters most on the route where it is least obvious:
# registration. A user whose account was created successfully must not watch a
# spinner for a 10-second mail timeout and conclude sign-up is broken -- the
# account exists either way, and mailer.send_email swallows its own failures.
#
# The rate-limit CHECK, by contrast, is deliberately synchronous and on the
# request thread: a caller that has exhausted its allowance must be told so, and
# a limiter consulted inside a background task could only decline silently.
def _queue_verification_email(tasks: BackgroundTasks, user: User) -> None:
    """Send this user a fresh confirm-your-address link, if there is anything to
    confirm. A no-op for an account with no address, or one already verified --
    re-confirming a confirmed address is a link that does nothing, arriving
    unprompted."""
    if not user.email or user.email_verified:
        return
    token = make_email_verification_token(user.id, user.email)
    tasks.add_task(mailer.send_verification_email, user.email, token)


def _rate_limit_or_429(address_key: str, request: Request) -> None:
    """Consume one send allowance against both the address and the caller, or 429.

    Both are checked because they guard different attacks: the ADDRESS key stops
    one victim's inbox being flooded from many clients, the CLIENT key stops one
    client walking a list of addresses. Neither alone is enough.

    Their LIMITS are very different and must stay so. An IP is not a person: a
    shared office connection or a mobile carrier's CGNAT puts many unrelated
    users behind one address, so a client allowance as tight as the per-address
    one would refuse the fifth person on that network to forget their password
    because of four strangers. (This was not hypothetical -- it fell out of
    testing the moment two flows shared a client.) See
    config.EMAIL_SEND_MAX_PER_HOUR_PER_CLIENT.

    The message is deliberately about sending, not about accounts -- on
    /auth/password/forgot even a rate-limit response must not become a way to
    learn whether an address is registered, and it doesn't: the limiter is
    consumed before any lookup happens, so it responds identically either way."""
    ok = mailer.rate_limit_ok(address_key) if address_key else True
    if ok:
        ok = mailer.client_rate_limit_ok(_client_key(request))
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="Too many emails requested just now. Please try again in a little while.",
        )


def _client_key(request: Request) -> str:
    return f"ip:{request.client.host}" if request and request.client else "ip:unknown"


# ── Google (self-serve) ──────────────────────────────────────────────────────
@router.post("/auth/google", response_model=LoginOut)
def google_auth(body: GoogleAuthIn, db: Session = Depends(get_db)):
    """Public: exchange a verified Google ID token for a Bearer token.

    Sign-up and sign-in are the same call deliberately. The browser cannot know
    whether this Google account has been here before, and asking the user to
    pick the right button ("sign up" vs "sign in") is a step that only ever
    produces a wrong answer and a confusing error."""
    try:
        claims = verify_google_id_token(body.credential)
    except GoogleAuthError as e:
        raise _provider_http_error(e)

    sub = claims["sub"]
    email = claims["email"]

    user = db.execute(select(User).where(User.google_sub == sub)).scalar_one_or_none()
    is_new = user is None

    if user is None:
        user = _new_self_serve_user(
            db, provider="google", google_sub=sub, email=email,
            email_verified=bool(claims.get("email_verified")),
            display_name=claims.get("name") or "",
        )
    else:
        # Google is the source of truth for these; refresh so a changed display
        # name or a newly-verified address shows up without a support ticket.
        if email:
            user.email = email
            user.email_verified = True
        if claims.get("name"):
            user.display_name = claims["name"]

    return _finish_login(db, user, is_new)


# ── Apple (self-serve) ───────────────────────────────────────────────────────
@router.post("/auth/apple", response_model=LoginOut)
def apple_auth(body: AppleAuthIn, db: Session = Depends(get_db)):
    """Public: exchange a verified Apple ID token for a Bearer token.

    Structurally identical to the Google route -- same sign-up-is-sign-in
    decision, same `sub`-as-join-key rule -- with two Apple-specific
    accommodations, both driven by Apple returning LESS than Google does:

    * **The name arrives once or never.** Apple puts it in the authorization
      response on the first sign-up only, never in the token, so it comes in on
      the request body rather than out of verified claims. It is therefore
      written only when creating the account and only when we have nothing
      better, and it is never trusted for anything but display -- identity is
      the token's verified `sub` alone.
    * **The email is often Apple's private relay.** Stored as-is; it is real,
      deliverable, and the only address this user will ever give us. A later
      sign-in must never overwrite a stored address with a blank one, which is
      why the refresh below is conditional."""
    try:
        claims = verify_apple_id_token(body.credential)
    except AppleAuthError as e:
        raise _provider_http_error(e)

    sub = claims["sub"]
    email = claims["email"]
    name = (body.name or "").strip()[:120]

    user = db.execute(select(User).where(User.apple_sub == sub)).scalar_one_or_none()
    is_new = user is None

    if user is None:
        user = _new_self_serve_user(
            db, provider="apple", apple_sub=sub, email=email,
            email_verified=bool(claims.get("email_verified")),
            display_name=name,
        )
    else:
        if email:
            user.email = email
            user.email_verified = True
        if name and not user.display_name:
            user.display_name = name

    return _finish_login(db, user, is_new)


# ── Email + password (self-serve) ────────────────────────────────────────────
# Two endpoints, not one, and they are NOT the same call the way Google's
# sign-up/sign-in are. There the browser genuinely cannot know whether the
# account exists, so asking would only produce wrong answers. Here the user
# knows perfectly well whether they have registered before, and merging the two
# would mean a mistyped password on an existing account silently creating a
# SECOND account under a near-miss email -- the user then finds an empty profile
# and no way to see why.
#
# Deliberately separate from POST /login, which serves the legacy hand-assigned
# credentials keyed on USERNAME. Sharing one endpoint would mean one lookup
# having to decide whether a submitted string is a username or an email.
def _require_email_signup_enabled() -> None:
    from ..config import EMAIL_SIGNUP_ENABLED

    if not EMAIL_SIGNUP_ENABLED:
        raise HTTPException(status_code=503,
                            detail="Email sign-up is not configured on this server.")


@router.post("/auth/email/register", response_model=LoginOut)
def email_register(body: EmailAuthIn, tasks: BackgroundTasks, request: Request,
                   db: Session = Depends(get_db)):
    """Public: create an account from an email address and a password.

    The account is created and signed in IMMEDIATELY, and the verification email
    is a background task whose outcome is not waited on. That ordering is the
    product invariant this whole router is built around: nothing in the auth
    path may block a new user. With EMAIL_VERIFICATION_REQUIRED off (the
    default) an unconfirmed address costs the user only a banner; see
    config.EMAIL_VERIFICATION_REQUIRED for when to tighten it and why it is not
    tight by default.

    409 on an email already in use, by ANY account and any provider. That is
    what stops the two-accounts-one-person case, and it is deliberately NOT
    solved by linking to the existing account: an unverified registration for
    victim@example.com that later merged with the victim's verified Google
    identity would be an account takeover with a signup form as the only tool
    required. It does let someone probe whether an address is registered, which
    is an accepted and near-universal property of signup forms, and a far
    smaller problem than the one it prevents."""
    _require_email_signup_enabled()
    try:
        email = validate_email_and_password(body.email, body.password)
    except EmailAuthError as e:
        raise HTTPException(status_code=422, detail=str(e))

    # Before the account is created, so a scripted signup loop cannot spend the
    # mail quota (or our sending reputation) faster than the limiter allows.
    _rate_limit_or_429(f"verify:{email}", request)

    existing = db.execute(select(User).where(User.email == email)).scalars().first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=("An account already exists for that email address. Sign in instead, "
                    "or use the Google or Apple button if that's how you signed up."),
        )

    user = _new_self_serve_user(db, provider="email", email=email,
                                email_verified=False, password=body.password)
    out = _finish_login(db, user, is_new=True)
    _queue_verification_email(tasks, user)
    return out


@router.post("/auth/email/login", response_model=LoginOut)
def email_login(body: EmailAuthIn, db: Session = Depends(get_db)):
    """Public: exchange an email + password for a Bearer token.

    Deliberately vague on failure (one message for unknown address and wrong
    password), matching POST /login.

    Scoped to `auth_provider == "email"`. A Google or Apple account stores the
    empty-hash sentinel, so verify_password could never match it anyway -- but
    the scoping makes that a property of the QUERY rather than of a comparison
    somewhere else, so this stays safe even if the sentinel is ever changed.

    The unverified-address check runs AFTER the password check, never before.
    Reversed, the endpoint would tell an anonymous caller "that address exists
    but isn't confirmed" for any address they typed -- an account-enumeration
    oracle bolted onto the one route that goes to lengths to avoid being one."""
    _require_email_signup_enabled()
    email = normalize_email(body.email)
    user = db.execute(
        select(User).where(User.email == email, User.auth_provider == "email")
    ).scalars().first()
    if not user or not verify_password(body.password or "", user.salt, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if EMAIL_VERIFICATION_REQUIRED and not user.email_verified:
        raise HTTPException(
            status_code=403,
            detail=("Please confirm your email address first -- check your inbox for the "
                    "link we sent when you signed up."),
        )
    return _finish_login(db, user, is_new=False)


# ── Email verification ───────────────────────────────────────────────────────
# Stateless links: no issued-tokens table, because each token carries what makes
# it self-invalidating (see services/auth.py). The consequence worth knowing is
# that a verification link cannot be revoked early -- only expired. That is
# acceptable for a token whose entire power is to assert "this mailbox
# received mail", and is emphatically NOT acceptable for the reset token below,
# which is why that one carries a password fingerprint instead.
@router.post("/auth/email/verify", response_model=LoginOut)
def email_verify(body: TokenIn, db: Session = Depends(get_db)):
    """Public: consume a confirm-your-address link.

    Returns a full LoginOut, i.e. clicking the link SIGNS YOU IN. That is
    deliberate and it is the difference between a link that works and one that
    strands people: mail is very often opened on a different device from the one
    the account was created on, and a bare "confirmed, now go and sign in"
    ending would send that user to a form to re-type a password they set on
    their laptop. The link is proof of mailbox control, which is exactly the
    proof every password-reset flow on the internet already accepts as
    sufficient to take over an account -- so treating it as sign-in evidence
    adds no new capability to a holder of the link.

    Public (no token required) for the same reason.

    Idempotent: a second click on an already-confirmed link is success, not an
    error. People forward these to themselves, mail clients pre-fetch them, and
    "this link has already been used" is indistinguishable to the reader from
    "something is broken"."""
    parsed = parse_email_verification_token(body.token or "", EMAIL_VERIFY_TOKEN_MAX_AGE_SECONDS)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail="That confirmation link is invalid or has expired. Sign in and request a new one.",
        )
    uid, email = parsed
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status_code=400, detail="That confirmation link is no longer valid.")

    # The address is pinned into the token, so a link issued for one address can
    # never confirm a different one -- the case that matters is an account whose
    # email changed after the link was sent.
    if normalize_email(user.email or "") != normalize_email(email):
        raise HTTPException(
            status_code=400,
            detail="That confirmation link was issued for a different email address.",
        )

    if not user.email_verified:
        user.email_verified = True
        log_event(db, user.id, None, "email_verified")
    return _finish_login(db, user, is_new=False)


@router.post("/auth/email/resend", response_model=MessageOut)
def email_resend_verification(tasks: BackgroundTasks, request: Request,
                              db: Session = Depends(get_db)):
    """Send this signed-in user another confirmation link.

    AUTHED, unlike the reset route below, and the asymmetry is the point: here
    the caller has already proved they hold the account, so there is no address
    to accept from them and nothing to enumerate. The reset route cannot make
    that assumption -- its whole purpose is serving someone who has lost their
    way in."""
    uid = current_user_id()
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status_code=401, detail="Unknown user")
    if user.auth_provider != "email" or not user.email:
        raise HTTPException(status_code=400, detail="This account has no address to confirm.")
    if user.email_verified:
        return MessageOut(message="That address is already confirmed.")

    _rate_limit_or_429(f"verify:{normalize_email(user.email)}", request)
    _queue_verification_email(tasks, user)
    return MessageOut(message="Sent. Check your inbox for the confirmation link.")


# ── Password reset ───────────────────────────────────────────────────────────
@router.post("/auth/password/forgot", response_model=MessageOut)
def password_forgot(body: ForgotPasswordIn, tasks: BackgroundTasks, request: Request,
                    db: Session = Depends(get_db)):
    """Public: email a reset link, if that address has a password account.

    **Always answers identically**, whether or not the address is registered and
    whether or not the send succeeded. An honest 404 here would turn this into a
    membership oracle for any address someone cares to type -- and unlike the
    registration 409, which is unavoidable (a signup form has to refuse a
    duplicate somehow), this route has no such constraint, so there is no reason
    to leak anything.

    Silently does nothing for a Google or Apple account. Those have no password
    to reset, and a reset link that granted a password to an account reachable
    by a provider identity would create a SECOND credential for it -- exactly
    the cross-provider linking the User model refuses on takeover grounds."""
    _require_email_signup_enabled()
    email = normalize_email(body.email)
    # Consumed before the lookup, so a throttled caller and an unregistered
    # address are still indistinguishable from each other in timing terms.
    _rate_limit_or_429(f"reset:{email}", request)

    ack = MessageOut(
        message="If there's an account for that address, a reset link is on its way.",
    )
    if not email:
        return ack

    user = db.execute(
        select(User).where(User.email == email, User.auth_provider == "email")
    ).scalars().first()
    if user is None:
        return ack

    token = make_password_reset_token(user.id, user.password_hash, user.salt)
    tasks.add_task(mailer.send_password_reset_email, user.email, token)
    log_event(db, user.id, None, "password_reset_requested")
    db.commit()
    return ack


@router.post("/auth/password/reset", response_model=LoginOut)
def password_reset(body: ResetPasswordIn, db: Session = Depends(get_db)):
    """Public: set a new password from a reset link, and sign the user in.

    Single-use falls out of the token's construction rather than out of a
    `used_at` column: the token carries a fingerprint of the password it was
    minted against, so completing a reset changes the fingerprint and every
    outstanding link for that account stops verifying at the same moment. A user
    who requested three links and used one has not left two live back doors in
    their inbox.

    One consequence to keep in mind before "improving" this: it also means a
    reset cannot be undone by re-using the previous link. That is the correct
    behaviour for the case that matters -- someone resetting BECAUSE they think
    a stranger has their password -- and it is the reason not to swap the
    fingerprint for a nonce table that expires links independently.

    Confirming the address as a side effect is safe and not an oversight:
    holding a link that was delivered to that mailbox is proof of exactly what
    the verification email asks for, so leaving the account unverified after a
    successful reset would nag the user about something they just demonstrated.
    """
    _require_email_signup_enabled()
    parsed = parse_password_reset_token(body.token or "", PASSWORD_RESET_TOKEN_MAX_AGE_SECONDS)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail="That reset link is invalid or has expired. Request a new one.",
        )
    uid, fingerprint = parsed

    try:
        validate_password(body.password or "")
    except EmailAuthError as e:
        raise HTTPException(status_code=422, detail=str(e))

    user = db.get(User, uid)
    # The provider check is repeated here rather than trusted from issue time:
    # tokens are stateless and long-lived relative to a config change, and a
    # password written onto a provider account would be a second credential for
    # an identity that is supposed to have exactly one.
    if user is None or user.auth_provider != "email":
        raise HTTPException(status_code=400, detail="That reset link is no longer valid.")

    if not password_fingerprint_matches(fingerprint, user.password_hash, user.salt):
        raise HTTPException(
            status_code=400,
            detail="That reset link has already been used. Request a new one if you still need it.",
        )

    user.password_hash, user.salt = hash_password(body.password)
    # See the docstring: the link proves mailbox control, which is the same
    # thing the verification email exists to establish.
    user.email_verified = True
    log_event(db, user.id, None, "password_reset")
    return _finish_login(db, user, is_new=False)


@router.get("/auth/config")
def auth_config():
    """Public: what sign-in methods this deployment actually supports.

    The frontend needs this to decide which buttons to render at all. Rendering
    a button that 503s is a worse first impression than not offering that
    method, and both client ids are public anyway (they ship in the providers'
    own script tags).

    Read at RUN time, so a frontend built before a credential existed still
    lights the button up the moment the server is configured -- which is why the
    client ids come from here rather than from NEXT_PUBLIC_ env vars baked in at
    build time."""
    from ..config import APPLE_CLIENT_ID, EMAIL_SIGNUP_ENABLED, GOOGLE_CLIENT_ID

    return {
        "google_enabled": bool(GOOGLE_CLIENT_ID),
        "google_client_id": GOOGLE_CLIENT_ID,
        "apple_enabled": bool(APPLE_CLIENT_ID),
        "apple_client_id": APPLE_CLIENT_ID,
        "email_enabled": bool(EMAIL_SIGNUP_ENABLED),
        # Whether a link will actually be sent. The frontend needs this to avoid
        # telling someone to "check your inbox" on a deployment with no
        # RESEND_API_KEY -- a confident instruction to wait for mail that will
        # never arrive is worse than saying nothing, because the user's next
        # move is to wait rather than to ask.
        "mail_enabled": mailer.mail_enabled(),
        # Whether an unconfirmed address blocks sign-in. Drives the wording on
        # the sign-up form: "you're straight in" versus "confirm before you can
        # sign in" are different promises and the form must make the right one.
        "email_verification_required": bool(EMAIL_VERIFICATION_REQUIRED),
    }


# ── Legacy hand-assigned credentials ─────────────────────────────────────────
@router.post("/login", response_model=LoginOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    """Public: exchange username+password for a signed Bearer token.

    Deliberately vague on failure (same message for unknown user and wrong
    password) so the endpoint can't be used to enumerate valid usernames.

    A Google or Apple account cannot be reached through here whatever password is
    sent -- its stored hash is the empty string, which verify_password never
    matches.

    Scoped to `auth_provider IS NULL`, i.e. to legacy accounts only. An email
    signup DOES have a real password hash, and its username is derived from the
    email's local part, so without this scoping "jay.test@example.com" could
    also sign in here as username "jay.test". That is the same credential and so
    not a weakening, but it makes /login quietly a second front door to a path
    that has its own endpoint -- and any rate-limiting, lockout or audit added to
    one would then silently not cover the other."""
    username = (body.username or "").strip()
    user = db.execute(
        select(User).where(User.username == username, User.auth_provider.is_(None))
    ).scalar_one_or_none()
    if not user or not verify_password(body.password or "", user.salt, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    log_event(db, user.id, None, "login")
    return _login_out(db, user)


@router.get("/me", response_model=MeOut)
def me(db: Session = Depends(get_db)):
    """Who the caller is. Self-guards: current_user_id() raises 401 when the
    request carried no valid token, so this needs no router-level auth dep.

    Carries `needs_survey` so a user who signs in on a second device (where the
    frontend's cached flag doesn't exist) is still routed to /welcome, and the
    beta-window fields for the same reason -- the frontend's cached
    expired/needs-exit-survey flags are a cache of THIS response, never the
    source of truth.

    Must stay reachable by an expired user: it is what tells the frontend the
    window has lapsed, so gating it would leave the client unable to explain
    why everything else is 403ing."""
    uid = current_user_id()
    user = db.get(User, uid)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return MeOut(
        user_id=user.id,
        username=user.username,
        needs_survey=_needs_survey(db, user),
        email=user.email or "",
        display_name=user.display_name or "",
        email_verified=bool(user.email_verified),
        auth_provider=user.auth_provider or "",
        **beta_fields(db, user),
    )


# ── Post-signup survey ───────────────────────────────────────────────────────
@router.get("/signup/survey", response_model=Optional[SurveyOut])
def get_survey(db: Session = Depends(get_db)):
    """This user's answers, or null if they haven't answered yet."""
    uid = current_user_id()
    row = db.execute(
        select(SignupSurvey).where(SignupSurvey.user_id == uid)
    ).scalar_one_or_none()
    if not row:
        return None
    return SurveyOut(priority=row.priority, used_ai_tool=row.used_ai_tool, created_at=row.created_at)


@router.post("/signup/survey", response_model=SurveyOut)
def submit_survey(body: SurveyIn, db: Session = Depends(get_db)):
    """Record the two sign-up questions. Idempotent: re-submitting overwrites,
    rather than 409-ing a user who double-clicked or came back via history."""
    uid = current_user_id()
    priority = (body.priority or "").strip()
    if priority not in SIGNUP_PRIORITY_CHOICES:
        raise HTTPException(
            status_code=422,
            detail=f"priority must be one of: {', '.join(SIGNUP_PRIORITY_CHOICES)}",
        )

    row = db.execute(
        select(SignupSurvey).where(SignupSurvey.user_id == uid)
    ).scalar_one_or_none()
    if row:
        row.priority = priority
        row.used_ai_tool = bool(body.used_ai_tool)
    else:
        row = SignupSurvey(user_id=uid, priority=priority, used_ai_tool=bool(body.used_ai_tool))
        db.add(row)

    # Mirror into the unified feedback store so all three surfaces are queryable
    # from one place. signup_surveys stays the source of truth -- it is the
    # survey GATE (_needs_survey reads it) and GET /admin/signups reads it
    # directly; this is a read-side convenience. Re-submits replace rather than
    # accumulate, matching the overwrite semantics above.
    _replace_feedback(db, uid, "signup")
    record_feedback(db, uid, "signup", "signup_priority", priority)
    record_feedback(db, uid, "signup", "signup_used_ai_tool", "yes" if body.used_ai_tool else "no")

    log_event(db, uid, None, "signup_survey")
    db.commit()
    db.refresh(row)
    return SurveyOut(priority=row.priority, used_ai_tool=row.used_ai_tool, created_at=row.created_at)


# ── Wrap-up survey (day EXIT_SURVEY_AFTER_DAYS onwards) ──────────────────────
# Deliberately on THIS router, which is public and self-guarding, rather than on
# a beta-gated one: these two routes have to work for a user whose window has
# already lapsed. Someone who never logged in between day 4 and day 7 hits the
# survey gate and the lapse gate at the same moment, and is exactly the person
# whose answers we most need -- serving them a 403 here would make the survey
# unanswerable by the group it exists to ask.
@router.get("/exit/survey", response_model=ExitSurveyOut)
def get_exit_survey(db: Session = Depends(get_db)):
    """This user's wrap-up answers, or an empty (`answered: false`) shape."""
    uid = current_user_id()
    rows = db.execute(
        select(FeedbackResponse)
        .where(FeedbackResponse.user_id == uid, FeedbackResponse.surface == EXIT_SURFACE)
        .order_by(FeedbackResponse.id)
    ).scalars().all()
    if not rows:
        return ExitSurveyOut()

    by_q = {r.question_id: r for r in rows}
    features: list[str] = []
    if "exit_useful_features" in by_q:
        try:
            features = json.loads(by_q["exit_useful_features"].answer)
        except (TypeError, ValueError):
            features = []
    return ExitSurveyOut(
        answered=True,
        change=by_q["exit_change"].answer if "exit_change" in by_q else "",
        useful_features=features,
        speed_tradeoff=by_q["exit_speed_tradeoff"].answer if "exit_speed_tradeoff" in by_q else "",
        created_at=rows[0].created_at,
    )


@router.post("/exit/survey", response_model=ExitSurveyOut)
def submit_exit_survey(body: ExitSurveyIn, db: Session = Depends(get_db)):
    """Record the three wrap-up questions. Idempotent: re-submitting replaces.

    Answering does NOT extend access past BETA_WINDOW_DAYS -- the survey gate
    and the lapse gate are independent (services/beta.py). Use
    POST /admin/users/{id}/beta to give someone more time."""
    uid = current_user_id()

    features = [f.strip() for f in body.useful_features if f and f.strip()]
    unknown = [f for f in features if f not in EXIT_SURVEY_FEATURE_CHOICES]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"useful_features must be from: {', '.join(EXIT_SURVEY_FEATURE_CHOICES)}",
        )
    speed = (body.speed_tradeoff or "").strip()
    if speed not in EXIT_SURVEY_SPEED_CHOICES:
        raise HTTPException(
            status_code=422,
            detail=f"speed_tradeoff must be one of: {', '.join(EXIT_SURVEY_SPEED_CHOICES)}",
        )
    # Zero ticks would be indistinguishable from "not answered" once stored, and
    # the gate reads presence-of-any-row to decide whether to release the user --
    # so an explicit "none" is required rather than inferred from emptiness.
    if not features:
        raise HTTPException(
            status_code=422,
            detail="useful_features must name at least one option (use 'none' for none of them)",
        )

    _replace_feedback(db, uid, EXIT_SURFACE)
    # `change` is optional -- record_feedback drops a blank rather than storing
    # an empty row that would look like a response in the admin readout.
    record_feedback(db, uid, EXIT_SURFACE, "exit_change", body.change)
    record_feedback(db, uid, EXIT_SURFACE, "exit_useful_features", features)
    record_feedback(db, uid, EXIT_SURFACE, "exit_speed_tradeoff", speed)

    log_event(db, uid, None, "exit_survey")
    db.commit()
    return get_exit_survey(db)
