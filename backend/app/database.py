"""SQLAlchemy engine + session. SQLite for the prototype; the same models work
against Postgres by changing DATABASE_URL only."""
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL

# check_same_thread is a SQLite-only knob; harmless to gate on the URL scheme.
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
Base = declarative_base()


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
        ],
        "company_ats": [("keyword", "TEXT")],
        "search_runs": [("phase_timings", "TEXT"), ("funnel_counts", "TEXT")],
        "roles": [("source", "TEXT")],
        "profiles": [("cv_text", "TEXT"), ("cv_summary", "TEXT")],
        "profile_attributes": [("proficiency", "TEXT")],
    }
    for table, cols in additions.items():
        if not insp.has_table(table):
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, coltype in cols:
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}"))
