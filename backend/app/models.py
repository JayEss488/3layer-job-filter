"""ORM models mirroring implementation_notes.md section 2.

Every user-data table carries user_id (directly, or via the profiles join) so
multi-user auth is a drop-in later. JSON-in-a-column memory is replaced by the
normalised profile_attributes table."""
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .config import CURRENT_USER_ID, DEFAULT_WEIGHT
from .database import Base


def _now() -> datetime:
    return datetime.utcnow()


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, default=CURRENT_USER_ID, index=True)
    name = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    attributes = relationship(
        "ProfileAttribute", back_populates="profile", cascade="all, delete-orphan"
    )
    roles = relationship(
        "Role", back_populates="profile", cascade="all, delete-orphan"
    )


class ProfileAttribute(Base):
    """The core memory table. One editable element of a profile per row."""

    __tablename__ = "profile_attributes"

    id = Column(Integer, primary_key=True)
    profile_id = Column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type = Column(Text, nullable=False)  # controlled vocab, see config.ATTRIBUTE_TYPES
    value = Column(Text, nullable=False)
    weight = Column(Float, nullable=False, default=DEFAULT_WEIGHT)
    source = Column(Text, nullable=False)  # cv_parsed|text_parsed|user_added|engine_inferred
    confirmed = Column(Boolean, default=False)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    profile = relationship("Profile", back_populates="attributes")


class Role(Base):
    """Every role the engine has fetched, with its lifecycle state per profile."""

    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    profile_id = Column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_id = Column(Text)  # dedupe key from source
    title = Column(Text, nullable=False)
    company = Column(Text)
    location = Column(Text)
    url = Column(Text)
    tags = Column(JSON)  # ["React","Python","Senior"] - display only
    salary_text = Column(Text)
    fit_rank = Column(Integer)  # 1..N within a search batch
    ai_analysis = Column(Text)  # the expensive-AI justification
    status = Column(Text, nullable=False, default="new")
    # new|saved|crossed|ignored|applied|deleted
    application_status = Column(Text)  # pending|interview|rejected (null until applied)
    applied_at = Column(DateTime)
    deadline = Column(DateTime)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    profile = relationship("Profile", back_populates="roles")


class FeedbackLog(Base):
    """Append-only audit trail. Never update or delete rows here."""

    __tablename__ = "feedback_log"

    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)
    action = Column(Text, nullable=False)  # tick|cross|ignore|apply
    created_at = Column(DateTime, default=_now)


class JobSeen(Base):
    """Persistent discovery store: every listing ever discovered for a profile,
    with a stable identity and an enrichment state. Discovery upserts here;
    enrichment only ever touches state='new' (plus backlog top-up when thin)."""

    __tablename__ = "jobs_seen"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, default=CURRENT_USER_ID, index=True)
    profile_id = Column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identity_hash = Column(Text, nullable=False, index=True)  # stable cross-source key
    source = Column(Text, nullable=False)                     # board name
    title = Column(Text, nullable=False)
    company = Column(Text)
    location = Column(Text)
    url = Column(Text)
    snippet = Column(Text)             # doubles as evaluation text (see 4.3)
    embedding = Column(Text)           # JSON-encoded vector, cached once per job
    state = Column(Text, nullable=False, default="new")       # new|enriched|shown
    source_updated_at = Column(DateTime)                      # ATS updated_at when present
    first_seen = Column(DateTime, default=_now)
    last_seen = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("profile_id", "identity_hash", name="uq_jobseen_profile_identity"),
    )


class CompanyATS(Base):
    """Bootstrapped ATS tokens (company -> vendor + board token). Maintained by an
    occasional SerpAPI harvest, read every run by select_ats_batch_for_run."""

    __tablename__ = "company_ats"

    id = Column(Integer, primary_key=True)
    company = Column(Text, nullable=False)
    vendor = Column(Text, nullable=False)  # greenhouse|lever|ashby
    token = Column(Text, nullable=False)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("vendor", "token", name="uq_companyats_vendor_token"),
    )


class SearchRun(Base):
    """One row per kicked-off search. Drives status polling + the daily cap."""

    __tablename__ = "search_runs"

    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    status = Column(Text, nullable=False, default="running")  # running|done|error
    message = Column(Text)  # progress / warning / error text
    warning = Column(Text)  # harsh-filter warning surfaced to the user
    result_count = Column(Integer, default=0)
    started_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime)
