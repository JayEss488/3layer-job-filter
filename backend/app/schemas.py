"""Pydantic request/response models."""
import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Auth ────────────────────────────────────────────────────────────────────
class LoginIn(BaseModel):
    username: str
    password: str


class BetaWindowOut(BaseModel):
    """The open-beta window fields carried by every identity response.

    All four are computed fresh from User.beta_started_at (services/beta.py),
    never stored, so changing the window length in config applies immediately to
    everyone already inside it.

    An account with no window (the legacy exemption) reports nulls and False
    throughout, so the frontend needs no special case for it."""

    beta_expires_at: Optional[datetime] = None
    beta_days_left: Optional[int] = None
    # Drives nothing on the client except messaging -- the real lapse is the 403
    # from require_active_beta. Reported so the UI can explain rather than just
    # bounce.
    beta_expired: bool = False
    # The day-4 gate: hold the user on /exit-survey until answered. Independent
    # of beta_expired; both can be true at once.
    needs_exit_survey: bool = False


class LoginOut(BetaWindowOut):
    token: str
    user_id: int
    username: str
    # True when this account has no SignupSurvey row yet. The frontend holds the
    # user on /welcome until it flips false; it is the whole survey gate. Legacy
    # password accounts predate the survey and are never asked (see the router).
    needs_survey: bool = False
    # Present for Google accounts, "" for hand-assigned beta credentials.
    email: str = ""
    display_name: str = ""
    # True the very first time an account signs in, so the frontend can route a
    # brand-new user to the survey instead of to the app shell.
    is_new: bool = False
    # Mirrors MeOut -- see there. Carried on the sign-in response too so a
    # freshly-registered user sees the "confirm your address" banner on the very
    # first paint, rather than only after the next /me lands.
    email_verified: bool = False
    auth_provider: str = ""


class GoogleAuthIn(BaseModel):
    """The `credential` field of the Google Identity Services callback -- a
    signed JWT ID token, verified server-side (services/auth.py)."""

    credential: str


class AppleAuthIn(BaseModel):
    """`authorization.id_token` from the AppleID.js sign-in response -- a signed
    JWT, verified server-side against Apple's JWKS (services/auth.py).

    `name` is carried separately and is NOT part of the token: Apple returns the
    user's name exactly once, in the authorization response on the very first
    sign-up, and never again. It is display-only and never trusted for identity
    -- an account is found by the token's verified `sub` and nothing else, so a
    forged name changes only what the header prints for that person."""

    credential: str
    name: str = ""


class EmailAuthIn(BaseModel):
    """Registration and sign-in both. One shape because the fields are the same
    and the endpoints differ only in what they do with an existing account."""

    email: str
    password: str


class ForgotPasswordIn(BaseModel):
    """An address to send a reset link to. Deliberately the ONLY field: adding
    anything the caller could use to narrow the account (a username, a provider)
    would turn a route that must not confirm whether an address is registered
    into one that can be probed."""

    email: str


class ResetPasswordIn(BaseModel):
    """A reset link's token plus the new password.

    The token identifies the account -- there is no email field, and there must
    not be. Taking the address from the request and the authorisation from the
    token would mean two independent claims about who this is, and a route whose
    correctness depends on them agreeing."""

    token: str
    password: str


class TokenIn(BaseModel):
    """A single opaque link token. Used by email verification."""

    token: str


class MessageOut(BaseModel):
    """A plain acknowledgement for the routes that must not report what they
    actually did (see POST /auth/password/forgot)."""

    ok: bool = True
    message: str = ""


class MeOut(BetaWindowOut):
    user_id: int
    username: str
    needs_survey: bool = False
    email: str = ""
    display_name: str = ""
    # Both exist so the app shell can decide whether to nag about an unconfirmed
    # address. It must only ever nag an EMAIL account: a Google or Apple account
    # can carry email_verified=False (the provider said the address was
    # unverified, so we dropped it), and there is nothing such a user could
    # click to fix it.
    email_verified: bool = False
    auth_provider: str = ""


class SurveyIn(BaseModel):
    priority: str
    used_ai_tool: bool


class SurveyOut(BaseModel):
    priority: str
    used_ai_tool: bool
    created_at: Optional[datetime] = None


# ── Wrap-up survey (asked from EXIT_SURVEY_AFTER_DAYS onwards) ───────────────
class ExitSurveyIn(BaseModel):
    """The three wrap-up questions.

    `change` is OPTIONAL and the other two are not. A required free-text box on
    a blocking page is where people either bail or type "n/a", and an answer
    nobody means looks like signal in the admin readout -- the same reasoning
    that put a first-class "None of these" on the sign-up survey. The two
    structured questions each carry their own "none"/neutral option instead, so
    "required" never means "pick something untrue"."""

    change: str = ""
    useful_features: list[str] = []
    speed_tradeoff: str


class ExitSurveyOut(BaseModel):
    answered: bool = False
    change: str = ""
    useful_features: list[str] = []
    speed_tradeoff: str = ""
    created_at: Optional[datetime] = None


# ── In-product feedback prompts ─────────────────────────────────────────────
class FeedbackIn(BaseModel):
    question_id: str
    answer: str
    profile_id: Optional[int] = None
    # The SearchRun this answer is about. Null for the setup prompt, which fires
    # at CV-parse time when no run exists yet.
    run_id: Optional[int] = None


class FeedbackPromptOut(BaseModel):
    due: bool = False
    run_id: Optional[int] = None


class FeedbackDueOut(BaseModel):
    """Which in-product prompts this profile should currently show.

    Computed server-side rather than in the client so the trigger rules live in
    one place and a user who answered on their laptop is not asked again on
    their phone -- localStorage cannot know what was already recorded."""

    results_quality: FeedbackPromptOut = FeedbackPromptOut()
    setup_ok: FeedbackPromptOut = FeedbackPromptOut()


# ── Profiles ────────────────────────────────────────────────────────────────
class ProfileCreate(BaseModel):
    name: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    intent_text: Optional[str] = None
    search_feedback: Optional[str] = None
    cv_summary: Optional[str] = None


class CommentIn(BaseModel):
    """Free-form product feedback from a beta tester -- bugs, confusing bits,
    feature ideas -- as opposed to ProfileUpdate.search_feedback, which tunes
    the judge for this profile's own search results."""
    text: str


class ProfileOut(ORMModel):
    id: int
    name: str
    is_active: bool
    intent_text: Optional[str] = None
    search_feedback: Optional[str] = None
    created_at: datetime
    updated_at: datetime


# ── Attributes ──────────────────────────────────────────────────────────────
class AttributeCreate(BaseModel):
    type: str
    value: str
    source: str = "user_added"
    confirmed: bool = True
    weight: Optional[float] = None
    proficiency: Optional[str] = None
    evidence_origin: Optional[str] = None
    family_id: Optional[int] = None
    pinned: Optional[bool] = None
    enforcement: Optional[str] = None


class AttributeUpdate(BaseModel):
    value: Optional[str] = None
    confirmed: Optional[bool] = None
    weight: Optional[float] = None
    proficiency: Optional[str] = None
    evidence_origin: Optional[str] = None
    family_id: Optional[int] = None
    pinned: Optional[bool] = None
    enforcement: Optional[str] = None


class AttributeOut(ORMModel):
    id: int
    profile_id: int
    type: str
    value: str
    weight: float
    source: str
    confirmed: bool
    proficiency: Optional[str] = None
    evidence_origin: Optional[str] = None
    family_id: Optional[int] = None
    pinned: bool = False
    # Null means "never set" -- the frontend resolves it through the same
    # per-type defaults the backend uses (config.enforcement_for), which is why
    # this stays nullable rather than being backfilled on read.
    enforcement: Optional[str] = None


# ── Role families ───────────────────────────────────────────────────────────
class FamilyCreate(BaseModel):
    name: str
    tier: Optional[str] = None


class FamilyUpdate(BaseModel):
    name: Optional[str] = None
    tier: Optional[str] = None
    position: Optional[int] = None


class FamilyOut(ORMModel):
    id: int
    profile_id: int
    name: str
    tier: str
    position: int


# ── Onboarding / parsing ────────────────────────────────────────────────────
class ParseTextIn(BaseModel):
    text: str


class SuggestIn(BaseModel):
    type: str
    context: Optional[str] = None


class SuggestOut(BaseModel):
    suggestions: list[str]


class ContextHeaderOut(BaseModel):
    """What the AI is told about the candidate, read-only (see profile_intel)."""

    header: str
    requirements: list[str]
    cv_summary: str


class ContextHeaderUpdate(BaseModel):
    """Manual edit of the AI-generated header -- see
    profile_intel.set_header_locked. cv_summary is edited via
    ProfileUpdate.cv_summary instead (a plain column, no lock needed)."""

    header: str


# ── Roles ───────────────────────────────────────────────────────────────────
class RoleOut(ORMModel):
    id: int
    profile_id: int
    search_run_id: Optional[int] = None
    external_id: Optional[str] = None
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    # Readable stand-in for `location` when the source gave a raw postcode, and
    # straight-line miles from the candidate's stated place. Both null far more
    # often than not -- see models.Role.
    location_label: Optional[str] = None
    distance_miles: Optional[int] = None
    # Three-state: True/False once checked, None when the listing named no
    # employer to check against the register. See models.Role.
    sponsor_licensed: Optional[bool] = None
    # What the listing itself says about sponsoring THIS role: "offered" |
    # "not_offered" | None (silent). A different question from sponsor_licensed
    # -- see models.Role -- and the quote is the employer's own sentence.
    sponsor_statement: Optional[str] = None
    sponsor_statement_quote: Optional[str] = None
    # When this listing was last directly confirmed to still exist.
    last_verified_at: Optional[datetime] = None
    url: Optional[str] = None
    tags: Optional[Any] = None
    salary_text: Optional[str] = None
    # salary_text parsed into comparable numbers (services/salary.py), in
    # salary_period's units and NOT annualised. All null together when nothing
    # parseable was stated, in which case salary_text is what the card shows.
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    salary_period: Optional[str] = None
    salary_currency: Optional[str] = None
    # True when salary_min/max is a modelled estimate (Adzuna's own
    # salary_is_predicted) rather than an employer/board-stated figure.
    salary_is_predicted: Optional[bool] = None
    source: Optional[str] = None
    fit_rank: Optional[int] = None
    rank_score: Optional[int] = None
    provisional: bool = False
    provisional_stage: Optional[str] = None  # "embed" | "rank" | None -- see models.Role
    ai_analysis: Optional[str] = None
    verdict: Optional[str] = None
    work_style: Optional[str] = None
    seniority_level: Optional[str] = None
    deadline_text: Optional[str] = None
    posted_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None
    posted_at_approx: Optional[bool] = None
    # "high" | "medium" | None, plus the named rules behind it. None is the
    # common case and means NOTHING FIRED, not "unknown" -- every ghost rule
    # fires on positive evidence and none fires on missing data (services/ghost.py).
    # ghost_signals is stored as a JSON string and decoded here so the card gets
    # a real list.
    ghost_level: Optional[str] = None
    ghost_signals: Optional[list[str]] = None
    status: str
    application_status: Optional[str] = None
    applied_at: Optional[datetime] = None
    # When the employer first responded, or the user declared no response.
    # Stamped once on the first non-pending transition, never overwritten.
    response_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    @field_validator("ghost_signals", mode="before")
    @classmethod
    def _decode_ghost_signals(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except (TypeError, ValueError):
                return None
            return parsed if isinstance(parsed, list) else None
        return v


class ApplicationStatusIn(BaseModel):
    application_status: str  # pending|interview|offer|rejected|no_response


# ── Search ──────────────────────────────────────────────────────────────────
class SearchStatusOut(ORMModel):
    id: int
    profile_id: int
    status: str
    message: Optional[str] = None
    warning: Optional[str] = None
    # Optional despite the model column defaulting to 0: a row not created
    # through the ORM's normal insert path (e.g. a hand-edited/migrated row)
    # can still have NULL here, and this endpoint 500ing while a run is
    # "running" silently kills the whole progressive-paint display for that
    # profile (the frontend's status poll never resolves to "running").
    result_count: Optional[int] = None
    started_at: datetime
    finished_at: Optional[datetime] = None


class SearchStartOut(BaseModel):
    run_id: int
    status: str
    searches_remaining: int


# ── Dashboard stats ─────────────────────────────────────────────────────────
class StatsOut(BaseModel):
    searched: int
    saved: int
    applied: int
