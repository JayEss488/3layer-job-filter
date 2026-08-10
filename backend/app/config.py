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

# Fallback user_id used ONLY as a column default (models.py) and by the
# throwaway diagnostics probe profile. The real, per-request user now comes from
# the authenticated login token -- see services/auth.py and deps.current_user_id().
CURRENT_USER_ID = 1

# ── Auth (closed beta) ───────────────────────────────────────────────────────
# AUTH_SECRET signs login tokens. MUST be set to a stable, random value in
# production: if it's the dev default, tokens are both forgeable and reset on
# every process restart. Generate one with e.g. `python -c "import secrets;
# print(secrets.token_urlsafe(48))"` and set it in the host's env.
AUTH_SECRET = os.getenv("AUTH_SECRET", "dev-insecure-secret-change-me")
if AUTH_SECRET == "dev-insecure-secret-change-me":
    print("[config] WARNING: AUTH_SECRET is the insecure dev default -- set it in the environment before any real deployment.")

# How long a login token stays valid before the user must sign in again.
TOKEN_MAX_AGE_SECONDS = int(os.getenv("TOKEN_MAX_AGE_SECONDS", str(60 * 60 * 24 * 30)))

# Shared secret guarding the owner-only analytics endpoint (GET /admin/analytics,
# sent as the `X-Admin-Token` header). Empty string = endpoint disabled/locked.
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

# ── Self-serve sign-up (Google) ──────────────────────────────────────────────
# The OAuth 2.0 *Web application* client id from Google Cloud Console →
# APIs & Services → Credentials. It is public by design (it ships in the
# frontend bundle); there is no client SECRET here because Google Identity
# Services' button hands the browser a signed ID token directly and the backend
# only ever VERIFIES it -- we never exchange an auth code, so no confidential
# credential is involved.
#
# It is nonetheless load-bearing for security: verification checks the token's
# `aud` claim equals this exact value. Without that check, an ID token minted by
# Google for ANY other application would authenticate here. Empty string
# therefore disables self-serve sign-up entirely (POST /auth/google returns 503)
# rather than falling back to an unverified path.
#
# Add the site's origin to "Authorised JavaScript origins" on that OAuth client,
# or Google refuses to render the button at all.
#
# GOOGLE_OAUTH_CLIENT_ID is accepted as a fallback: a client already existed in
# the repo-root .env under that name before self-serve sign-up was built, and
# silently ignoring it would present as "sign-in is unavailable" on a deployment
# that is, as far as the operator is concerned, already configured.
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "") or os.getenv("GOOGLE_OAUTH_CLIENT_ID", "")
if not GOOGLE_CLIENT_ID:
    print("[config] NOTE: GOOGLE_CLIENT_ID is unset -- Google sign-in is disabled (POST /auth/google -> 503).")

# ── Sign in with Apple ───────────────────────────────────────────────────────
# The **Services ID** (e.g. "com.fourinathousand.web"), NOT the App ID: the web
# flow authenticates against a Services ID and Apple puts it in the token's
# `aud`. As with Google this is public (it ships in the AppleID.js init call) and
# is load-bearing for exactly the same reason -- verification compares `aud` to
# this exact value, and without that comparison an Apple ID token minted for any
# other application would authenticate here.
#
# Unlike Google there is NO client secret and no .p8 key needed, because the
# browser flow used here (AppleID.auth.signIn with usePopup) hands the page a
# signed id_token directly and the backend only verifies it. The private key is
# only required for the server-side authorization-code exchange, which this does
# not do.
#
# Empty disables Apple sign-in entirely (POST /auth/apple -> 503) and the button
# is not rendered. Setup, all of which is on Apple's side:
#   1. An Apple Developer Program membership (paid) and an App ID.
#   2. A Services ID with "Sign in with Apple" enabled, whose value goes here.
#   3. The site's domain registered on that Services ID, and the return URL
#      added -- Apple rejects the popup outright if the origin isn't listed.
APPLE_CLIENT_ID = os.getenv("APPLE_CLIENT_ID", "")

# Email + password sign-up. On by default: its whole purpose is to be the path
# that needs no third-party account, so requiring an env var to turn it on would
# leave the default deployment offering exactly the two providers it exists to
# supplement. Set EMAIL_SIGNUP_ENABLED=0 to hide the form and 503 the endpoints.
#
# There is deliberately NO verification email -- this deployment has no mail
# service of any kind. The consequence is recorded rather than hidden:
# User.email_verified stays False for these accounts and GET /admin/signups
# reports it, so an unverified address is never presented as a confirmed contact.
EMAIL_SIGNUP_ENABLED = os.getenv("EMAIL_SIGNUP_ENABLED", "1").lower() not in {"0", "false", "no"}
# Minimum password length. A length floor is the only password rule imposed:
# composition rules ("one number, one symbol") measurably push people toward
# shorter, more guessable passwords, and nothing here is protecting a payment
# method -- the account holds a CV and a list of job adverts.
PASSWORD_MIN_LENGTH = int(os.getenv("PASSWORD_MIN_LENGTH", "8"))

# Q1 of the post-signup survey (single select). Stored on SignupSurvey.priority
# as the raw slug; the labels live in the frontend so wording can change without
# a migration. Order is the display order.
SIGNUP_PRIORITY_CHOICES = [
    "ghost_roles",       # avoiding jobs that aren't really hiring
    "visa_sponsors",     # filtering to visa sponsors
    "faster",            # discovering ok roles faster
    "hard_to_find_fit",  # it's difficult to find roles that fit
    "niche",             # finding niche / lower-competition roles
    "none",              # none of these
]

# ── Open beta: the fixed test window ─────────────────────────────────────────
# Two SEPARATE gates off one clock (User.beta_started_at), and they must stay
# separate -- see services/beta.py:
#
#   * EXIT_SURVEY_AFTER_DAYS -- from this day on, the next time the user loads
#     the app they are held on /exit-survey until they answer. Answering
#     releases them back into the app for the rest of the window. Deliberately
#     NOT the last day: triggering on day 7 would require the user to log in on
#     one specific day, which is the single most likely way to collect nothing.
#   * BETA_WINDOW_DAYS -- access lapses (a real 403, see require_active_beta).
#
# Note what CANNOT enforce this: TOKEN_MAX_AGE_SECONDS above is 30 days, far
# longer than the window, so token expiry is no help and the check is its own.
BETA_WINDOW_DAYS = int(os.getenv("BETA_WINDOW_DAYS", "7"))
EXIT_SURVEY_AFTER_DAYS = int(os.getenv("EXIT_SURVEY_AFTER_DAYS", "4"))

# Q2 of the wrap-up survey: which features materially proved useful (multi
# select). "none" is NOT padding -- with a plain checkbox group, zero ticks is
# indistinguishable between "none of these were useful" (a real, valuable
# answer) and "hasn't answered yet", and the survey gate has to be able to tell
# when to release the user. Same reasoning as SIGNUP_PRIORITY_CHOICES' "none".
EXIT_SURVEY_FEATURE_CHOICES = [
    "ghost_check",        # ghost job checking
    "sponsor_check",      # visa sponsor checking
    "one_line_summary",   # the one-line role summary
    "why_qualified",      # the "why qualified" summary
    "none",               # none of these
]

# Q3 of the wrap-up survey: the speed/quality trade-off, asked assuming the
# current ~12 roles per run.
EXIT_SURVEY_SPEED_CHOICES = [
    "slower_better",  # wait longer for slightly better roles
    "as_is",          # keep it as-is
    "faster_worse",   # wait a lot less for slightly worse roles
]

# SQLite by default; flip DATABASE_URL to a postgres:// URL to migrate later.
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BACKEND_DIR / 'jobmatch.db'}")

# Frontend origin(s) allowed through CORS.
FRONTEND_ORIGINS = os.getenv(
    "FRONTEND_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",")

# Cost guard: max live searches PER USER, per calendar day (see
# routers/search.py::_searches_today, which counts a user's own runs only). It
# is not a global/shared pool -- one beta user cannot exhaust everyone's quota.
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
