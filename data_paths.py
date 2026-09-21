"""Single home for every path this app WRITES at runtime.

Everything the app generates -- the SQLite store, the engine's gate/rank cache,
the CV scratch file the expensive-AI step reads, the raw discovery dump -- lands
under ONE directory so that "give me a clean slate" is `rm -rf data/` rather than
hunting two dozen files across backend/, tests/ and the repo root. That directory
is gitignored as a unit, the same posture `archive/` already has.

Two consumers, and they must not drift: `full_auto.py` (repo root, also runs
standalone) and `backend/app/config.py`. Neither can import the other cheaply --
config.py importing full_auto would pull in crawl4ai and playwright just to learn
a path -- so this module is the shared dependency both take instead of each
spelling the paths out. Same single-source reasoning as `SOFT_GATE_AXES`.

Override the whole directory with DATA_DIR, or any individual file with its own
env var (see the constants below). An override wins over DATA_DIR, which wins
over the default. Nothing here reads .env itself: under the backend, config.py's
load_dotenv() has already run by the time anything imports this, and the
standalone `python full_auto.py` path takes its environment from the shell, which
is how every other env var in that file already behaves.
"""
import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"

# The one directory everything generated lands in. Resolved at import so a
# relative DATA_DIR is interpreted against the cwd the process started in, not
# against whatever a later chdir leaves behind.
DATA_DIR = Path(os.getenv("DATA_DIR") or (ROOT_DIR / "data")).expanduser().resolve()


def data_path(name: str, env_var: str | None = None) -> Path:
    """Resolve one generated file. `env_var` overrides DATA_DIR for that file alone."""
    if env_var:
        override = os.getenv(env_var)
        if override:
            return Path(override).expanduser().resolve()
    return DATA_DIR / name


def ensure_data_dir(path: Path | None = None) -> None:
    """Create the parent directory for a generated file (or DATA_DIR itself).

    Every path here is WRITTEN before it is read, so the directory has to exist
    before the first open(..., "w") rather than being assumed present -- that
    assumption is exactly what made CV_PATH a FileNotFoundError on a fresh
    checkout.
    """
    target = (path.parent if path is not None else DATA_DIR)
    target.mkdir(parents=True, exist_ok=True)


# --- the generated files ----------------------------------------------------
# The CV text the expensive-AI step reads. Written at the top of every run.
CV_PATH = data_path("exp.txt", "CV_PATH")
# The engine's own SQLite cache: gate_cache (screen/rank verdicts) + profile_cache.
BOARDS_CACHE_PATH = data_path("boards_cache.db", "BOARDS_CACHE_DB")
# Raw discovery dump, written when DEBUG_SAVE_RAW is on. Several MB per run.
RAW_API_JOBS_PATH = data_path("raw_api_jobs.json", "RAW_API_JOBS_PATH")

# The application store. Unlike the three above -- all of which are caches or
# scratch and cost nothing to lose -- this one holds the user's profiles, roles
# and feedback history. Moving it would silently orphan an existing install's
# only copy, so a database already sitting at the pre-DATA_DIR location keeps
# being used. Fresh installs get data/; nobody's store disappears on upgrade.
#
# This is ALWAYS a real path, never None, even when DATABASE_URL is set. Callers
# that can honour DATABASE_URL (config.py) check it themselves and use this only
# as their default; callers that cannot -- full_auto._ats_db_path() opens a raw
# sqlite3 connection for the end-of-run ATS harvest -- need a usable answer here
# or they silently write to a different database than the backend reads.
_LEGACY_DB = BACKEND_DIR / "jobmatch.db"
APP_DB_PATH = _LEGACY_DB if _LEGACY_DB.exists() else data_path("jobmatch.db")
