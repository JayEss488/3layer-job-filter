"""SQLAlchemy engine + session. SQLite for the prototype; the same models work
against Postgres by changing DATABASE_URL only."""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# check_same_thread is a SQLite-only knob; harmless to gate on the URL scheme.
# timeout=30: the search background thread now commits mid-run (provisional
# roles) while tick/cross requests commit concurrently on the same rows --
# pysqlite's default 5s busy-timeout is thin for that. Mirrors
# full_auto.get_db()'s rationale for its own (separate) cache DB.
connect_args = (
    {"check_same_thread": False, "timeout": 30} if DATABASE_URL.startswith("sqlite") else {}
)

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


if DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _conn_record):
        """Put SQLite in WAL mode on every connection. A plain busy_timeout is
        NOT enough here: in the default rollback-journal mode a reader holding a
        SHARED lock while the search thread's frequent mid-run commits want an
        EXCLUSIVE lock is a genuine deadlock that SQLite fails *immediately*
        (SQLITE_BUSY / "database is locked"), never waiting out the timeout --
        which crashed a real UAE run mid-gate while the frontend polled
        /roles + /search/status every ~200ms. WAL lets readers read from a
        snapshot without ever blocking the single writer, so only writer-vs-
        writer serialises, and busy_timeout covers that. synchronous=NORMAL is
        the safe, faster pairing for WAL."""
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()


def get_db():
    """FastAPI dependency yielding a request-scoped session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """Create tables if they do not exist. Called on startup."""
    from . import models  # noqa: F401  (ensure models are registered)

    Base.metadata.create_all(bind=engine)
    _migrate_columns()
    _migrate_signup_indexes()
    _migrate_auth_provider()
    _migrate_family_tier_vocabulary()
    _migrate_soft_dup_key()
    _migrate_ghost_indexes()
    _migrate_dead_at_backfill()
    _migrate_listing_observations()
    _migrate_signup_feedback_backfill()


def _migrate_columns():
    """Lightweight additive migrations for SQLite (create_all won't add columns
    to an existing table). Each is idempotent: add the column only if missing."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    additions = {
        "jobs_seen": [
            ("embedding", "TEXT"),
            ("full_text", "TEXT"),
            ("eval_verdict", "TEXT"),
            ("eval_analysis", "TEXT"),
            ("eval_signature", "TEXT"),
            ("evaluated_at", "DATETIME"),
            ("dead_reason", "TEXT"),
            ("posted_at", "DATETIME"),
            ("expires_at", "DATETIME"),
            ("gate_signature", "TEXT"),
            ("posted_at_approx", "BOOLEAN"),
            ("seen_days", "INTEGER"),
            ("last_verified_at", "DATETIME"),
            ("salary_min", "FLOAT"), ("salary_max", "FLOAT"),
            ("salary_period", "TEXT"), ("salary_currency", "TEXT"),
            ("seen_dates", "TEXT"), ("dead_at", "DATETIME"), ("repost_key", "TEXT"),
            ("soft_dup_key", "TEXT"),
            ("source_ref", "TEXT"), ("agency_flag", "BOOLEAN"),
            ("ghost_signals", "TEXT"), ("ghost_evaluated_at", "DATETIME"),
        ],
        "company_ats": [("keyword", "TEXT")],
        "search_runs": [("phase_timings", "TEXT"), ("funnel_counts", "TEXT"), ("cancel_requested", "BOOLEAN"),
                         ("snapshot_samples", "TEXT")],
        "roles": [("source", "TEXT"), ("search_run_id", "INTEGER"), ("verdict", "TEXT"),
                   ("work_style", "TEXT"), ("seniority_level", "TEXT"), ("deadline_text", "TEXT"),
                   ("rank_score", "INTEGER"), ("provisional", "BOOLEAN DEFAULT 0"),
                   ("provisional_stage", "TEXT"), ("posted_at", "DATETIME"),
                   ("expires_at", "DATETIME"), ("posted_at_approx", "BOOLEAN"),
                   ("distance_miles", "INTEGER"), ("location_label", "TEXT"),
                   ("sponsor_licensed", "BOOLEAN"), ("last_verified_at", "DATETIME"),
                   ("salary_min", "FLOAT"), ("salary_max", "FLOAT"),
                   ("salary_period", "TEXT"), ("salary_currency", "TEXT"),
                   ("ghost_level", "TEXT"), ("ghost_signals", "TEXT"),
                   ("response_at", "DATETIME")],
        "profiles": [("cv_text", "TEXT"), ("cv_summary", "TEXT"), ("intent_text", "TEXT"),
                     ("search_feedback", "TEXT")],
        "profile_attributes": [("proficiency", "TEXT"), ("evidence_origin", "TEXT"),
                                ("family_id", "INTEGER"), ("pinned", "BOOLEAN DEFAULT 0"),
                                ("enforcement", "TEXT")],
        # Self-serve sign-up. Note google_sub/email declare index=True + unique on
        # the model, and ADD COLUMN cannot carry either -- see
        # _migrate_signup_indexes below, which is the same class of bug the
        # jobs_seen.repost_key note documents.
        # Self-serve sign-up + the open-beta window. beta_started_at is
        # deliberately left NULL by this migration: NULL means "no window", so
        # every pre-existing account is exempt from expiry and from the wrap-up
        # survey without any backfill. Do NOT "fix" that by stamping a date here
        # -- it would start a 7-day clock on the original beta testers.
        # auth_provider is deliberately left NULL for every pre-existing row:
        # NULL means "legacy hand-assigned account", which is exactly what those
        # rows are, and it is what exempts them from the sign-up survey. Google
        # accounts created before this column existed are backfilled from
        # google_sub by _migrate_auth_provider, which cannot guess wrong (only
        # the Google path has ever written that column).
        "users": [("google_sub", "TEXT"), ("apple_sub", "TEXT"),
                   ("auth_provider", "TEXT"), ("email", "TEXT"),
                   ("email_verified", "BOOLEAN"),
                   ("display_name", "TEXT"), ("last_login_at", "DATETIME"),
                   ("beta_started_at", "DATETIME")],
    }
    for table, cols in additions.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, coltype in cols:
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))


def _migrate_signup_feedback_backfill():
    """Mirror already-collected signup_surveys rows into feedback_responses.

    The feedback store is meant to hold all three surfaces so the admin readout
    is one query; without this, sign-up answers collected BEFORE the store
    existed would be the one surface missing from it, and the report would
    silently under-count the earliest (and currently only) respondents.

    signup_surveys remains the source of truth -- it is the survey GATE
    (auth_router._needs_survey reads it) and GET /admin/signups reads it
    directly. This is a read-side mirror, nothing depends on it for correctness.

    Idempotent: skips any user who already has a "signup" row, so it is a no-op
    on every boot after the first."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("signup_surveys") or not insp.has_table("feedback_responses"):
        return
    try:
        with engine.begin() as conn:
            done = {
                r[0] for r in conn.execute(
                    text("SELECT DISTINCT user_id FROM feedback_responses WHERE surface = 'signup'")
                )
            }
            rows = conn.execute(
                text("SELECT user_id, priority, used_ai_tool, created_at FROM signup_surveys")
            ).all()
            # Count what was actually INSERTED, not a difference between two set
            # sizes -- those only agree when every already-mirrored user still
            # has a signup_surveys row, and a log line that reports 0 while
            # writing rows is how a working migration gets distrusted later.
            mirrored = 0
            for user_id, priority, used_ai_tool, created_at in rows:
                if user_id in done:
                    continue
                for question_id, answer in (
                    ("signup_priority", priority or ""),
                    ("signup_used_ai_tool", "yes" if used_ai_tool else "no"),
                ):
                    conn.execute(
                        text(
                            "INSERT INTO feedback_responses "
                            "(user_id, profile_id, run_id, surface, question_id, answer, created_at) "
                            "VALUES (:u, NULL, NULL, 'signup', :q, :a, :t)"
                        ),
                        {"u": user_id, "q": question_id, "a": answer, "t": created_at},
                    )
                mirrored += 1
            if mirrored:
                print(f"[migrate] mirrored {mirrored} signup survey answer set(s) into feedback_responses")
    except Exception as e:  # pragma: no cover - a mirror must never block startup
        print(f"[migrate] could not backfill signup feedback: {e!r}")


def _migrate_signup_indexes():
    """Create the indexes `users.google_sub` / `users.email` declare on the model.

    Same trap as _migrate_ghost_indexes documents for jobs_seen.repost_key:
    _migrate_columns only issues ALTER TABLE ADD COLUMN, and create_all never
    revisits an existing table, so `index=True` / `unique=True` on a column added
    that way takes effect only on a database built from scratch afterwards.

    The uniqueness of `google_sub` is not a performance detail here -- it is the
    last line of defence against two rows for the same Google account, which
    would silently split one person's profiles and search history in two. It is
    created as a UNIQUE index for that reason; `email` gets a plain one because
    email is display-only and is genuinely allowed to repeat (a legacy beta
    account and a Google account can belong to the same person).

    Idempotent via IF NOT EXISTS. The UNIQUE index will legitimately fail on a
    database that somehow already holds duplicates -- that is reported rather
    than swallowed, because carrying on would leave the invariant unenforced
    with nothing anywhere saying so."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("users"):
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if "google_sub" not in cols:
        return
    # Guarded per column rather than as one list: _migrate_columns runs first so
    # all four normally exist, but a half-migrated store must not turn a missing
    # column into a printed error that looks like index corruption.
    stmts = ["CREATE UNIQUE INDEX IF NOT EXISTS ix_users_google_sub ON users (google_sub)",
             "CREATE INDEX IF NOT EXISTS ix_users_email ON users (email)"]
    if "apple_sub" in cols:
        # Same argument as google_sub, for the same reason: two rows for one
        # Apple account would split a person's profiles and history in two.
        stmts.append("CREATE UNIQUE INDEX IF NOT EXISTS ix_users_apple_sub ON users (apple_sub)")
    if "auth_provider" in cols:
        stmts.append("CREATE INDEX IF NOT EXISTS ix_users_auth_provider ON users (auth_provider)")
    with engine.begin() as conn:
        for s in stmts:
            try:
                conn.execute(text(s))
            except Exception as e:  # pragma: no cover - only on a corrupt store
                print(f"[migrate] could not create index ({s.split()[-3]}): {e}")


def _migrate_auth_provider():
    """Stamp `google` on accounts that predate the auth_provider column.

    NULL on that column means "legacy hand-assigned account", and that meaning
    is load-bearing: auth_router._needs_survey never asks a legacy account the
    sign-up questions, so leaving a Google account NULL would silently exempt
    every existing self-serve user from a survey they have (in most cases)
    already answered -- harmless -- while also mislabelling them in
    GET /admin/signups, which is the only place anyone sees who has arrived.

    Cannot guess wrong: `google_sub` has only ever been written by the Google
    sign-in path, so a non-null value is proof of provenance. Rows with a NULL
    google_sub are left NULL, which is exactly right for them. Apple and email
    accounts always write the column at creation, so they never need this.
    Idempotent -- the WHERE clause matches nothing on a second run."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("users"):
        return
    cols = {c["name"] for c in insp.get_columns("users")}
    if not {"auth_provider", "google_sub"} <= cols:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE users SET auth_provider = 'google' "
            "WHERE auth_provider IS NULL AND google_sub IS NOT NULL AND google_sub != ''"
        ))


def _migrate_soft_dup_key():
    """Backfill + index jobs_seen.soft_dup_key, the normalized company+title key
    the discovery upsert's soft-duplicate lookup keys on (see
    engine._soft_dup_key / _find_soft_duplicate).

    Why this exists at all: the lookup used to pre-filter with
    `lower(company) = x OR lower(company) LIKE x || '%'` and hydrate FULL JobSeen
    entities for every match. On a real store that returned ~818 rows per call --
    each carrying an 8KB base64 embedding -- and it ran once per newly-discovered
    identity (992 of them in a measured run). Profiling put 45s of a 61s
    _upsert_discovered inside those queries alone, which is most of what the
    "embed" phase timing actually measures. Keyed equality on an indexed column
    took the same workload from 53.6s to 0.34s.

    A plain index on lower(company) is NOT a substitute (measured 81.4s -> 77.8s):
    the cost is the VOLUME of rows a common company prefix returns, not scan time.

    The key is also strictly more correct than the SQL it replaces. `_norm_company`
    strips leading/trailing whitespace on the incoming side, but SQL could not
    strip it on the stored side, so a source that emits "\\t ZENOVO LTD" never
    matched its own earlier row. Computing both sides in Python fixes that: on a
    400-row sample the two agreed 397 times and all 3 differences were duplicates
    the old query MISSED.

    Idempotent: rows already carrying a key are left alone, so this is a no-op
    after the first boot. Backfill measured 1.4s for 9,042 rows, and adds no
    measurable file size."""
    from sqlalchemy import inspect, text
    from app.services.engine import _soft_dup_key

    insp = inspect(engine)
    if not insp.has_table("jobs_seen"):
        return
    if "soft_dup_key" not in {c["name"] for c in insp.get_columns("jobs_seen")}:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_jobs_seen_soft_dup "
            "ON jobs_seen (profile_id, soft_dup_key)"
        ))
        rows = conn.execute(text(
            "SELECT id, title, company FROM jobs_seen WHERE soft_dup_key IS NULL"
        )).fetchall()
        if not rows:
            return
        conn.execute(
            text("UPDATE jobs_seen SET soft_dup_key = :k WHERE id = :i"),
            [{"k": _soft_dup_key(company, title), "i": rid}
             for rid, title, company in rows],
        )


def _migrate_ghost_indexes():
    """Create the indexes the ghost-listing reads need, including one that was
    supposed to already exist.

    JobSeen.repost_key is DECLARED `index=True`, and on the live database the
    index is simply absent -- only profile_id, identity_hash, user_id and
    soft_dup are there. The reason is a trap worth stating plainly, because it
    applies to every column this codebase has added since the first boot:
    _migrate_columns only issues `ALTER TABLE ADD COLUMN`, and create_all never
    revisits an existing table to add an index. So `index=True` takes effect
    only on a database created from scratch AFTER the column was declared. Every
    repost-group aggregation was a full 10k-row scan.

    Idempotent via IF NOT EXISTS; costs nothing on a database that already has
    them."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("jobs_seen"):
        return
    cols = {c["name"] for c in insp.get_columns("jobs_seen")}
    statements = []
    if "repost_key" in cols:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_jobs_seen_repost "
            "ON jobs_seen (profile_id, repost_key)"
        )
    if "source_ref" in cols:
        statements.append(
            "CREATE INDEX IF NOT EXISTS ix_jobs_seen_source_ref "
            "ON jobs_seen (source, source_ref)"
        )
    if not statements:
        return
    with engine.begin() as conn:
        for stmt in statements:
            conn.execute(text(stmt))


def _migrate_listing_observations():
    """Seed the global listing_observations table from the per-profile history
    already sitting on jobs_seen.

    Why a backfill at all: the observation window is the one thing about this
    feature that cannot be reconstructed later, and the store already holds real
    (if short) history. Starting the global table empty would throw away the
    days already recorded and push every longitudinal rule out by that much.

    The merge is per identity_hash across ALL profiles: set-union of the
    seen_dates day integers (so two profiles that saw the same listing on
    different days contribute both), earliest first_seen, latest last_seen, and
    the first non-null dead_at/dead_reason/last_verified_at found. Union rather
    than max(seen_days) because seen_days is a count and counts cannot be merged
    without double-counting a shared day.

    Idempotent: an identity already present is left alone entirely, so this is a
    no-op after the first boot and never overwrites history the crawl has since
    extended. Measured shape at time of writing: ~10k rows, sub-second."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("jobs_seen") or not insp.has_table("listing_observations"):
        return
    cols = {c["name"] for c in insp.get_columns("jobs_seen")}
    if "seen_dates" not in cols:
        return

    with engine.begin() as conn:
        existing = {r[0] for r in conn.execute(
            text("SELECT identity_hash FROM listing_observations")
        )}
        rows = conn.execute(text(
            "SELECT identity_hash, source, company, title, url, repost_key, "
            "       posted_at, posted_at_approx, expires_at, first_seen, last_seen, "
            "       seen_dates, dead_at, dead_reason, last_verified_at "
            "FROM jobs_seen ORDER BY id"
        )).fetchall()

        merged: dict[str, dict] = {}
        for r in rows:
            ident = r[0]
            if not ident or ident in existing:
                continue
            days = {int(d) for d in (r[11] or "").split(",") if d.strip().isdigit()}
            cur = merged.get(ident)
            if cur is None:
                merged[ident] = {
                    "identity_hash": ident, "source": r[1], "company": r[2],
                    "title": r[3], "url": r[4], "repost_key": r[5],
                    "posted_at": r[6], "posted_at_approx": r[7], "expires_at": r[8],
                    "first_seen": r[9], "last_seen": r[10], "days": days,
                    "dead_at": r[12], "dead_reason": r[13], "last_verified_at": r[14],
                }
                continue
            cur["days"] |= days
            # earliest first_seen / latest last_seen across every profile that saw it
            if r[9] and (not cur["first_seen"] or r[9] < cur["first_seen"]):
                cur["first_seen"] = r[9]
            if r[10] and (not cur["last_seen"] or r[10] > cur["last_seen"]):
                cur["last_seen"] = r[10]
            for key, val in (("posted_at", r[6]), ("expires_at", r[8]),
                             ("dead_at", r[12]), ("dead_reason", r[13]),
                             ("last_verified_at", r[14]), ("repost_key", r[5]),
                             ("company", r[2]), ("url", r[4])):
                if cur.get(key) is None and val is not None:
                    cur[key] = val

        if not merged:
            return
        payload = []
        for m in merged.values():
            m["seen_dates"] = ",".join(str(d) for d in sorted(m.pop("days")))
            payload.append(m)
        conn.execute(text(
            "INSERT INTO listing_observations "
            "(identity_hash, source, company, title, url, repost_key, posted_at, "
            " posted_at_approx, expires_at, first_seen, last_seen, seen_dates, "
            " dead_at, dead_reason, last_verified_at) "
            "VALUES (:identity_hash, :source, :company, :title, :url, :repost_key, "
            " :posted_at, :posted_at_approx, :expires_at, :first_seen, :last_seen, "
            " :seen_dates, :dead_at, :dead_reason, :last_verified_at)"
        ), payload)


def _migrate_dead_at_backfill():
    """Repair rows carrying a dead_reason but no dead_at.

    dead_at is documented as stamped once and never overwritten, and every
    current write site honours that -- but a row predating the column, or
    written by a path that set dead_reason alone, leaves the pair inconsistent
    (the live store has one). That matters now rather than cosmetically, because
    the takedown-then-repost rule keys on dead_at and would silently skip such a
    row forever.

    last_seen is the honest stand-in: it is the last time discovery saw the
    listing at all, so it is the latest moment the listing could still have been
    alive. Idempotent -- only fills NULLs, never adjusts an existing stamp."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("jobs_seen"):
        return
    cols = {c["name"] for c in insp.get_columns("jobs_seen")}
    if not {"dead_at", "dead_reason"} <= cols:
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE jobs_seen SET dead_at = COALESCE(last_seen, first_seen) "
            "WHERE dead_reason IS NOT NULL AND dead_at IS NULL"
        ))


def _migrate_family_tier_vocabulary():
    """One-time value fixup for role_families.tier: the old core/secondary
    PRIORITY scale was replaced by a strict active/inactive on/off switch (see
    config.py's role-families section) -- an inactive family now gets no
    cluster at all, rather than merely a damped embedding weight. 'core' meant
    full weight, so it maps straight to 'active'. 'secondary' also mapped to
    'active': at runtime it was always fully searched (gated, ranked, judged)
    same as core, just at a 0.6x embedding weight that barely showed up in
    practice -- mapping it to 'inactive' instead would silently stop searching
    a stream the candidate is currently getting real results from. Idempotent:
    a no-op once no row still carries the old values."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("role_families"):
        return
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE role_families SET tier = 'active' WHERE tier IN ('core', 'secondary')"
        ))
