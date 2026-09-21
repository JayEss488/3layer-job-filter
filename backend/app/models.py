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


# There is no User table. This app runs as a single local user (see
# config.LOCAL_USER_ID): every table below still carries `user_id`, defaulted to
# that constant, so the multi-user seam is preserved without an account system
# to go with it. A login would protect nothing here -- anyone who can reach the
# port can already read the SQLite file beside it.


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
    # What the source/judge said about pay, verbatim. Kept as the display
    # fallback and as the audit trail for the parsed columns below -- the
    # formats are genuinely chaotic ("£30,000 (rising to £45,000)", "up to 70k",
    # "£32,000 - £35,000 per annum, inc benefits") and a parse that comes back
    # None must still be able to show the candidate what the employer wrote.
    salary_text = Column(Text)
    # salary_text (or the board's own structured figures) parsed into comparable
    # numbers by services/salary.py, in salary_period's units rather than
    # annualised. All four are null together when nothing was parseable, which
    # is the common case. See engine._role_salary_fields.
    salary_min = Column(Float)
    salary_max = Column(Float)
    salary_period = Column(Text)       # year|month|week|day|hour
    salary_currency = Column(Text)     # ISO code (GBP/USD/...), null when unstated
    # True when salary_min/max is a MODELLED estimate (currently only Adzuna's
    # salary_is_predicted) rather than a figure the employer/board actually
    # stated. Never null-safe to skip: the card must label an estimate rather
    # than show it as fact, and the salary-floor filters must never hard-drop a
    # candidate on one. See engine._role_salary_fields.
    salary_is_predicted = Column(Boolean)
    source = Column(Text)  # board this role was discovered on (see JobSeen.source)
    # Copied straight from JobSeen.posted_at/expires_at/posted_at_approx at every
    # Role-creation/upgrade site (see engine._role_date_fields) -- a display gap,
    # not a data gap: the pipeline has carried these since the listing-age work,
    # but only JobSeen ever exposed them, so /search and /my-roles (which render
    # Role, never JobSeen) had no age to show regardless of what the source gave.
    # Nullable and often null, same as on JobSeen -- an unknown date must render
    # as no chip, never a guessed one. posted_at_approx mirrors JobSeen's own
    # flag so a Greenhouse-aliased updated_at is never rendered as "posted".
    posted_at = Column(DateTime)
    expires_at = Column(DateTime)
    posted_at_approx = Column(Boolean)
    # Straight-line miles from the candidate's stated place to this listing's,
    # computed once per run from ONS postcode centroids (services/geo.py) and
    # copied here by engine._role_location_fields. NULL whenever either side
    # couldn't be resolved -- which is the common case, and must render as no
    # chip rather than as 0. Not a drive time and not accurate below a few
    # miles: outcode-centroid precision, see geo.py.
    distance_miles = Column(Integer)
    # A human-readable version of `location` when the source gave a raw postcode
    # ("B706AW" -> "Sandwell"). NULL when `location` needs no fixing, which is
    # most rows -- the card falls back to `location` itself. Kept alongside
    # rather than overwriting `location`, so what the board actually said is
    # never lost.
    location_label = Column(Text)
    # Whether this listing's employer is on the Home Office register of licensed
    # visa sponsors (services/sponsors.py). THREE-STATE and the NULL matters:
    # True/False mean we had a company name and checked it, NULL means the
    # listing named no employer to check -- so a blank-company aggregator row
    # never renders as a confirmed non-sponsor. Stamped on every run, not only
    # when the visa_sponsor_only filter is on.
    sponsor_licensed = Column(Boolean)
    # What THIS LISTING says about sponsoring THIS vacancy, in its own words:
    # "offered" | "not_offered" | NULL (the listing was silent, which is the
    # overwhelmingly common case -- 12,146 of 12,273 text-bearing store rows).
    #
    # A DIFFERENT QUESTION from sponsor_licensed above, and the reason this
    # column exists. The register answers "does this employer hold a licence"
    # and can never answer "will this vacancy be sponsored" -- 21 rows in the
    # measured store are a licensed employer whose advert states it will not
    # sponsor the role, and those used to carry a "Visa sponsor" badge and pass
    # the sponsors-only filter. Four rows are the reverse: the advert says
    # sponsorship is available for a company the register cannot resolve, which
    # is the agency/blank-company hole the register alone can never close.
    # Never inferred from silence in either direction.
    sponsor_statement = Column(Text)
    # The employer's own sentence, so the card can show the words rather than
    # ask the candidate to trust a badge. Capped at
    # sponsors.STATEMENT_QUOTE_MAX; NULL exactly when sponsor_statement is.
    sponsor_statement_quote = Column(Text)
    # When this exact listing was last confirmed to still exist, by a direct
    # fetch of its own URL (or, for an ATS row, by its continued presence in the
    # vendor feed). Every non-provisional row surfaced by a run carries one --
    # engine._verify_final_picks checks the picks before they are persisted. Its
    # twin on JobSeen is the cross-run store; this copy is what the card renders.
    last_verified_at = Column(DateTime)
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
    # Ghost-listing risk as assessed when this row was persisted: "high",
    # "medium", or NULL for "nothing fired". Deliberately three-valued with no
    # "low" -- a bottom value covering most rows would invite the card to render
    # it, and "Low ghost risk" on an ordinary listing is noise that trains the
    # user to ignore the chip. NULL is the overwhelmingly common case.
    #
    # Note the semantics are INVERTED from sponsor_licensed above: that field
    # badges only a positive because a False there means "not on the register",
    # which for an agency posting is not the same as "does not sponsor". Here
    # the badge is a NEGATIVE, so absence must read as "nothing fired" -- which
    # is only honest while every ghost rule fires on POSITIVE evidence and none
    # fires on missing data (a listing with no posted_at produces no signal, the
    # same rule _listing_age_tag follows: silence is not evidence of age). If a
    # rule is ever added that fires on absence, this field's meaning breaks and
    # so does the "none flagged" line on /search.
    ghost_level = Column(Text)
    # The named rules that fired, as a JSON list of slugs (see services/ghost.py).
    # Persisted ALONGSIDE the verdict, not derivable from it, because several
    # rules read state that is destroyed on write -- dead_at is stamped once and
    # never overwritten, seen_dates truncates at _SIGHTING_MAX_DAYS, and a
    # repost_key group's membership changes as rows arrive. A verdict re-derived
    # from a later store is not the same verdict. Same reasoning as gate_cache
    # storing the model's own score so a bonus can be retuned without
    # invalidating it.
    ghost_signals = Column(Text)
    status = Column(Text, nullable=False, default="new")
    # new|saved|crossed|ignored|applied|deleted
    application_status = Column(Text)
    # pending|interview|offer|rejected|no_response (null until applied).
    # `offer` exists so the terminal states are not uniformly negative -- a form
    # whose only outcomes are bad is a form people don't fill in. `no_response`
    # is NOT final: the interview/offer/rejected controls stay available so a
    # late reply can correct it.
    applied_at = Column(DateTime)
    # When the employer first responded, or when the user declared no response.
    # Distinct from updated_at, which moves on any edit. Stamped ONCE on the
    # first non-pending transition and never overwritten -- same rule as dead_at
    # and for the same reason: the interval applied_at -> response_at is the
    # measurement, and a later correction must not rewrite the history.
    #
    # Read as ground truth for ghost-listing calibration, with one caveat that
    # must travel with it: `no_response` is a BIASED label for ghosting. Most
    # applications get no response for entirely ordinary reasons. It is usable
    # only as a rate across many rows conditioned on a fired signal, never as
    # per-listing confirmation that a specific role was a ghost.
    response_at = Column(DateTime)
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
    # Normalised pay, in `salary_period`'s own units (services/salary.py) -- NOT
    # annualised, so a source's own figure is never silently rewritten. Nullable
    # and usually null: most listings state no salary, and "Competitive" must
    # stay unknown rather than become a number.
    #
    # These exist because the pipeline's candidates come from THIS table, not
    # from the fresh-discovery dicts: without them the structured salary every
    # board API returns was read once by the discovery-time filter and then
    # thrown away, so a listing resurfacing from the backlog reached the gates
    # with no pay information at all, and full_auto._listing_salary_suffix --
    # which feeds screen_gate's salary axis and rank_gate's HARD DOWNGRADE (e)
    # -- rendered empty for effectively every candidate ever gated.
    salary_min = Column(Float)
    salary_max = Column(Float)
    salary_period = Column(Text)       # year|month|week|day|hour
    salary_currency = Column(Text)     # ISO code (GBP/USD/...), null when unstated
    # True when salary_min/max is a MODELLED estimate (Adzuna's own
    # salary_is_predicted) rather than a figure the employer/board stated -- see
    # Role.salary_is_predicted, which this backfills onto every Role persisted
    # from this row.
    salary_is_predicted = Column(Boolean)
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
    # ── Ghost-listing evidence (recording only) ─────────────────────────────
    # These three exist to be WRITTEN now and analysed later. A "ghost" listing
    # -- one that stays up for months, or is taken down and reposted verbatim,
    # without a real vacancy behind it -- can only be identified from a history
    # of observations, and that history CANNOT BE RECONSTRUCTED AFTER THE FACT.
    # Every week these aren't recorded is permanently lost, while the scoring
    # and the UI that read them can be built whenever. Nothing in the pipeline
    # currently reads any of them, and that is the intended state.
    #
    # The distinct UTC dates this identity has been observed, as days-since-
    # epoch integers, comma-separated and ascending ("20304,20305,20309").
    # seen_days above is the COUNT of exactly these and stays the fast path;
    # this is the shape of the observation window, which the count destroys.
    # A gap means "not seen on that day", never "not live" -- discovery only
    # returns a listing when a run's search terms happen to surface it, so this
    # is a lower bound on how long the ad ran, in the same way seen_days is.
    # Text rather than a sightings TABLE on purpose: a row per observation would
    # be ~3,000 inserts per run (6 runs a day are allowed) for data whose whole
    # value is longitudinal, where this is a few hundred bytes on a row that is
    # already being written.
    seen_dates = Column(Text)
    # When we FIRST confirmed this listing was gone -- the closing bracket
    # around an ad's life that first_seen opens. dead_reason records that it
    # died and why; without a timestamp there is no way to ask how long any
    # listing actually stayed up, which is the central ghost-listing question.
    # Stamped once, never overwritten.
    dead_at = Column(DateTime)
    # Normalised company+title, shared by every listing that is arguably the
    # same vacancy re-advertised. NOT a dedupe key (identity_hash is that, and
    # these rows are deliberately kept separate): a repost is a distinct listing
    # with its own dates, and the signal is precisely how MANY of them there
    # are and how far apart. A live store already shows one recruiter's
    # ".NET Developer" 34 times. Written at discovery, read by nothing yet.
    repost_key = Column(Text, index=True)
    # Normalised "<company>|<title>" (engine._soft_dup_key), written at discovery
    # so the soft-duplicate lookup in _upsert_discovered is an indexed equality
    # instead of a `lower(company) LIKE x%` pre-filter that hydrated ~818 full
    # rows per call -- see database._migrate_soft_dup_key for the measurements
    # and for why this is also MORE correct than the SQL it replaced.
    #
    # Deliberately distinct from repost_key above, which is the same shape but a
    # different question: repost_key groups re-advertisements of one vacancy for
    # later ghost-listing analysis and is read by nothing, while this one decides,
    # at write time, whether an incoming listing IS a row we already hold. Keeping
    # them separate means a change to either normalisation can't silently move the
    # other's meaning. The index is created in _migrate_soft_dup_key rather than
    # with index=True here because the same migration has to backfill existing
    # rows anyway, and a NULL key must never be treated as "no duplicate".
    soft_dup_key = Column(Text)
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
    # The last time this SPECIFIC listing's liveness was directly confirmed --
    # a Phase-5 scrape, or a Reed/Adzuna per-job detail fetch (whether it found
    # the listing alive or dead) -- as opposed to first_seen/last_seen, which
    # track discovery re-observing the identity via a board's SEARCH api and
    # say nothing about whether anyone has looked at the listing's own page
    # since. NULL = never directly verified (discovery-only, e.g. every ATS
    # row, or a Reed/Adzuna row before this column existed). Read by
    # engine._enrich_pre_gate's revalidate pass: a judge-pool candidate whose
    # listing hasn't been reverified in LISTING_REVALIDATE_AFTER_DAYS gets one
    # more cheap detail-endpoint check before the judge trusts its cached text.
    last_verified_at = Column(DateTime)
    # The board's OWN id for this listing, as "<vendor>:<id>" (full_auto._board_ref),
    # parsed back out of the URL because no fetcher stores it. Its job is
    # observation CONTINUITY, not deduplication: identity_hash is
    # sha1(_canonical_url(url)), so an aggregator adding a tracking parameter or
    # Reed changing a slug mints a NEW identity and silently resets first_seen to
    # zero -- an ongoing, undetected corruption of the exact data every
    # longitudinal ghost rule depends on. ListingObservation looks this up before
    # falling back to identity_hash so a URL-churned listing continues its window.
    #
    # Read the vendor column carefully before trusting it as a vacancy key: a
    # reed/adzuna id identifies a LISTING (a repost gets a new number), while a
    # greenhouse gh_jid identifies the employer's own REQUISITION and persists
    # for as long as the req is open -- the closest thing in this codebase to
    # ground truth about whether one vacancy is still the same vacancy.
    # NULL for recruitee/careerjet/google_jobs, which expose no usable id.
    source_ref = Column(Text)
    # Whether this listing's `company` is a recruitment agency rather than the
    # employer. Three-state: NULL when there is no company name to judge.
    # Load-bearing for ghost scoring because agency reposting is routine
    # business, not ghosting -- the repost-family rules are suppressed when this
    # is true, while the age rules are NOT (an ad up for a year with no vacancy
    # behind it is the candidate's problem whoever posted it). Never rendered as
    # a negative on a card: the judge's own WISH-LIST rule treats agency-posted
    # as a reason to be MORE generous.
    agency_flag = Column(Boolean)
    # The ghost rules that fired for this listing, JSON list of slugs. The
    # longitudinal record; Role.ghost_signals is the snapshot the card renders.
    ghost_signals = Column(Text)
    ghost_evaluated_at = Column(DateTime)
    first_seen = Column(DateTime, default=_now)
    last_seen = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("profile_id", "identity_hash", name="uq_jobseen_profile_identity"),
    )


class ListingObservation(Base):
    """Global, profile-independent sighting history for one listing.

    Deliberately NOT scoped by user_id/profile_id, for the same reason
    JobEmbedding isn't: when a listing was seen is a fact about the listing, not
    about whoever happened to search for it. Two concrete forces made this a
    separate table rather than more columns on JobSeen:

      * engine._store_age_days is PER-PROFILE, and it gates every observation
        rule (OBSERVATION_MIN_STORE_DAYS). Without a global clock, a user who
        signs up next month is silently blind to every longitudinal ghost signal
        for their first 30 days, with no error anywhere to notice.
      * The scheduled observation crawl (services/observe.py) has no profile at
        all. JobSeen is UniqueConstraint(profile_id, identity_hash) on a
        non-nullable FK, so a profile-free writer would need a sentinel profile
        row, which would then pollute every profile-scoped query in the pipeline.

    What it is NOT justified by: unifying history fragmented across profiles.
    Measured on the live store, all 9,998 identity hashes appeared under exactly
    one profile -- there was nothing to unify, and that argument does not survive
    contact with the data.

    ONE ROW PER LISTING, not one per sighting: `seen_dates` uses the identical
    comma-separated days-since-epoch encoding as JobSeen.seen_dates and is
    appended by the same engine._append_sighting, for the reason recorded there
    (a row per observation is thousands of inserts per pass for data whose whole
    value is longitudinal).

    JobSeen.seen_dates stays exactly as it was and keeps being written. This
    table is additive: nothing in the search path reads it yet, so if it turns
    out to be the wrong shape it can be dropped without touching the pipeline."""

    __tablename__ = "listing_observations"

    identity_hash = Column(Text, primary_key=True)
    # Preferred continuity key -- see JobSeen.source_ref. Indexed because the
    # upsert looks up (source, source_ref) BEFORE falling back to the PK.
    source_ref = Column(Text, index=True)
    source = Column(Text)
    company = Column(Text)
    title = Column(Text)
    url = Column(Text)
    # Normalised company+title (engine._repost_key), grouping re-advertisements
    # of arguably the same vacancy. Indexed here from the start, unlike its twin
    # on JobSeen -- see database._migrate_ghost_indexes for why that one's
    # index=True never actually took effect.
    repost_key = Column(Text, index=True)
    posted_at = Column(DateTime)
    posted_at_approx = Column(Boolean)
    expires_at = Column(DateTime)
    first_seen = Column(DateTime, default=_now)
    last_seen = Column(DateTime, default=_now, onupdate=_now)
    seen_dates = Column(Text)
    # Closing bracket on the listing's life. Stamped once, never overwritten.
    # This is the column the whole feature is waiting on: without death
    # timestamps there is no way to ask how long a listing actually stays up,
    # and takedown-then-repost cannot be detected at all.
    dead_at = Column(DateTime)
    dead_reason = Column(Text)
    last_verified_at = Column(DateTime)


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
    vendor = Column(Text, nullable=False)  # see full_auto.ATS_FEEDS for the live set
    token = Column(Text, nullable=False)
    keyword = Column(Text)  # harvest phrase that found it ("curated" for the seed,
                            # "charity:*" for a direct_employer.py crawl hit)
    created_at = Column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("vendor", "token", name="uq_companyats_vendor_token"),
    )


class DirectEmployerProbe(Base):
    """One row per employer domain the direct-employer crawl has looked at.

    The SEED list is not stored here -- it lives in the generated
    `uk_charity_gen` module, which stays the source of truth for who exists.
    This table records only what a probe FOUND, so it grows with work actually
    done and doubles as the crawl's cursor: a domain with a row inside the
    recheck window is skipped.

    Keeping the misses (`no_ats`, `unreachable`) matters as much as the hits.
    Without them the crawl has no way to distinguish "not yet looked at" from
    "looked at, nothing there", and would re-spend its whole budget on the same
    dead domains on every pass. They are also the only way to measure the
    strategy: the hit RATE is what says whether this vertical is worth the
    crawl, and a table of hits alone silently reports 100%."""

    __tablename__ = "direct_employer_probes"

    id = Column(Integer, primary_key=True)
    domain = Column(Text, nullable=False, unique=True, index=True)
    company = Column(Text)
    source_list = Column(Text)   # which seed list it came from, e.g. "uk_charity"
    status = Column(Text, nullable=False)  # ats_found|no_ats|unreachable|blocked_by_robots
    vendor = Column(Text)        # set when status == "ats_found"
    token = Column(Text)
    careers_url = Column(Text)   # the page the board link was found on
    note = Column(Text)          # short failure detail, for triage
    probed_at = Column(DateTime, default=_now, index=True)


class ListingHostStat(Base):
    """Rolling per-host tally of liveness-check outcomes (see
    engine._verify_listings_alive).

    OBSERVABILITY ONLY -- deliberately not wired to any automatic drop, and no
    code reads it to make a decision. It exists because the opposite design was
    tried and was wrong: an apparent 100% dead rate for two mirror hosts turned
    out to be an artefact of sampling old STORE rows, i.e. it measured listing
    age rather than host health, and re-measuring against live URLs showed those
    same hosts serving perfectly good postings. Deadness is a property of the
    LISTING, so that is the only level acted on.

    What this is for is the one thing per-run funnel counts can't show: a
    platform genuinely degrading over time (an `unverifiable` share climbing
    toward 100% would mean we can no longer check that host at all)."""

    __tablename__ = "listing_host_stats"

    id = Column(Integer, primary_key=True)
    host = Column(Text, nullable=False, unique=True, index=True)
    checked = Column(Integer, nullable=False, default=0)
    dead = Column(Integer, nullable=False, default=0)
    unverifiable = Column(Integer, nullable=False, default=0)
    updated_at = Column(DateTime, default=_now)


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
