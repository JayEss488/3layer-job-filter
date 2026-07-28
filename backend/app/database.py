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
    _migrate_family_tier_vocabulary()


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
        ],
        "company_ats": [("keyword", "TEXT")],
        "search_runs": [("phase_timings", "TEXT"), ("funnel_counts", "TEXT"), ("cancel_requested", "BOOLEAN"),
                         ("snapshot_samples", "TEXT")],
        "roles": [("source", "TEXT"), ("search_run_id", "INTEGER"), ("verdict", "TEXT"),
                   ("work_style", "TEXT"), ("seniority_level", "TEXT"), ("deadline_text", "TEXT"),
                   ("rank_score", "INTEGER"), ("provisional", "BOOLEAN DEFAULT 0"),
                   ("provisional_stage", "TEXT")],
        "profiles": [("cv_text", "TEXT"), ("cv_summary", "TEXT"), ("intent_text", "TEXT"),
                     ("search_feedback", "TEXT")],
        "profile_attributes": [("proficiency", "TEXT"), ("evidence_origin", "TEXT"),
                                ("family_id", "INTEGER"), ("pinned", "BOOLEAN DEFAULT 0"),
                                ("enforcement", "TEXT")],
    }
    for table, cols in additions.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, coltype in cols:
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))


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
