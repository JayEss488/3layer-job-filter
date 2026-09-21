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

# ── The single local user ────────────────────────────────────────────────────
# There is no login. Every table still carries a `user_id` column and every
# ownership check still goes through deps.current_user_id(), which returns this
# constant -- so the multi-user seam is intact if it is ever wanted again, but
# nothing today asks who you are.
LOCAL_USER_ID = int(os.getenv("LOCAL_USER_ID", "1"))

# Kept as an alias because `models.py` uses it as a column default and a handful
# of diagnostics probes reference it by this name.
CURRENT_USER_ID = LOCAL_USER_ID

# Optional shared secret guarding the maintenance endpoints under /admin (the
# direct-employer crawl, the daily observation pass, the liveness re-check),
# sent as the `X-Admin-Token` header. Empty (the default) leaves them open,
# which is correct for a tool bound to localhost and wrong the moment it is not.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# SQLite by default; flip DATABASE_URL to a postgres:// URL to migrate later.
#
# The default file now lives under the gitignored data/ directory alongside every
# other generated file, so wiping local state is one `rm -rf data/` instead of a
# hunt across backend/, tests/ and the repo root. data_paths is imported (not
# spelled out here) because full_auto.py needs the same answers and the two must
# not drift; it is safe to import at this point because ROOT_DIR is already on
# sys.path above and the module pulls in nothing heavier than pathlib.
#
# data_paths.APP_DB_PATH keeps returning the OLD backend/jobmatch.db whenever that
# file already exists, so an existing install's only copy of its profiles and
# roles is never silently orphaned by this change -- only fresh installs get data/.
# It is None when DATABASE_URL is set, in which case the caller's value wins and
# there is no path to resolve.
import data_paths as _dp

_dp.ensure_data_dir()
DATABASE_URL = os.getenv("DATABASE_URL") or f"sqlite:///{_dp.APP_DB_PATH}"

# Frontend origin(s) allowed through CORS.
FRONTEND_ORIGINS = os.getenv(
    "FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")

# Cost guard: max live searches per calendar day. This is the main thing
# standing between an experiment and a surprising API bill -- a run costs
# roughly 100k tokens (see the README's cost section). Raise it deliberately.
#
# Note a run killed mid-flight (crash, restart) still counts: the row exists and
# it really did spend API credits.
MAX_SEARCHES_PER_DAY = int(os.getenv("MAX_SEARCHES_PER_DAY", "6"))

# Capacity guard, orthogonal to the per-user daily cap above: how many searches
# may be IN FLIGHT across the whole process at once. Each concurrent run drives
# its own headless Chromium (full_auto.MAX_CONCURRENT pages apiece) plus several
# thread pools, so this is really a memory ceiling -- on the 2GB Fly VM a
# handful of simultaneous runs is enough to OOM the machine, which kills every
# user's search at once rather than just delaying one. Degrading to "try again
# in a few minutes" is strictly better than that. Raise this only alongside the
# VM's memory.
MAX_CONCURRENT_SEARCHES = int(os.getenv("MAX_CONCURRENT_SEARCHES", "2"))

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
    # Single-value, like location_scope/seniority: how old a listing (by its own
    # stated/discoverable posting date) the candidate will tolerate. Value is the
    # number of days as a string, e.g. "30"; no row -> DEFAULT_MAX_LISTING_AGE_DAYS.
    # Enforced Hard by default (see ENFORCEMENT_DEFAULT) -- a listing whose posting
    # date is DEFINITELY known to be older is dropped outright; Soft downgrades it
    # instead of dropping it. An unknown/uncertain date is never penalised either
    # way -- see full_auto.py's listing_over_max_age/_listing_age_tag.
    "max_listing_age",
    # Single-value, like location_scope/max_listing_age: how far the candidate
    # will travel from their stated location, in miles as a string (e.g. "30").
    # "0" is a real value meaning "no distance limit". Only bites at
    # location_scope="local" -- National/International have deliberately opted
    # out of narrowing by place at all -- and rides the Location row's own
    # Hard/Soft rather than carrying its own, since it answers the same question
    # ("does where I am constrain results"). No row -> DEFAULT_COMMUTE_MILES.
    "commute_miles",
    # Single-value boolean: show only employers on the Home Office register of
    # licensed visa sponsors. Value "true"; no row at all -> off, which is the
    # default, because it is a strict filter that most candidates don't need.
    # Unlike every other constraint here this one is inherently HARD -- there is
    # no useful "soft" reading of "I need a visa" -- so it carries no Hard/Soft
    # control and ENFORCEMENT_DEFAULT is irrelevant to it.
    #
    # The match is on company NAME against a 127k-organisation register with no
    # domains and no company numbers, so a miss conflates "not a sponsor" with
    # "the listing didn't name the employer well enough to tell" -- agency
    # postings and blank-company aggregator rows are the two structural cases.
    # See services/sponsors.py for the measured match rate and both limitations.
    "visa_sponsor_only",
    # Single-value, like max_listing_age/commute_miles: the minimum ANNUAL salary
    # a role must clear to count as sponsorable, in pounds as a string (e.g.
    # "41700"). Only ever consulted when visa_sponsor_only is on. "0" is a real
    # value meaning "no floor" -- no row at all means DEFAULT_VISA_SPONSOR_MIN_
    # SALARY, same "unset vs. explicit zero" distinction as those two types.
    #
    # The Skilled Worker route is NOT a single cutoff -- GBP 41,700 is the general
    # threshold for a standard applicant, but several categories are sponsorable
    # well below it: "new entrants" (under 26, a recent Student/Graduate switcher,
    # or training toward a professional qualification) at 70% of the going rate
    # (roughly GBP 33,400) for up to four years; PhD holders at GBP 37,500 (GBP
    # 33,400 for a STEM PhD); roles on the Immigration Salary List at a discounted
    # floor (~GBP 33,400); and a specific health/education occupation table as low
    # as GBP 31,300. The Health and Care Worker visa is a separate route entirely,
    # not governed by any of these figures.
    #
    # None of those categories are things this app knows about the candidate
    # (age, visa-switch history, PhD subject) or the listing (occupation code,
    # ISL membership), and guessing them from free text would be unreliable on
    # a HARD filter -- so this is deliberately just the number itself, defaulted
    # to the standard-applicant floor and left for the candidate to lower if a
    # reduced category applies to them. See services/sponsors.py.
    "visa_sponsor_min_salary",
    # Single-value boolean: is the candidate open to roles pitched BELOW their own
    # stated seniority? Value "true"; no row at all -> off, which is the default,
    # because most candidates searching at their level do not want to be shown
    # roles beneath it and the free title prescreen that drops those is one of the
    # cheapest filters in the pipeline.
    #
    # Direction-aware, and only meaningful for a SENIOR-side profile: it turns
    # engine._heuristic_prescreen's junior-title reject from an unconditional drop
    # into a soft signal, and tells the cheap gate/rank/judge that a lower-pitched
    # role is not itself a mismatch. It does NOT touch the junior-profile
    # direction (a Graduate candidate is not helped by being shown Director roles).
    #
    # Like visa_sponsor_only it carries no Hard/Soft control -- the whole point of
    # the flag is that it makes something soft, so a "Hard" reading would be
    # self-contradictory. Note what it deliberately does NOT re-admit:
    # apprenticeships and placement years, which were excluded for reasons OTHER
    # than seniority (they are courses with eligibility bars against people who
    # already hold the qualification -- see _PLACEMENT_YEAR_RE and the
    # apprenticeship rules at all three LLM tiers).
    "allow_overqualified",
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
# whitespace; CV_SUMMARY_MAX_CHARS guards the long-CV, AI-compressed path.
#
# These are SAFETY bounds against a runaway reply, NOT a length budget -- the
# length the model is actually asked for lives in profile_intel._SUMMARY_TASK
# ("max 450 words"), and this must sit clear of it or it silently becomes the
# real limit. 3000 did exactly that: a live parse was cut at 2997 chars, mid-
# sentence, at word 432 of a compliant ~460-word reply. What gets lost is not
# random -- _SUMMARY_TASK numbers its sections, so the tail is always (4)
# leadership/extracurriculars, (5) writing/communication work and (6) languages
# and other differentiators, and the candidate_brief the final judge reads
# (snapshot.candidate_brief) is where that evidence now lives at all, since
# skill/qualification chips are no longer extracted. So the cap was quietly
# deleting a whole class of evidence from every long-CV profile, permanently.
# 450 words of dense prose runs ~3100-3300 chars; 4000 leaves real headroom.
# Truncation is also sentence-boundary-aware now (profile_intel.clip_summary),
# so even a reply that does overrun can no longer end mid-word.
CV_SUMMARY_RAW_MAX_CHARS = 6000
CV_SUMMARY_MAX_CHARS = 4000

# Wall-clock ceiling on the whole CV/notes parse (services/formation.py), after
# which the request gives up and says so.
#
# WHY THIS HAS TO EXIST. The parse fans out to three concurrent llm_json calls,
# and NOTHING upstream bounded the block. llm_json retries once on its own, and
# the OpenAI SDK retries twice underneath that, against a 90s read timeout -- so
# one stalled call could occupy the request for ~9 minutes before raising
# anything, and llm_json's failure path returns {} rather than logging loudly.
# What that looks like from the outside is precisely what was reported: a
# spinner that never resolves, no error in the console, and a second attempt
# seconds later that succeeds in ten. An unbounded wait is not a safer wait --
# it just moves the failure somewhere the user cannot see it.
#
# Sized off the real distribution, not guessed: the block's critical path is the
# ~450-word summary call, measured at ~11s on a 4,400-word CV, and the Settings
# "CV parse timing" panel reports the same shape. 120s is roughly ten times that
# -- generous enough that no honest slow parse is cut short, short enough that a
# stall surfaces as an error while the user is still watching.
#
# Abandoning the in-flight threads is safe here BY CONSTRUCTION and not by luck:
# formation's three calls are deliberately DB-free (see its module docstring) so
# they share no session, hold no transaction, and touch nothing when they
# eventually return into a request that has gone.
CV_PARSE_TIMEOUT_SECONDS = 120

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
    # Hard by default per the candidate's own request: a definitely-old listing
    # is dropped outright unless the candidate explicitly softens it to a
    # downgrade-only preference.
    "max_listing_age": "hard",
}

# Fallback when the candidate has no max_listing_age row at all (never edited
# the preference). Mirrors full_auto.py's own module-level fallback for the
# standalone CLI path, which has no ProfileAttribute table to read from --
# kept in sync manually since the two modules don't share config imports.
DEFAULT_MAX_LISTING_AGE_DAYS = 30

# Fallback commute radius, in miles, when the candidate has picked "Local" scope
# but never touched the distance control. Only ever consulted at local scope.
# 30 miles is roughly the upper end of a routine UK commute -- generous enough
# that turning Local on doesn't silently empty the results page, tight enough
# that it means something. Note this is straight-line distance between postcode
# centroids (services/geo.py), not drive time: it is a coarse "is this even in
# reach" test, and every stage downstream still reasons about the location text
# itself.
DEFAULT_COMMUTE_MILES = 30

# Default floor for visa_sponsor_min_salary: the general Skilled Worker going-rate
# threshold for a standard applicant (not a new entrant, not a reduced-rate
# occupation). Only consulted when visa_sponsor_only is on -- see the
# ATTRIBUTE_TYPES entry above for the categories that qualify for less and why
# this app doesn't try to detect them automatically.
DEFAULT_VISA_SPONSOR_MIN_SALARY = 41700


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
#
# active|inactive -- a strict on/off switch, not a priority scale. An inactive
# family gets no cluster at all (see snapshot._role_groups/families.
# ordered_for_engine): no discovery, no embedding, no gate/rank/judge calls --
# the algorithm ignores it completely, same as if its target roles didn't
# exist. There used to be a third "secondary" tier that stayed fully in the
# pipeline but at a damped 0.6x embedding weight (FAMILY_TIER_MULT); dropped
# because a family that still runs its own full discovery/gate/judge lifecycle
# barely reads as deprioritized in practice -- if a stream isn't wanted, it
# should cost nothing, not just rank lower.
FAMILY_TIER_CHOICES = ["active", "inactive"]
FAMILY_TIER_DEFAULT = "active"

# Emphasis multiplier applied in snapshot._weighted_text for a pinned role
# within its (active) family.
PINNED_ROLE_MULT = 1.4

# Same cap the LLM clustering path has always enforced: discovery volume is
# fixed per run, not scaled by cluster count, so more clusters only thins each
# one's candidate pool. Active families past this cap are merged into the last
# one, in display order (see families.ordered_for_engine); inactive families
# never count against the cap at all.
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

# The reserve pool size services/families.top_up_family_titles tops a family
# up to (additively -- existing titles, pinned or not, are never touched or
# replaced). Bigger than FAMILY_REGEN_TARGET_COUNT on purpose: this is the
# pool a weak search run's wider discovery-term window (see
# engine._weak_reference_run / full_auto.TERMS_PER_RUN_WIDE) draws from, not
# what's shown by default -- RoleFamilyCard.tsx caps the UI to 8 chips
# regardless of how many titles a family actually holds.
FAMILY_TITLE_RESERVE_TARGET = 15

# Below this many roles shown, the PREVIOUS first-run-of-the-day (see
# engine._reference_run) counts as a "weak" run: this run's discovery term
# window widens from TERMS_PER_RUN to TERMS_PER_RUN_WIDE (full_auto.py) to
# give the boards more distinct titles to search against. A run returning at
# or above this is left on the normal, faster window -- see
# engine._weak_reference_run.
WEAK_RUN_RESULT_THRESHOLD = 6

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
    "max_listing_age": "constraint",
    "commute_miles": "constraint",
    "visa_sponsor_only": "constraint",
    "visa_sponsor_min_salary": "constraint",
    "allow_overqualified": "constraint",
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
# "global" is a sentinel meaning "no country filter". The worldwide list is
# generated from scripts/gen_countries.py alongside the engine's country tokens
# and the frontend picker, so all three stay in sync (regenerate to refresh).
from .countries_gen import COUNTRY_CHOICES  # noqa: E402

# ── Feedback weight system (see implementation notes section 3) ──────────────
DELTAS = {"tick": 0.10, "cross": -0.15, "ignore": -0.02}
WEIGHT_MIN, WEIGHT_MAX = 0.1, 2.0
DEFAULT_WEIGHT = 1.0
