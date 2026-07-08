"""Central configuration. Single-user prototype: user_id is hardcoded but every
table carries it so multi-user auth can be added later without a schema rewrite."""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Repo root is two levels up from this file (backend/app/config.py -> repo root).
ROOT_DIR = Path(__file__).resolve().parents[2]
BACKEND_DIR = Path(__file__).resolve().parents[1]

# The search engine (full_auto.py) lives at the repo root. Make it importable no
# matter what directory uvicorn is launched from (e.g. running inside backend/).
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# The live engine keys live in the repo-root .env. Load it explicitly so the
# backend picks them up regardless of the working directory it is launched from.
load_dotenv(ROOT_DIR / ".env")
load_dotenv(BACKEND_DIR / ".env", override=False)

# Hardcoded single user. Swap for the authenticated user id when auth arrives.
CURRENT_USER_ID = 1

# SQLite by default; flip DATABASE_URL to a postgres:// URL to migrate later.
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BACKEND_DIR / 'jobmatch.db'}")

# Frontend origin(s) allowed through CORS.
FRONTEND_ORIGINS = os.getenv(
    "FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")

# Cost guard: max live searches per profile per calendar day.
MAX_SEARCHES_PER_DAY = int(os.getenv("MAX_SEARCHES_PER_DAY", "5"))

# Cost/time guard: skip re-querying the ~40-company ATS rotation batch (the
# single largest chunk of a run's discovery calls) when the last fetch for
# this profile is still within this many hours. The always-fresh term-based
# API sources (Reed/Adzuna/Google Jobs/etc.) are unaffected.
DISCOVERY_ATS_CACHE_TTL_HOURS = float(os.getenv("DISCOVERY_ATS_CACHE_TTL_HOURS", "4"))

# Below this many semantic-filter survivors we warn the filters may be too harsh.
HARSH_FILTER_THRESHOLD = 5

# ── Controlled vocabulary of attribute types ────────────────────────────────
# Keep this small. New categories should generally reuse a type, not add
# columns. "qualification" and "sector_target" are deliberate exceptions:
# formal credentials (degree class, certifications) and explicit sector/
# mission-targeting language (cover-letter angles) are structurally distinct
# from skill/experience bullets and job titles, and previously had no field
# to land in at all -- see the profile-builder fidelity fix.
ATTRIBUTE_TYPES = [
    "past_role",
    "skill",
    "experience",
    "qualification",
    "seniority",
    "target_role",
    "sector_target",
    "salary",
    "location",
    "country",
    "location_scope",
    "custom",
]

# Direction is used by the engine mapping + weighting.
ATTRIBUTE_DIRECTION = {
    "past_role": "background",
    "skill": "background",
    "experience": "background",
    "qualification": "background",
    "seniority": "background",
    "target_role": "target",
    "sector_target": "target",
    "salary": "constraint",
    "location": "constraint",
    "country": "constraint",
    "location_scope": "constraint",
    "custom": "constraint",
}

# How far the candidate's stated location/country should be trusted as a hard
# filter. Single-value (like seniority), independent of work-type (remote/
# hybrid/on-site) and independent of the country multi-select: "local" narrows
# to the stated city on top of the country filter, "national" is today's
# default country-fail-closed behaviour, "international" drops the country
# filter entirely regardless of any country chips picked.
LOCATION_SCOPE_CHOICES = [
    ("national", "National"),
    ("local", "Local"),
    ("international", "International"),
]

# Selectable countries for the hard location filter (code -> Adzuna cc / label).
# "global" is a sentinel meaning "no country filter".
COUNTRY_CHOICES = [
    ("global", "Global (no filter)"),
    ("gb", "United Kingdom"),
    ("us", "United States"),
    ("ca", "Canada"),
    ("au", "Australia"),
    ("de", "Germany"),
    ("fr", "France"),
    ("in", "India"),
    ("it", "Italy"),
    ("nl", "Netherlands"),
    ("at", "Austria"),
    ("pl", "Poland"),
    ("sg", "Singapore"),
    ("za", "South Africa"),
]

# ── Feedback weight system (see implementation notes section 3) ──────────────
DELTAS = {"tick": 0.10, "cross": -0.15, "ignore": -0.02}
WEIGHT_MIN, WEIGHT_MAX = 0.1, 2.0
DEFAULT_WEIGHT = 1.0

# ── Confidence indicator weights (section 6) ────────────────────────────────
CONFIDENCE_REQUIRED = {
    "target_role": 0.30,
    "skill": 0.25,
    "seniority": 0.15,
    "location": 0.15,
    "salary": 0.10,
    "past_role": 0.05,
}

# Human labels for the "missing" tip builder.
TYPE_LABELS = {
    "target_role": "target roles",
    "skill": "skills",
    "seniority": "seniority",
    "location": "location",
    "salary": "salary range",
    "past_role": "past roles",
    "experience": "experience",
    "custom": "extra preferences",
}
