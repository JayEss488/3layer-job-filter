"""Pydantic request/response models."""
import json
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Profiles ────────────────────────────────────────────────────────────────
class ProfileCreate(BaseModel):
    name: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None
    intent_text: Optional[str] = None
    search_feedback: Optional[str] = None
    cv_summary: Optional[str] = None


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
