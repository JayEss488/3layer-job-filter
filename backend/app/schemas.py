"""Pydantic request/response models."""
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ── Profiles ────────────────────────────────────────────────────────────────
class ProfileCreate(BaseModel):
    name: Optional[str] = None


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    is_active: Optional[bool] = None


class ProfileOut(ORMModel):
    id: int
    name: str
    is_active: bool
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


class AttributeUpdate(BaseModel):
    value: Optional[str] = None
    confirmed: Optional[bool] = None
    weight: Optional[float] = None
    proficiency: Optional[str] = None


class AttributeOut(ORMModel):
    id: int
    profile_id: int
    type: str
    value: str
    weight: float
    source: str
    confirmed: bool
    proficiency: Optional[str] = None


# ── Onboarding / parsing ────────────────────────────────────────────────────
class ParseTextIn(BaseModel):
    text: str


class SuggestIn(BaseModel):
    type: str
    context: Optional[str] = None


class SuggestOut(BaseModel):
    suggestions: list[str]


class ConfidenceOut(BaseModel):
    score: int
    missing: list[str]
    tip: str


# ── Roles ───────────────────────────────────────────────────────────────────
class RoleOut(ORMModel):
    id: int
    profile_id: int
    external_id: Optional[str] = None
    title: str
    company: Optional[str] = None
    location: Optional[str] = None
    url: Optional[str] = None
    tags: Optional[Any] = None
    salary_text: Optional[str] = None
    source: Optional[str] = None
    fit_rank: Optional[int] = None
    ai_analysis: Optional[str] = None
    status: str
    application_status: Optional[str] = None
    applied_at: Optional[datetime] = None
    created_at: datetime


class ApplicationStatusIn(BaseModel):
    application_status: str  # pending|interview|rejected


# ── Search ──────────────────────────────────────────────────────────────────
class SearchStatusOut(ORMModel):
    id: int
    profile_id: int
    status: str
    message: Optional[str] = None
    warning: Optional[str] = None
    result_count: int
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
