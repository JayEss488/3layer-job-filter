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


class User(Base):
    """A login account. Added for the closed beta: credentials are hand-assigned
    (see scripts/gen_beta_users.py), not self-service. `User.id` IS the `user_id`
    every other table already carries, so authenticating simply makes
    deps.current_user_id() return this id instead of the old hardcoded constant --
    no other table changed. Passwords are stored as a pbkdf2-sha256 hash + per-user
    salt (see services/auth.py); the plaintext is never persisted."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(Text, nullable=False, unique=True, index=True)
    password_hash = Column(Text, nullable=False)  # pbkdf2_hmac(sha256) hex digest
    salt = Column(Text, nullable=False)            # per-user hex salt
    created_at = Column(DateTime, default=_now)


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
    # active|inactive -- whether this stream runs in the pipeline at all. An
    # inactive family gets no cluster (see snapshot._role_groups): no
    # discovery, embedding, gate, rank, or judge calls for it, full stop.
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
    # The cheap rank_gate's 0-100 fit estimate, written when this row is first
    # persisted provisionally (mid-run, before the expensive judge). Kept after
    # finalization but only rendered while `provisional` is true.
    rank_score = Column(Integer)
    # True only in the window between the gate+rank phase persisting this row
    # and the run finishing: the row is a "being verified" placeholder that the
    # final judge either upgrades in place or removes. Every non-/search
    # consumer filters these out (see routers/search.py::list_roles).
    provisional = Column(Boolean, nullable=False, default=False)
    # Which pipeline stage a still-provisional row was painted by, so /search can
    # bucket the three progressive-paint sections. "embed" = surfaced straight off
    # the cosine pre-filter, before any LLM has looked at it (no rank_score, no
    # analysis); "rank" = survived the cheap gate and carries rank_gate's 0-100
    # estimate. A row is only ever promoted embed -> rank -> final IN PLACE
    # (matched by external_id within the run), which is what stops the same job
    # appearing in two sections at once.
    # Survives finalization in ONE case: `provisional_stage="rank"` together with
    # `provisional=False` means a row the cheap stages scored but the expensive
    # judge never got to, retained on screen under its own heading rather than
    # deleted (engine._retain_unreviewed_provisional). A genuine judged pick has
    # provisional False and this NULL.
    provisional_stage = Column(Text)
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


class EventLog(Base):
    """Append-only usage-analytics stream for the beta (see services/analytics.py).

    Carries user_id directly (FeedbackLog/SearchRun only have profile_id) so
    per-user activity rolls up without a join. `event_type` is a short slug
    (login|search_started|role_tick|role_cross|role_ignore|role_apply|
    profile_created|...); `payload` is optional free-form JSON for extra context.
    Read only by the owner-only GET /admin/analytics endpoint."""

    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, index=True)
    profile_id = Column(Integer, nullable=True, index=True)
    event_type = Column(Text, nullable=False, index=True)
    payload = Column(Text, nullable=True)  # JSON-encoded when present
    created_at = Column(DateTime, default=_now, index=True)


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
    # When the EMPLOYER posted / closes the listing, as stated by the source --
    # distinct from source_updated_at (a change-detection key for the re-queue
    # rule) and from first_seen (when WE happened to discover it, which for a
    # months-old listing discovered today says nothing about its age). Nullable
    # everywhere and often null: a source that states no date must leave the age
    # unknown rather than have one inferred, since every consumer treats
    # "unknown" as "no penalty". See full_auto.py's posting-date normalisation.
    posted_at = Column(DateTime)
    expires_at = Column(DateTime)
    # True when posted_at was ALIASED from the source's updated/published field
    # rather than being a real "first posted" date -- Greenhouse, Workable,
    # Recruitee and Ashby all supply only the former (see full_auto.fetch_ats).
    # Kept rather than discarded, because an ad nobody has touched in four months
    # is a stronger ghost signal than one posted four months ago and edited last
    # week -- but it must not be RENDERED as "posted N ago", which is a claim the
    # source never made. Lever's createdAt is a genuine creation date and is not
    # marked. NULL = unknown provenance (rows predating this column).
    posted_at_approx = Column(Boolean)
    # Distinct UTC days on which discovery has re-observed this identity. The one
    # age signal a source cannot launder by re-listing: an aggregator can reset
    # posted_at on every re-syndication, but it cannot change how long WE have
    # been seeing the ad. Counts DAYS, not runs -- MAX_SEARCHES_PER_DAY allows 6
    # runs a day, and a run counter would measure how often the user searches
    # rather than how long the employer has been advertising.
    #
    # A LOWER BOUND, and only ever that: discovery returns a listing only when
    # this run's search terms happen to surface it, so a gap means "not seen",
    # never "not live". Must never be phrased to any model as "open for N days".
    # NULL = discovered before this column existed; coalesce to 1 and never let
    # NULL suppress anything.
    seen_days = Column(Integer)
    # Cross-run reuse of the two most expensive artifacts, so a job that resurfaces
    # (backlog top-up, re-queue) skips re-scraping and re-judging. Cleared when a
    # source-updated row is re-queued so a changed posting is re-scraped/re-judged.
    full_text = Column(Text)           # scraped page text, persisted so no re-scrape
    eval_verdict = Column(Text)        # strong|backup|reject (final-AI decision)
    eval_analysis = Column(Text)       # JSON: summary/filters_on/highlight/concerns
    eval_signature = Column(Text)      # profile signature at eval time (validity key)
    evaluated_at = Column(DateTime)
    # The gate-side twin of eval_signature: full_auto._profile_signature at the time
    # this row was retired to state='enriched' by the CHEAP gate (dropped before the
    # judge ever saw it). Retirement used to be a bare state flip with no signature,
    # i.e. permanent and profile-independent -- which quietly made it the single
    # biggest constraint on how many roles a run could find. Every gate-dropped row
    # left the candidate pool forever, and since the gate examines the highest-scoring
    # rows first, what it removed was disproportionately the top of the store: a
    # measured live store held 882 retired rows of which 838 cleared the relevance
    # floor, against 274 in the whole remaining pool. Scoping it lets a profile edit
    # re-open exactly the rows whose gate verdict that edit invalidated -- matching how
    # eval_signature already works for the judge -- while an unchanged profile still
    # never re-gates the same row twice. NULL = retired before this existed.
    gate_signature = Column(Text)
    # Set only on a HIGH-CONFIDENCE dead/expired-listing signal from Phase 5
    # scraping (status_404/status_410/expired_phrase/generic_hub -- the last
    # being a redirect to the employer's general careers page instead of this
    # job's own, see full_auto.py's _dead_listing_signal) with no alternate
    # posting found. Deliberately its
    # own field, not a `state` value or `eval_verdict='reject'`: it's a fact
    # about the URL, independent of pipeline-progress state and of the
    # profile/CV (must survive an eval_signature change, unlike a real verdict).
    dead_reason = Column(Text)
    first_seen = Column(DateTime, default=_now)
    last_seen = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("profile_id", "identity_hash", name="uq_jobseen_profile_identity"),
    )


class JobEmbedding(Base):
    """Global, content-addressed cache of job embedding vectors.

    Deliberately NOT scoped by user_id/profile_id, unlike every other table
    here: a job's embedding text is purely `title + company + snippet[:2000]`
    (see engine._embed_text) -- zero profile data -- so the same job yields an
    identical vector for every candidate. Caching it per-profile on JobSeen.embedding
    meant every new profile that discovered the same job re-embedded it from
    scratch (a ~90s tax on a first international run, paid again per user). This
    store lets any profile reuse a vector computed once, ever.

    Keyed by sha1(EMBED_MODEL + "\\n" + embed_text): folding the model name into
    the hash means a future embedding-model switch transparently recomputes
    under fresh keys instead of serving stale vectors. `embedding` is the same
    base64-float32 encoding JobSeen.embedding uses (engine._encode_embedding),
    so a cache hit is copied straight across with no re-encode."""

    __tablename__ = "job_embeddings"

    text_hash = Column(Text, primary_key=True)
    embedding = Column(Text, nullable=False)  # base64 float32, see engine._encode_embedding
    model = Column(Text)                      # EMBED_MODEL at compute time (informational)
    created_at = Column(DateTime, default=_now)


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
