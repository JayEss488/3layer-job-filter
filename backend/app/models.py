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

from .config import CURRENT_USER_ID, DEFAULT_WEIGHT, FAMILY_TIER_DEFAULT
from .database import Base


def _now() -> datetime:
    return datetime.utcnow()


class Profile(Base):
    __tablename__ = "profiles"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, default=CURRENT_USER_ID, index=True)
    name = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    cv_text = Column(Text)  # raw uploaded/pasted document text, for target-role regeneration
    cv_summary = Column(Text)  # LLM-compressed cv_text; extra judge context, see snapshot.py
    intent_text = Column(Text)  # free-text "what I'm looking for"; feeds the final judge and target-role regeneration
    # Free-text feedback on recent search RESULTS ("too senior", "stop showing sales
    # roles"), edited from the /search page itself. Distinct from intent_text (which
    # drives target-role generation): this just feeds the final judge as extra
    # context on the next run -- see snapshot.build_snapshot.
    search_feedback = Column(Text)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    attributes = relationship(
        "ProfileAttribute", back_populates="profile", cascade="all, delete-orphan"
    )
    roles = relationship(
        "Role", back_populates="profile", cascade="all, delete-orphan"
    )
    families = relationship(
        "RoleFamily", back_populates="profile", cascade="all, delete-orphan"
    )


class RoleFamily(Base):
    """One user-editable stream of target roles -- and the engine's cluster unit.

    The pipeline scores, gates, and judges each family independently, so a
    candidate targeting two unrelated fields is judged fairly on each rather
    than against a blend of both (see CLAUDE.md's search-pipeline section).
    This table is what made those clusters user-editable and stable: they used
    to be re-derived by an LLM call every run (snapshot.cluster_target_roles),
    which now only ever runs to SEED families from a fresh CV.

    A family owns its target_role rows via ProfileAttribute.family_id. Deleting
    a family deletes its target_role rows too, not just the family -- see
    routers/families.py::delete_family. Orphaning them instead reads as safer
    but isn't: the card is the only place a target role renders, so an orphan
    would be invisible while still driving discovery, and the next
    ensure_families call would silently re-seed it into a brand-new card."""

    __tablename__ = "role_families"

    id = Column(Integer, primary_key=True)
    profile_id = Column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = Column(Text, nullable=False)
    # core|secondary -- the candidate's declared priority for this stream. Feeds
    # an emphasis multiplier in snapshot._weighted_text (config.FAMILY_TIER_MULT)
    # and orders which families survive the MAX_ROLE_CLUSTERS cap.
    tier = Column(Text, nullable=False, default=FAMILY_TIER_DEFAULT)
    position = Column(Integer, nullable=False, default=0)  # display order
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    profile = relationship("Profile", back_populates="families")


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
    source = Column(Text, nullable=False)  # cv_parsed|text_parsed|user_added|engine_inferred|ai_suggested|feedback_derived
    confirmed = Column(Boolean, default=False)
    # Depth/duration signal for skill|past_role, e.g. "expert, 5+ years" or
    # "one-off, one week". Free text, set by CV parsing or edited by the user;
    # only meaningful for skill/past_role rows, null everywhere else.
    proficiency = Column(Text, nullable=True)
    # Where a skill's depth was earned -- Commercial|Self-directed|Academic|
    # AI-assisted (see config.EVIDENCE_ORIGIN_CHOICES). Orthogonal to
    # proficiency (which grades depth, not origin): a skill can be a
    # long-practiced but still unpaid/self-directed one. Only meaningful for
    # skill rows -- past_role already has its own paid/unpaid signal via
    # proficiency=="Informal", null everywhere else.
    evidence_origin = Column(Text, nullable=True)
    # Which RoleFamily this target_role belongs to. Only meaningful for
    # target_role rows; null everywhere else, and null on a target_role means
    # "not yet grouped" -- families.ensure_families seeds those into a family on
    # first sight (and snapshot falls back to in-memory LLM clustering if it
    # somehow still sees ungrouped roles at search time, so a run can never fail
    # for want of a family row).
    family_id = Column(Integer, ForeignKey("role_families.id", ondelete="SET NULL"),
                       nullable=True, index=True)
    # The candidate's "this one especially" pin within its family. Only
    # meaningful for target_role rows. Deliberately separate from `weight`:
    # weight is what tick/cross feedback LEARNED, pinned is what the candidate
    # DECLARED, and snapshot._weighted_text multiplies the two rather than
    # letting either overwrite the other.
    pinned = Column(Boolean, nullable=False, default=False)
    # hard|soft -- see config.enforcement_for for which types this applies to
    # and what each type defaults to when null (which every pre-existing row is).
    # Null is meaningful: it means "never set, use the type's default", so
    # reading this must always go through config.enforcement_for.
    enforcement = Column(Text, nullable=True)
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
    # Which SearchRun produced this row. Nullable: rows created before this
    # column existed have no run to point to. Lets the frontend separate "this
    # run"'s inbox from a still-unreviewed ('new') role left over from an
    # earlier run, instead of interleaving every past run's picks by fit_rank.
    search_run_id = Column(Integer, ForeignKey("search_runs.id"), nullable=True, index=True)
    title = Column(Text, nullable=False)
    company = Column(Text)
    location = Column(Text)
    url = Column(Text)
    tags = Column(JSON)  # ["React","Python","Senior"] - display only
    salary_text = Column(Text)
    source = Column(Text)  # board this role was discovered on (see JobSeen.source)
    fit_rank = Column(Integer)  # 1..N within a search batch
    ai_analysis = Column(Text)  # the expensive-AI justification
    # very_strong|strong|ok|stretch -- the final judge's own verdict, a finer
    # grade than the strong/backup list it landed in (see full_auto's
    # _FINAL_EVAL_SCHEMA). Leads the result card. Null on rows judged before
    # FINAL_EVAL_PROMPT_VERSION 8, and on an inconclusive-call fallback pick.
    verdict = Column(Text)
    # Facts the judge read out of the JD while it already had the full text in
    # hand, for the card to show instead of the generic skill tags: Remote/
    # Hybrid/On-site, the role's REAL seniority bar (not its label), and the
    # application deadline as stated. Free text, null when the listing is silent
    # -- a null renders as no chip rather than a guess. deadline_text is text
    # rather than a DateTime because the honest answer is often "rolling" or
    # "until filled". (An unused `deadline` DateTime column predates this and
    # was never read or written; it lingers in existing SQLite files.)
    work_style = Column(Text)
    seniority_level = Column(Text)
    deadline_text = Column(Text)
    status = Column(Text, nullable=False, default="new")
    # new|saved|crossed|ignored|applied|deleted
    application_status = Column(Text)  # pending|interview|rejected (null until applied)
    applied_at = Column(DateTime)
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
    # Cross-run reuse of the two most expensive artifacts, so a job that resurfaces
    # (backlog top-up, re-queue) skips re-scraping and re-judging. Cleared when a
    # source-updated row is re-queued so a changed posting is re-scraped/re-judged.
    full_text = Column(Text)           # scraped page text, persisted so no re-scrape
    eval_verdict = Column(Text)        # strong|backup|reject (final-AI decision)
    eval_analysis = Column(Text)       # JSON: summary/top_match_reason/concerns
    eval_signature = Column(Text)      # profile signature at eval time (validity key)
    evaluated_at = Column(DateTime)
    # Set only on a HIGH-CONFIDENCE dead/expired-listing signal from Phase 5
    # scraping (status_404/status_410/expired_phrase, see full_auto.py's
    # _dead_listing_signal) with no alternate posting found. Deliberately its
    # own field, not a `state` value or `eval_verdict='reject'`: it's a fact
    # about the URL, independent of pipeline-progress state and of the
    # profile/CV (must survive an eval_signature change, unlike a real verdict).
    dead_reason = Column(Text)
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
    vendor = Column(Text, nullable=False)  # greenhouse|lever|ashby|workable|recruitee|personio
    token = Column(Text, nullable=False)
    keyword = Column(Text)  # harvest phrase that found it ("curated" for the seed)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("vendor", "token", name="uq_companyats_vendor_token"),
    )


class SearchRun(Base):
    """One row per kicked-off search. Drives status polling + the daily cap."""

    __tablename__ = "search_runs"

    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, ForeignKey("profiles.id"), nullable=False, index=True)
    status = Column(Text, nullable=False, default="running")  # running|done|error|cancelled
    cancel_requested = Column(Boolean, default=False)  # set by POST /search/cancel; see
                                                        # engine.py's _check_cancelled
    message = Column(Text)  # progress / warning / error text
    warning = Column(Text)  # harsh-filter warning surfaced to the user
    result_count = Column(Integer, default=0)
    started_at = Column(DateTime, default=_now)
    finished_at = Column(DateTime)
    phase_timings = Column(Text)  # JSON-encoded {phase_name: seconds}, for perf analysis
    funnel_counts = Column(Text)  # JSON-encoded {stage_name: count}, for diagnosing thin results
    # JSON-encoded {stage_name: [{title, company, url}, ...]} -- a few sample roles
    # per pipeline stage, so a run can be inspected (and fed to an AI) to see which
    # stage is actually weak, not just how many rows it dropped. Sits alongside
    # funnel_counts rather than inside it: that stays ints/bools only.
    snapshot_samples = Column(Text)


class Setting(Base):
    """Generic key/value store for app settings that don't warrant their own
    table: per-source enable/disable toggles, last-run per-source counts, and the
    harvest keyword-hash marker. profile_id NULL means a global (per-user) setting."""

    __tablename__ = "settings"

    id = Column(Integer, primary_key=True)
    profile_id = Column(Integer, nullable=True, index=True)  # NULL = global
    key = Column(Text, nullable=False)
    value = Column(Text)  # free-form; JSON-encoded where structured
    updated_at = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("profile_id", "key", name="uq_settings_profile_key"),
    )
