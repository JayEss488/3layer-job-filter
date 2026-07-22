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

# Cost guard: max live searches, shared across all profiles, per calendar day.
MAX_SEARCHES_PER_DAY = int(os.getenv("MAX_SEARCHES_PER_DAY", "6"))

# Cost/time guard: skip re-querying the ~40-company ATS rotation batch (the
# single largest chunk of a run's discovery calls) when the last fetch for
# this profile is still within this many hours. The always-fresh term-based
# API sources (Reed/Adzuna/Google Jobs/etc.) are unaffected.
DISCOVERY_ATS_CACHE_TTL_HOURS = float(os.getenv("DISCOVERY_ATS_CACHE_TTL_HOURS", "4"))

# Category-page expansion (full_auto.expand_category_pages). Currently recovers
# ~0 jobs in practice -- crawl4ai finds 0 raw links on JS-hydrated category pages
# -- while still paying full crawl cost (~4-5s/page). Off by default until
# extraction is revisited. Internal/ops knob, not exposed in the Settings UI.
CATEGORY_EXPAND_ENABLED = os.getenv("CATEGORY_EXPAND_ENABLED", "false").strip().lower() == "true"

# Below this many semantic-filter survivors we warn the filters may be too harsh.
HARSH_FILTER_THRESHOLD = 5

# Nothing ever re-scrapes an already-shown "new" role to check whether the
# listing has since closed -- Phase 5 only scrapes a job the first time it's
# evaluated. Past this many days unreviewed, auto-move it to "ignored" (not
# "deleted": reversible via the Ignored tab's re-save) on the assumption a
# listing that old has very likely expired.
ROLE_STALE_DAYS = int(os.getenv("ROLE_STALE_DAYS", "30"))

# ── Controlled vocabulary of attribute types ────────────────────────────────
# Keep this small. New categories should generally reuse a type, not add
# columns. "qualification" and "sector_target" are deliberate exceptions:
# formal credentials (degree class, certifications) and explicit sector/
# mission-targeting language (cover-letter angles) are structurally distinct
# from skill bullets and job titles, and previously had no field to land in
# at all -- see the profile-builder fidelity fix.
ATTRIBUTE_TYPES = [
    "past_role",
    "skill",
    "qualification",
    "seniority",
    "target_role",
    "sector_target",
    "salary",
    "location",
    "country",
    "location_scope",
    "custom",
    # Candidate's own hard filters, editable as chips and auto-filled from the CV
    # (parsing.py). "avoid" = things to reject on; "must_have" = non-negotiables.
    # Enforced as hard drops at the cheap gate + final judge (LLM-adjudicated on a
    # CLEAR violation only) -- distinct from the softer LLM-derived "requirements".
    "avoid",
    "must_have",
]

# Where a skill's depth was earned -- distinct from proficiency (which grades
# depth, not origin). Set by CV parsing (parsing.py) or edited by the user via
# AttributeRow.tsx's dropdown (kept in manual sync there, same as the
# proficiency choices below have no shared frontend import). Only meaningful
# for skill rows; omitted means no origin signal available -- never defaults
# to "Commercial".
EVIDENCE_ORIGIN_CHOICES = ["Commercial", "Self-directed", "Academic", "AI-assisted"]

# The work-type tokens that share the `location` attribute type with the
# free-text place name. snapshot.build_snapshot splits the two apart on exactly
# this set -- keep it in sync with LocationPicker.tsx's WORK_TYPES.
WORK_TYPE_VALUES = {"remote", "hybrid", "on-site", "onsite"}

# ── CV summary length handling ──────────────────────────────────────────────
# Below this many words, a candidate's raw CV/notes text is already about as
# short as a compressed brief would be -- asking the model to compress it
# further risks losing real, concrete detail (paid work experience, named
# projects, societies, honesty caveats like "AI-assisted") for no space saved,
# and a compressed reply that runs long risks being hard-truncated mid-word by
# CV_SUMMARY_MAX_CHARS below. So parsing.py stores the raw text as cv_summary
# verbatim instead of asking the model to write one, and profile_intel.py skips
# generating a separate "Looking for" header for the same reason -- the raw
# text already speaks for itself. Shared by both modules so the two decisions
# never disagree about what counts as "short".
CV_SHORT_WORD_THRESHOLD = 750

# Storage caps for profile.cv_summary (a Text column -- these are app-level
# sanity bounds, not DB limits). CV_SUMMARY_RAW_MAX_CHARS guards the verbatim
# short-CV path above against a pathological document with almost no
# whitespace; CV_SUMMARY_MAX_CHARS guards the long-CV, AI-compressed path,
# raised from an old 1200 (which used to cut a reply off mid-word) to comfortably
# fit the fuller ~400-word descriptive overview parsing.py now asks for.
CV_SUMMARY_RAW_MAX_CHARS = 6000
CV_SUMMARY_MAX_CHARS = 3000

# ── Hard/soft enforcement ───────────────────────────────────────────────────
# How literally a constraint row is applied. "hard" = an unconditional drop the
# moment the listing CLEARLY violates it (screen_gate drops it outright; the
# final judge carries it as a DISQUALIFIER). "soft" = a stated preference the
# judge weighs and the cheap gate counts as one ordinary soft-axis failure, but
# which can never on its own remove a listing.
#
# Only meaningful on the constraint types below; null elsewhere. Each type's
# default is the behaviour that type already had before enforcement existed, so
# an un-migrated row keeps working identically:
#   avoid/must_have -> hard   (they have always been unconditional drops)
#   location        -> hard   (the country/city prefilter is fail-closed by
#                              design -- see CLAUDE.md's location-scope section;
#                              defaulting it to soft would silently un-close it)
#   seniority/salary -> soft  (they have always been soft screen_gate axes)
ENFORCEMENT_CHOICES = ["hard", "soft"]

ENFORCEMENT_DEFAULT = {
    "avoid": "hard",
    "must_have": "hard",
    "location": "hard",
    "seniority": "soft",
    "salary": "soft",
}


def enforcement_for(attr_type: str, value: str | None) -> str:
    """The stored enforcement, or the type's pre-enforcement default.

    Work-type rows (Remote/Hybrid/On-site) share the `location` type with the
    free-text city but are NOT the fail-closed country prefilter -- they feed
    screen_gate's soft work_arrangement axis. So they default to "soft" while a
    place row defaults to "hard"; both are still overridable per row."""
    if attr_type == "location" and (value or "").lower().strip() in WORK_TYPE_VALUES:
        return "soft"
    return ENFORCEMENT_DEFAULT.get(attr_type, "soft")


# ── Role families ───────────────────────────────────────────────────────────
# A family is one user-editable "stream" of target roles (e.g. "Data Analyst"
# holding "Data Analyst", "CRM Data Analyst", "Data Officer"). Families ARE the
# engine's clusters: the pipeline scores, gates, and judges each one
# independently (see CLAUDE.md's search-pipeline section). They replace the
# per-run LLM clustering call, which now only ever runs once to SEED families
# from a freshly parsed CV (see services/families.py).
FAMILY_TIER_CHOICES = ["core", "secondary"]
FAMILY_TIER_DEFAULT = "core"

# Emphasis multipliers applied in snapshot._weighted_text, alongside (not
# instead of) the learned feedback weight -- tier and pin are the candidate's
# declared priority, weight is what their tick/cross history revealed, and the
# two are deliberately orthogonal signals that multiply together.
FAMILY_TIER_MULT = {"core": 1.0, "secondary": 0.6}
PINNED_ROLE_MULT = 1.4

# Same cap the LLM clustering path has always enforced: discovery volume is
# fixed per run, not scaled by cluster count, so more clusters only thins each
# one's candidate pool. Families past this cap are merged into the last one
# (core families first -- see families.ordered_for_engine).
MAX_ROLE_CLUSTERS = 3

# How many family CARDS the candidate can manually create, enforced in
# routers/families.py::add_family. Deliberately one more than MAX_ROLE_CLUSTERS
# above -- LLM seeding already never exceeds that cap, so this only bounds
# manual additions, and capping at +1 means at most one family ever needs to
# be folded into another at search time instead of an unbounded amount of
# fold-in damage.
MAX_USER_ROLE_FAMILIES = 4

# How many fresh title suggestions a single family's "regenerate" action asks
# the LLM for (see services/families.regenerate_family). Deliberately more
# generous than a bare minimum -- a family card with only 1-2 roles thins its
# own discovery pool for no reason when the theme plausibly supports more.
FAMILY_REGEN_TARGET_COUNT = 8

# Direction is used by the engine mapping + weighting.
ATTRIBUTE_DIRECTION = {
    "past_role": "background",
    "skill": "background",
    "qualification": "background",
    "seniority": "background",
    "target_role": "target",
    "sector_target": "target",
    "salary": "constraint",
    "location": "constraint",
    "country": "constraint",
    "location_scope": "constraint",
    "custom": "constraint",
    "avoid": "constraint",
    "must_have": "constraint",
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
# `skill` was dropped here when it stopped being auto-extracted (see the
# formation rewrite / parsing.py): a profile that never fills skills as chips
# shouldn't sit permanently at 75% confidence nagging "add skills". Its weight
# was redistributed across the types that ARE still populated automatically, so a
# freshly-parsed profile reads as ready.
CONFIDENCE_REQUIRED = {
    "target_role": 0.35,
    "seniority": 0.20,
    "location": 0.20,
    "salary": 0.10,
    "past_role": 0.15,
}

# Human labels for the "missing" tip builder.
TYPE_LABELS = {
    "target_role": "target roles",
    "skill": "skills",
    "seniority": "seniority",
    "location": "location",
    "salary": "salary range",
    "past_role": "past roles",
    "custom": "extra preferences",
}
