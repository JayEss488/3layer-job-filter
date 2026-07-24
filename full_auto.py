#!/usr/bin/env python3
"""
Job Search Automation (Production Grade & Highly Flexible)
──────────────────────────────────────────────────────────
Phase 1  Dynamic profile & region extraction from exp.txt (cached in DB)
Phase 2  Board URL pattern detection                       (cached 30 days)
Phase 3  Parallel targeted scraping + embedding pre-filter
Phase 4  Cheap-model candidate ranking → top 10
Phase 5  Robust full-page scrape with anti-bot bypass & retries
Phase 6  Expensive-model final evaluation → top 3
Phase 7  Output to terminal + results.md + DB
"""

import asyncio
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, List, Dict
import numpy as np
import httpx
from openai import OpenAI
from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CrawlerRunConfig,
    DefaultMarkdownGenerator,
    PruningContentFilter,
)
import requests
from requests.auth import HTTPBasicAuth
from urllib.parse import urlsplit
# ── Add these near the top, after imports ──────────────────────────────────────
import queue
from crawl4ai import CacheMode

_log_queue: queue.Queue | None = None

def set_log_queue(q: queue.Queue):
    global _log_queue
    _log_queue = q

def emit(msg: str):
    """Prints to terminal and pushes to web queue if one is set. Falls back to an
    ASCII-safe encode on print() so a console whose stdout isn't UTF-8-capable
    (e.g. legacy cp1252, seen on some Windows launch paths) can't crash whatever
    phase is currently running just because a log line contains an emoji."""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode(sys.stdout.encoding or "ascii", errors="backslashreplace").decode(
            sys.stdout.encoding or "ascii", errors="replace"))
    if _log_queue is not None:
        _log_queue.put(msg)

# ── API KEYS ───────────────────────────────────────────────────────────────────
REED_API_KEY = os.getenv("REED_API_KEY", "")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
# serper.dev: a cheaper Google *organic* search API (no Google-Jobs engine). We
# prefer it over SerpAPI for the two organic-search jobs -- ATS-token discovery
# and Google-Jobs-style board discovery -- to stay clear of the SerpAPI credit
# limit. SerpAPI stays wired as a fallback where a key is present.
SERPER_DEV_API_KEY = os.getenv("SERPER_DEV_API_KEY", "")
# Serpent (apiserpent.com): a third Google-organic provider, kept behind the
# GOOGLE_SEARCH_PROVIDER switch so serper/serpent/serpapi can be A/B compared
# without touching code. NOTE: new Serpent accounts get only ~10 free Google
# searches -- do not point the whole pipeline at it casually.
SERPENT_API_KEY = os.getenv("SERPENT_API_KEY", "")
# Which provider fetch_google_jobs uses for its organic Google search. Default
# serper (cheap, lots of credits); "serpent" or "serpapi" for comparison.
GOOGLE_SEARCH_PROVIDER = os.getenv("GOOGLE_SEARCH_PROVIDER", "serper").strip().lower()
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
# ATS-token harvesting (harvest_ats_tokens) needs a domain-restricted search.
# serper.dev's free tier rejects `site:`-operator queries outright ("Query
# pattern not allowed for free accounts"), so by default it builds a plain
# keyword+domain query instead (lower precision, but it actually runs on a
# free plan). Flip this on once/if the serper.dev plan is upgraded to one that
# allows site: search, to get the precise query back with no other changes.
SERPER_SITE_OPERATOR_OK = os.getenv("SERPER_SITE_OPERATOR_OK", "false").strip().lower() == "true"
# Hard cap on (vendor, keyword) query combos per harvest_ats_tokens() call, so
# one profile edit can't burn through the whole serper/CSE credit balance.
ATS_HARVEST_MAX_QUERIES = int(os.getenv("ATS_HARVEST_MAX_QUERIES", "30"))
# Alternate-source lookup on scrape failure (see _find_alternate_posting): when a
# job's own link can't be scraped (dead link, click-tracking redirect, anti-bot
# block), search for the same posting elsewhere by title+company before giving up
# to a bare snippet -- mirrors how a human re-finds a broken listing. Costs one
# organic-search call per attempt, so capped per run like the ATS harvest above.
ALT_SOURCE_LOOKUP_MAX_PER_RUN = int(os.getenv("ALT_SOURCE_LOOKUP_MAX_PER_RUN", "15"))

# Reed full-description enrichment (see fetch_reed_details). Reed's SEARCH endpoint
# truncates jobDescription to ~455 chars -- a measured 361-row sample of one live
# profile's store had min 453 / max 500 -- which is the opening blurb and never the
# requirements section. That teaser is all screen_gate and rank_gate ever see for a
# freshly-discovered Reed job (full_text is only written by Phase 5, which runs
# AFTER both), so their GATE_LISTING_TEXT_CHARS/RANK_LISTING_TEXT_CHARS budgets
# were ceilings with nothing to fill them. Reed's per-JOB endpoint returns the
# whole description (measured avg ~3900 chars, 8.6x the teaser) for one cheap HTTP
# call -- no LLM, no browser. Capped per run because engine.py only enriches the
# candidates a run is actually about to examine, not the whole store.
REED_DETAIL_ENRICH_ENABLED = os.getenv("REED_DETAIL_ENRICH_ENABLED", "true").lower() == "true"
REED_DETAIL_MAX_PER_RUN = int(os.getenv("REED_DETAIL_MAX_PER_RUN", "150"))

# Pages fetched per (source, term) by gather_jobs' per-run discovery, cut from
# the fetchers' own pages=3 default (which the legacy standalone path keeps).
# One page still returns up to 100 (Reed) / 50 (Adzuna) results per term, and a
# measured live run discovered 7,800 raw listings of which only ~100 were ever
# examined past the embedding stage -- pages 2-3 were pure fetch latency. The
# fetchers emit a "page cap hit" note whenever the last page came back full, so
# the coverage trade-off stays visible per term.
REED_PAGES_PER_TERM = int(os.getenv("REED_PAGES_PER_TERM", "1"))
ADZUNA_PAGES_PER_TERM = int(os.getenv("ADZUNA_PAGES_PER_TERM", "1"))

DEBUG_SAVE_RAW = True

# ── Dynamic Path Configuration ──────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "boards_cache.db")

# Automatically discover CV file in current directory or fallback gracefully
CV_PATH = os.path.join(BASE_DIR, "exp.txt")
if not os.path.exists(CV_PATH):
    CV_PATH = "/home/jamesstephens/Documents/search_auto/exp.txt"

# ── Global Tuning Hyperparameters ──────────────────────────────────────────────
# Deliberately kept on the older/cheaper nano tier, not GPT-5.6 Luna -- nano is
# meaningfully cheaper still, and screen_gate's binary sector/seniority-style
# check doesn't need Luna's extra reasoning power. This is the highest-volume
# gate call, so keeping it on the cheapest viable model matters most here.
CHEAP_MODEL         = "gpt-5.4-nano-2026-03-17"
# Middle tier, used only where the cheap tier's coarseness is the limiting factor
# (currently just rank_gate's numeric fit scoring) -- screen_gate stays on
# CHEAP_MODEL since its binary sector/seniority check doesn't need the extra
# reasoning power, and this keeps the highest-volume gate call cheapest.
# Upgraded to GPT-5.6 Luna: rank_gate now also enforces RANK_REJECT_SCORE_FLOOR
# (engine.py), an absolute floor on its 0-100 fit score rather than just a
# relative ranking -- that's a materially heavier ask of this tier than before,
# so it's worth the step up from gpt-5.4-mini.
MID_MODEL           = "gpt-5.6-luna"
# Phase 6 final judge only (1-3 calls per run, the only exp-tier call in a live
# search), so a newer/stronger model here costs cents per run, not dollars --
# the highest-leverage place to spend more. Upgraded to GPT-5.6 Terra.
EXP_MODEL           = "gpt-5.6-terra"
EMBED_MODEL         = "text-embedding-3-small"
# The whole gpt-5.6 family (Luna, Terra) rejects any non-default temperature
# outright (400 Unsupported value) -- only the default (1) is accepted. Both
# rank_gate (MID_MODEL) and the Phase 6 final judge (EXP_MODEL) pass an explicit
# temperature and must check this rather than using their caller's default.
_FIXED_TEMPERATURE_MODELS = ("gpt-5.5", "gpt-5.6-luna", "gpt-5.6-terra")

PROFILE_CACHE_DAYS  = 7
MAX_CONCURRENT      = 5       # Max general simultaneous crawl requests
RELEVANCE_THRESHOLD = 0.35    # Balanced threshold preventing snippet penalty
TOP_CANDIDATES      = 25      # Pool size handed to the final evaluator
FINAL_PICKS         = 12      # Max results returned, quality-gated
FINAL_EVAL_MAX_JOBS_PER_CALL = 20  # cap per single Phase 6 prompt; larger
                                    # clusters split into concurrent batches
                                    # instead of one call risking the 90s
                                    # client read timeout (see client below).
                                    # Raised from 15 -> 20 (fewer, larger calls
                                    # for a JUDGE_POOL=40 run); a batch this
                                    # size eats more into that 90s budget than
                                    # 15 did, so watch for call_failed/timeout
                                    # fallbacks in practice if this proves too
                                    # aggressive.
# Per-job text budget in the Phase 6 prompt (see _final_eval_job_block). Up to
# 8000 chars of real scraped text are captured and persisted per job (see
# scrape_full_details), but the judge prompt used to hard-truncate to 2000 --
# well short of that, so requirements/location clarifications sitting later in
# a long posting were invisible to the judge even after a successful scrape.
# Raised 2000 -> 4000 -> now to the full 8000: a live-run audit of a real
# response showed every job's full_text still cut off mid-word/mid-sentence at
# 4000 (e.g. "...designing developing and maintaini", "...data quality, gov"),
# with the requirements section for at least one posting sitting entirely past
# the cut -- exactly the failure mode this budget exists to prevent, and the
# trigger the prior comment said to watch for. Now matches the full captured
# budget 1:1, so a job can only be truncated here if it was truncated at
# scrape time too. Up to JUDGE_POOL (40) jobs can land in one cluster's call,
# so this scales the single most expensive stage's prompt size/cost directly;
# FINAL_EVAL_MAX_JOBS_PER_CALL's chunk-splitting is the pressure valve on the
# per-call token/timeout budget, not this constant.
FINAL_EVAL_JOB_TEXT_CHARS = 8000

# screen_gate's per-listing text budget -- scales every screen_gate call's
# prompt size directly (batches _GATE_BATCH=20 listings per call), so this
# stays well short of FINAL_EVAL_JOB_TEXT_CHARS. Candidate dicts already carry
# full_text = r.full_text or r.snippet (engine.py's JobSeen->dict mapping), so
# a job resurfacing from a prior run's Phase-5 scrape gets its richer scraped
# text here for free; a brand-new job this run still falls back to its raw
# API snippet.
# Raised 900 -> 2000 after a live full_text audit of a real Reed.co.uk posting
# (see _best_markdown's docstring): even with fit_markdown's nav/boilerplate
# stripped, that posting's actual "About You" requirements section didn't start
# until character ~1980 of its full_text -- comfortably past the old 900-char
# window. At 900 chars, screen_gate saw only the title/salary/opening blurb for
# a real posting and misjudged listing_ok=false ("not a real single job
# posting") purely because so little of the actual content was visible, not
# because the text itself was ambiguous. 2000 chars comfortably covers a
# typical posting's full description + requirements bullets while staying an
# order of magnitude below FINAL_EVAL_JOB_TEXT_CHARS.
GATE_LISTING_TEXT_CHARS = 2000

# rank_gate's per-listing text budget -- deliberately larger than
# GATE_LISTING_TEXT_CHARS: unlike screen_gate (which runs on every discovered
# candidate), rank_gate only ever sees the much smaller post-gate survivor
# pool (see rank_gate's docstring), and its DEPTH FIT criterion specifically
# needs to read into the requirements/skills section of a posting, not just
# the title/opening blurb screen_gate's coarser checks can get by on. Same
# Reed.co.uk audit that motivated GATE_LISTING_TEXT_CHARS found the full core
# JD (title through the closing "What's on Offer" section) ran to ~2450
# characters before board "Similar Jobs" boilerplate started -- 3000 covers
# that with headroom while staying well short of the judge's full 8000-char
# budget.
RANK_LISTING_TEXT_CHARS = 3000

# Below this many available chars (and with no enriched/scraped full_text), a
# listing reaching rank_gate is tagged as a truncated source teaser in the
# prompt (see _score_rank_batch). Mirrors engine.py's SNIPPET_SUFFICIENT_CHARS
# reasoning: the Reed/Adzuna search APIs truncate descriptions at ~455-500
# chars, which is the opening blurb only -- the requirements section is simply
# not visible. The tag tells the rank model that explicitly, so its HARD
# DOWNGRADES (which require CLEAR visible evidence) don't fire on absence and
# its score leans on what actually IS visible instead of guessing at the rest.
RANK_TEASER_MARKER_CHARS = 600

# Category-page expansion (see expand_category_pages): Google-organic discovery
# has no caching, so this cost repeats every run that surfaces category hits,
# not once -- kept small and cheap (link-discovery only, no content scrape).
CATEGORY_EXPAND_MAX_PAGES           = 8     # cap on listing pages expanded per run
CATEGORY_EXPAND_MAX_LINKS_PER_PAGE  = 15    # cap on postings pulled from one page
CATEGORY_EXPAND_BUDGET_SECONDS      = 30.0  # wall-clock budget for the whole step

# The SDK default (httpx.Timeout(600, connect=5.0), max_retries=2) lets a single
# stalled call block up to ~30 min before raising anything -- with a search
# running as a background task with no outer watchdog, that stalls the whole
# pipeline at status="running" with no way to recover short of a restart.
client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"), timeout=httpx.Timeout(90.0, connect=5.0))


def run_pipeline(cv_text: str, log_queue: queue.Queue) -> list[dict]:
    set_log_queue(log_queue)

    with open(CV_PATH, "w", encoding="utf-8") as f:
        f.write(cv_text)

    conn = get_db()
    conn.execute("DELETE FROM profile_cache WHERE key='profile'")
    conn.execute("DELETE FROM profile_cache WHERE key='profile_embedding'")
    conn.commit()
    conn.close()

    results = asyncio.run(_pipeline_async())
    emit(f"__RESULTS__:{json.dumps(results)}")
    return results


def build_profile(cv_text: str, log_queue: queue.Queue) -> dict:
    """Phase 1 only — save CV, build and cache profile. Returns the profile dict."""
    set_log_queue(log_queue)

    with open(CV_PATH, "w", encoding="utf-8") as f:
        f.write(cv_text)

    # Bust only the profile rows — preserve card swipe memory (cards_* keys)
    init_db()
    conn = get_db()
    conn.execute("DELETE FROM profile_cache WHERE key='profile'")
    conn.execute("DELETE FROM profile_cache WHERE key='profile_embedding'")
    conn.commit()
    conn.close()

    profile = get_profile()
    emit(f"__PROFILE__:{json.dumps(profile)}")
    return profile


def run_search(log_queue: queue.Queue) -> list[dict]:
    """Phase 2–7 — assumes CV and profile are already cached. Returns final results."""
    set_log_queue(log_queue)

    if not os.path.exists(CV_PATH):
        emit("[!] No CV found. Please upload a CV first.")
        return []

    results = asyncio.run(_pipeline_async())
    emit(f"__RESULTS__:{json.dumps(results)}")
    return results

async def _pipeline_async() -> list[dict]:
    init_db()
    profile = get_profile()
    profile_embedding = get_profile_embedding(profile)
    raw_jobs = gather_jobs(profile)
    # This standalone CLI path doesn't wire up category-page expansion (that
    # lives in engine.py, the backend's pipeline) -- drop tagged category-page
    # pseudo-rows here so they don't get treated as real candidates.
    raw_jobs = [j for j in raw_jobs if not j.get("_is_category_page")]

    seen, deduped = set(), []
    for job in raw_jobs:
        key = (job["title"].lower(), job["company"].lower())
        if key not in seen:
            seen.add(key)
            deduped.append(job)

    candidates = api_candidates(deduped, profile_embedding)
    if not candidates:
        emit("[!] No candidates passed embedding filter.")
        return []

    browser_config = BrowserConfig(headless=True, verbose=False,
        viewport_width=1280, viewport_height=800, user_agent_mode="random")

    async with AsyncWebCrawler(config=browser_config) as crawler:
        top10 = rank_candidates(candidates, profile)
        top10 = await scrape_full_details(top10, crawler)

    return final_evaluation(top10, profile)

# ── Location Normalization Guardrails ──────────────────────────────────────────
def normalize_location(location_str: str) -> str:
    """Cleans up common shorthand geographical inputs for strict APIs."""
    loc = location_str.strip().upper()
    if loc in ["UK", "U.K.", "GREAT BRITAIN", "GB"]:
        return "United Kingdom"
    if loc in ["US", "U.S.", "USA", "UNITED STATES OF AMERICA"]:
        return "United States"
    return location_str.strip()


# Token sets used to positively identify which of the supported countries a
# free-text job location string belongs to. Keyed by the 2-letter Adzuna code
# used throughout the engine/snapshot. Kept intentionally small/high-signal:
# false negatives (returning None) are safe, false positives are not.
_COUNTRY_TOKENS = {
    "gb": {"united kingdom", "uk", "u.k.", "great britain", "england", "scotland",
           "wales", "northern ireland", "london", "manchester", "birmingham",
           "leeds", "glasgow", "edinburgh", "bristol", "liverpool", "sheffield",
           "newcastle", "nottingham", "leicester", "coventry", "cardiff", "belfast",
           "cambridge", "oxford", "reading", "brighton", "aberdeen", "dundee",
           "southampton", "portsmouth", "milton keynes", "essex", "kent", "surrey",
           "sussex", "hampshire", "yorkshire", "lancashire", "cheshire", "devon",
           "cornwall", "southend", "southend-on-sea"},
    "us": {"united states", "usa", "u.s.", "u.s.a.", "america", "new york",
           "san francisco", "los angeles", "chicago", "seattle", "austin",
           "boston", "texas", "california", "florida", "washington", "denver",
           "atlanta", "dallas", "houston", "san diego", "philadelphia"},
    "ca": {"canada", "toronto", "vancouver", "montreal", "ottawa", "calgary"},
    "au": {"australia", "sydney", "melbourne", "brisbane", "perth"},
    "de": {"germany", "deutschland", "berlin", "munich", "munchen", "hamburg", "frankfurt"},
    "fr": {"france", "paris", "lyon", "marseille"},
    "in": {"india", "bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune"},
    "it": {"italy", "italia", "rome", "milan", "turin"},
    "nl": {"netherlands", "holland", "amsterdam", "rotterdam", "the hague"},
    "at": {"austria", "vienna"},
    "pl": {"poland", "warsaw", "krakow", "wroclaw"},
    "sg": {"singapore"},
    "za": {"south africa", "johannesburg", "cape town", "pretoria"},
}


def country_of(job_location: str) -> str | None:
    """Best-effort country code for a free-text job location, or None if it
    can't be confidently determined (ambiguous/blank/unrecognised)."""
    loc = (job_location or "").strip().lower()
    if not loc:
        return None
    for code, tokens in _COUNTRY_TOKENS.items():
        if any(tok in loc for tok in tokens):
            return code
    return None


# ── Paid-"training"/placement-scheme detection ─────────────────────────────────
# A recurring class of non-jobs (especially on Reed): a training provider posts a
# "Trainee X" ad that is really a paid course / placement programme, not a
# vacancy -- the candidate pays (or finances) a course and is only promised a
# "job guarantee" or interview afterwards. These must be hard-dropped: there is
# nothing here the candidate can simply apply to and be hired for. Kept
# deliberately high-precision -- a genuine "Trainee Accountant" at a real
# employer must survive -- so "trainee" in the title alone is NOT a signal; we
# key on the scheme/fee/guarantee language these ads share and real vacancies
# almost never use.
# Strong phrases: course/scheme language a genuine vacancy essentially never
# uses. Any one of these alone is enough to drop the listing.
_TRAINING_STRONG = (
    "traineeship", "job guarantee", "guaranteed job", "guaranteed interview",
    "interview guarantee", "course fee", "course fees", "tuition fee", "fees apply",
    "fees back", "self-funded", "self funded", "finance options", "flexible payment",
    "money back guarantee", "we help place graduates", "placement programme",
    "placement program", "career programme", "job programme", "future-proof your career",
    "upon completion of the course", "once you complete the course",
    "on completion of the programme",
)
# Soft signals: common enough in genuine entry-level ads (care, hospitality,
# retail) that they only count when paired with a training-provider context.
_TRAINING_WEAK = (
    "no experience required", "no experience needed", "no previous experience",
    "full training will be provided", "full training provided", "career change",
    "career changers", "career switch", "fresh start", "kick-start a new career",
    "kickstart your career", "kick start your career",
)
_TRAINING_COMPANY_RE = re.compile(
    r"\b(training|academy|bootcamp|boot camp|career switch|careers? academy|"
    r"upskill|reskill|coding school|skills? academy)\b", re.I)


def looks_like_training_scheme(title: str, company: str, text: str) -> bool:
    """True if a listing is a paid training course / placement scheme rather than
    a real job vacancy (see note above). High precision by design: any one strong
    phrase, OR one soft signal in a training-provider context (a provider-named
    company, or a "Trainee ..." title). A bare "Trainee X" title on its own is
    NOT enough -- a genuine trainee vacancy at a real employer must survive."""
    blob = f"{title or ''} {company or ''} {text or ''}".lower()
    if any(s in blob for s in _TRAINING_STRONG):
        return True
    weak = sum(1 for w in _TRAINING_WEAK if w in blob)
    provider = bool(_TRAINING_COMPANY_RE.search(company or "")) or "trainee" in (title or "").lower()
    return provider and weak >= 1


# ── Flexible API Fetchers ──────────────────────────────────────────────────────

def fetch_reed(query: str, location: str = "United Kingdom", country_code: str = "gb", pages: int = 3) -> List[Dict]:
    """Reed is UK-only. Skips execution if the profile's resolved country isn't GB.
    `location` is the locationName Reed scopes the search to; an empty string
    (national scope) means UK-wide -- omit locationName rather than passing a
    country name, which Reed's geocoder would reject (same failure mode Adzuna
    documents). A real city/postcode is passed through for a 'local' scope."""
    if (country_code or "gb").strip().lower() != "gb" or not REED_API_KEY:
        return []

    url = "https://www.reed.co.uk/api/1.0/search"
    page_size = 100  # Reed's max resultsToTake
    jobs: List[Dict] = []
    last_page_full = False
    for page in range(pages):
        params = {"keywords": query,
                  "resultsToTake": page_size, "resultsToSkip": page * page_size}
        if location and location.strip():
            params["locationName"] = location
        try:
            r = requests.get(url, params=params, auth=HTTPBasicAuth(REED_API_KEY, ""), timeout=12)
            results = r.json().get("results", [])
        except Exception as e:
            emit(f"   [!] Reed API Error: {e}")
            last_page_full = False
            break
        for job in results:
            jobs.append({
                "board": "reed",
                "title": job.get("jobTitle", ""),
                "company": job.get("employerName", ""),
                "url": job.get("jobUrl", ""),
                "location": job.get("locationName", ""),
                "salary_min": job.get("minimumSalary"),
                "salary_max": job.get("maximumSalary"),
                "snippet": job.get("jobDescription", "")
            })
        last_page_full = len(results) >= page_size
        if len(results) < page_size:  # last page reached
            break
    if last_page_full:
        emit(f"   [reed] '{query}': page cap ({pages}) hit with a full page -- more results likely available")
    return jobs


# Reed's jobUrl always ends in the numeric jobId
# (https://www.reed.co.uk/jobs/data-scientist/57069135). Parsing it back out here
# avoids widening fetch_reed's return shape and the jobs_seen schema just to carry
# an id the URL already encodes.
_REED_JOB_ID_RE = re.compile(r"reed\.co\.uk/jobs/[^/]+/(\d+)", re.I)


def reed_job_id(url: str) -> str | None:
    m = _REED_JOB_ID_RE.search(url or "")
    return m.group(1) if m else None


def fetch_reed_details(job_ids: List[str]) -> Dict[str, str]:
    """jobId -> full plain-text job description, for the ids that resolved.

    Reed's search response truncates jobDescription to a ~455-char teaser; this
    per-job endpoint returns the whole thing (see REED_DETAIL_ENRICH_ENABLED for
    why that matters). One cheap HTTP call each, fanned out over the same
    12-wide pool gather_jobs uses -- a measured 24-id batch resolved in 1.7s.

    Fails SOFT and per-id: a 404/410 (listing pulled), a timeout, or an empty
    description simply doesn't appear in the returned dict, and the caller keeps
    whatever text it already had. Enrichment that can't be done is never a reason
    to lose a candidate."""
    ids = [j for j in dict.fromkeys(job_ids) if j][:REED_DETAIL_MAX_PER_RUN]
    if not ids or not REED_API_KEY or not REED_DETAIL_ENRICH_ENABLED:
        return {}

    def _one(job_id: str) -> tuple[str, str]:
        try:
            r = requests.get(f"https://www.reed.co.uk/api/1.0/jobs/{job_id}",
                             auth=HTTPBasicAuth(REED_API_KEY, ""), timeout=12)
            if r.status_code != 200:
                return job_id, ""
            return job_id, _strip_html(r.json().get("jobDescription") or "")
        except Exception:
            return job_id, ""

    with ThreadPoolExecutor(max_workers=12) as ex:
        out = {job_id: text for job_id, text in ex.map(_one, ids) if text}
    emit(f"   [reed] full descriptions fetched for {len(out)}/{len(ids)} listing(s)")
    return out


# Country-level location strings that Adzuna's `where` geocoder rejects (the cc
# endpoint already scopes the country, so these must be omitted, not passed).
_ADZUNA_COUNTRY_LEVEL = {
    "united kingdom", "great britain", "uk", "u.k.", "gb",
    "united states", "united states of america", "usa", "us", "u.s.",
    "canada", "australia", "germany", "france", "india", "italy",
    "netherlands", "austria", "poland", "singapore", "south africa",
}


_ADZUNA_PAREN_RE = re.compile(r"\([^)]*\)")


def _sanitize_adzuna_term(term: str) -> str:
    """Adzuna's `what` param AND-matches every word, so a long/punctuated search
    term (parenthetical asides, slashes) tends to zero out even when Reed handles
    the identical raw string fine. Strip parenthetical clauses, collapse stray
    punctuation/whitespace, and cap to 3 words -- keeping the LAST 3 rather than
    the first 3, since English job titles put modifiers first and the core role
    noun last ("Junior Policy / Research Assistant" -> "Policy Research
    Assistant", not "Junior Policy Research"); the first-3 cap was systematically
    dropping the most identifying word and zeroing out otherwise-findable terms."""
    cleaned = _ADZUNA_PAREN_RE.sub("", term)
    cleaned = re.sub(r"[\/,]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return " ".join(cleaned.split(" ")[-3:]) or term


def fetch_adzuna(query: str, location: str = "United Kingdom", country_code: str = "gb", pages: int = 3) -> List[Dict]:
    """Routes dynamically to the matching Adzuna global regional server. The page
    number is the last path segment (/search/{page}), so pagination just walks it."""
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []
    query = _sanitize_adzuna_term(query)

    # Ensure clean lowercase ISO code string (default to 'gb')
    cc = country_code.strip().lower() if country_code else "gb"

    base_params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": query,
        "results_per_page": 50,
    }
    # The cc endpoint already scopes the country. Passing a country *name* as
    # `where` makes Adzuna's geocoder return nothing (e.g. "United Kingdom" -> 0
    # results), so only set `where` for a sub-country location (a city/region).
    # Adzuna's geocoder also chokes on a trailing postcode fragment (e.g.
    # "Bristol, BS6") -- pass just the city/region component.
    if location and location.strip().lower() not in _ADZUNA_COUNTRY_LEVEL:
        base_params["where"] = location.split(",")[0].strip()

    jobs: List[Dict] = []
    last_page_full = False
    for page in range(1, pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}"
        try:
            r = requests.get(url, params=base_params, timeout=12)
            body = r.json()
            results = body.get("results", [])
        except Exception as e:
            emit(f"   [!] Adzuna ({cc}) API Error: {e}")
            last_page_full = False
            break
        for job in results:
            jobs.append({
                "board": "adzuna",
                "title": job.get("title", ""),
                "company": job.get("company", {}).get("display_name", ""),
                "url": job.get("redirect_url", ""),
                "location": (job.get("location") or {}).get("display_name", ""),
                "salary_min": job.get("salary_min"),
                "salary_max": job.get("salary_max"),
                "snippet": job.get("description", "")
            })
        if page == 1 and not results:
            emit(f"   [!] Adzuna ({cc}) returned 0 results for '{query}' "
                 f"(where={base_params.get('where', '<none>')}): {body.get('exception') or body.get('error') or 'no error field'}")
        last_page_full = len(results) >= 50
        if len(results) < 50:  # last page reached
            break
    if last_page_full:
        emit(f"   [adzuna] '{query}': page cap ({pages}) hit with a full page -- more results likely available")
    return jobs


def fetch_remotive(query: str) -> List[Dict]:
    """Queries global remote listings filtered cleanly by search term."""
    url = f"https://remotive.com/api/remote-jobs?search={query}&limit=50"
    try:
        r = requests.get(url, timeout=12)
        jobs = []
        for job in r.json().get("jobs", []):
            jobs.append({
                "board": "remotive",
                "title": job.get("title", ""),
                "company": job.get("company_name", ""),
                "url": job.get("url", ""),
                # Remotive is remote-first; candidate_required_location is a
                # geographic eligibility string (e.g. "USA Only", "Worldwide").
                "location": job.get("candidate_required_location") or "Remote",
                "snippet": job.get("description", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] Remotive API Error: {e}")
        return []


def _serper_search_raw(query: str, gl: str = "gb", location: str | None = None,
                        num: int = 10) -> tuple[List[Dict], bool]:
    """Same request as _serper_search, but also reports whether serper refused
    the query pattern itself (e.g. a free-tier `site:` block) as distinct from
    a normal zero-result search -- callers that issue many queries in a row
    (harvest_ats_tokens) use this to stop early instead of repeating the same
    rejection dozens of times."""
    if not SERPER_DEV_API_KEY:
        return [], False
    body = {"q": query, "gl": gl, "num": num}
    if location:
        body["location"] = location
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_DEV_API_KEY, "Content-Type": "application/json"},
            data=json.dumps(body), timeout=12,
        )
        data = r.json()
    except Exception as e:
        emit(f"   [!] serper.dev search error ({query!r}): {e}")
        return [], False
    if isinstance(data, dict) and data.get("message") and not data.get("organic"):
        emit(f"   [!] serper.dev: {data.get('message')}")
        return [], True
    return (data.get("organic", []) if isinstance(data, dict) else []), False


def _serper_search(query: str, gl: str = "gb", location: str | None = None,
                   num: int = 10) -> List[Dict]:
    """One organic Google search via serper.dev. Returns the raw `organic`
    result list (each: title/link/snippet/position). [] on any error or when no
    key is set. serper.dev has no Google-Jobs engine -- this is web search."""
    results, _rejected = _serper_search_raw(query, gl, location, num)
    return results


def _serpent_search(query: str, country: str = "gb", num: int = 10) -> List[Dict]:
    """One organic Google search via Serpent (apiserpent.com) Quick Search.
    Returns normalized [{title, link, snippet}] (Serpent uses `url`; mapped to
    `link` to match _serper_search). [] on error / no key. NOTE: free Serpent
    accounts get only ~10 Google searches -- use sparingly."""
    if not SERPENT_API_KEY:
        return []
    try:
        r = requests.get(
            "https://apiserpent.com/api/search/quick",
            headers={"X-API-Key": SERPENT_API_KEY},
            params={"q": query, "engine": "google", "country": country, "num": num},
            timeout=15,
        )
        data = r.json()
    except Exception as e:
        emit(f"   [!] serpent search error ({query!r}): {e}")
        return []
    if not (isinstance(data, dict) and data.get("success")):
        msg = (data.get("error") or data.get("message")) if isinstance(data, dict) else data
        emit(f"   [!] serpent: {msg}")
        return []
    organic = (data.get("results") or {}).get("organic", []) or []
    return [{"title": o.get("title", ""), "link": o.get("url", ""),
             "snippet": o.get("snippet", "")} for o in organic]


def _google_organic(query: str, gl: str = "gb", location: str | None = None,
                    num: int = 10) -> List[Dict]:
    """Dispatch one organic Google search to the provider named by
    GOOGLE_SEARCH_PROVIDER (serper default; serpent/serpapi opt-in), so the
    three can be A/B compared by flipping one env var. Returns normalized
    [{title, link, snippet}]. Falls back to whichever organic key exists when the
    selected provider has none."""
    if GOOGLE_SEARCH_PROVIDER == "serpent" and SERPENT_API_KEY:
        return _serpent_search(query, country=gl, num=num)
    if GOOGLE_SEARCH_PROVIDER == "serper" and SERPER_DEV_API_KEY:
        return _serper_search(query, gl=gl, location=location, num=num)
    if SERPER_DEV_API_KEY:
        return _serper_search(query, gl=gl, location=location, num=num)
    if SERPENT_API_KEY:
        return _serpent_search(query, country=gl, num=num)
    return []


# Aggregator/listing domains whose organic results are mostly index/search
# pages rather than a single posting -- and which the funnel already covers via
# Adzuna/Reed/Remotive. Google's unique value here is the ATS / custom-career /
# niche-board tier (charityjob, jobs.ac.uk, company sites), so these are dropped
# by registrable-domain suffix match. Wikipedia et al. are just noise.
_ORGANIC_SKIP_DOMAINS = (
    "indeed.com", "linkedin.com", "glassdoor.com", "glassdoor.co.uk",
    "ziprecruiter.com", "totaljobs.com", "reed.co.uk", "simplyhired.co.uk",
    "simplyhired.com", "cwjobs.co.uk", "milkround.com", "jobsite.co.uk",
    "wikipedia.org",
)


def _skip_organic_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _ORGANIC_SKIP_DOMAINS)


# Category/search-listing pages on boards we DO want individual postings from
# (charityjob, itjobswatch, jobs.ac.uk, etc.) -- _skip_organic_host only blocks
# whole aggregator domains, so a niche board's own "N jobs matching X" page
# still gets through that check and was observed landing in the discovery store
# as if it were a posting (e.g. "charityjob.co.uk/research-assistant-jobs-in-
# london", "itjobswatch.co.uk/find/Conversational-AI-jobs-in-England").
_CATEGORY_URL_RE = re.compile(
    r"/(?:find|search|categor(?:y|ies))/|-jobs-in-[a-z0-9-]+(?:/|$)|/jobs-in-[a-z0-9-]+(?:/|$)",
    re.I,
)
_CATEGORY_TITLE_RE = re.compile(r"^\s*\d[\d,]*\+?\s+.{0,60}\bjobs\b", re.I)  # "53 ... Jobs in England"
# A bare ".../something-jobs" or ".../jobs" path suffix is another common
# category-page shape, but risks colliding with a real posting's URL on an
# unusual ATS/career page -- only applied to hosts actually known to do this,
# not globally.
_CATEGORY_HOST_HINTS = ("charityjob.co.uk", "itjobswatch.co.uk", "jobs.ac.uk")
_CATEGORY_URL_SUFFIX_RE = re.compile(r"-jobs/?$|/jobs/?$", re.I)


def _looks_like_category_page(title: str, url: str, host: str) -> bool:
    if _CATEGORY_TITLE_RE.search(title or ""):
        return True
    try:
        path = urlsplit(url).path.lower()
    except ValueError:
        return False
    if _CATEGORY_URL_RE.search(path):
        return True
    if any(host == d or host.endswith("." + d) for d in _CATEGORY_HOST_HINTS):
        return bool(_CATEGORY_URL_SUFFIX_RE.search(path))
    return False


# ── Category-page link filtering (used by expand_category_pages) ───────────
# Segments that mark a link as site nav/boilerplate rather than a specific
# posting, wherever they appear in the path (so both /about and /company/about
# are excluded, but /careers/12345-engineer is NOT, since "careers"/"jobs" is
# a legitimate directory prefix for real postings, not a slug by itself).
_LINK_BOILERPLATE_SEGMENTS = {
    "about", "about-us", "contact", "contact-us", "privacy", "privacy-policy",
    "login", "signin", "sign-in", "register", "faq", "faqs", "blog", "press",
    "news", "terms", "terms-of-service", "cookie-policy", "cookies", "sitemap",
    "rss", "help", "support", "legal", "accessibility",
}
# A path whose ONLY segment is one of these is the listing/landing page itself
# (e.g. "/jobs", "/careers"), not an individual posting.
_LINK_BARE_LANDING_SEGMENTS = {"jobs", "careers", "job", "career",
                                "openings", "opportunities", "vacancies"}
_GENERIC_ANCHOR_RE = re.compile(
    r"^\s*(home|jobs?|careers?|search|view all|see more|next|previous|"
    r"apply( now)?|read more|learn more|sign in|log in|register|menu|"
    r"skip to content)\s*$", re.I,
)
_LINK_MIN_ANCHOR_CHARS = 8


def _is_boilerplate_or_bare_link(path: str) -> bool:
    segs = [s for s in (path or "").lower().strip("/").split("/") if s]
    if not segs:
        return True  # bare "/" -> homepage
    if len(segs) == 1 and segs[0] in _LINK_BARE_LANDING_SEGMENTS:
        return True
    return bool(set(segs) & _LINK_BOILERPLATE_SEGMENTS)


def _same_or_related_host(child_host: str, page_host: str) -> bool:
    child_host, page_host = (child_host or "").lower(), (page_host or "").lower()
    if not child_host or not page_host:
        return False
    return (child_host == page_host
            or child_host.endswith("." + page_host)
            or page_host.endswith("." + child_host))


def _posting_link_reject_reason(link, page_host: str) -> str | None:
    """Same filtering logic as _is_plausible_posting_link, but returns WHY a
    link was rejected (or None if it's plausible) so expand_category_pages can
    tally where a page's links are actually being lost -- a 0-yield category
    page could mean crawl4ai found no internal links at all, or it found
    plenty and every one of them got filtered out here, and those two cases
    need different fixes."""
    href = (getattr(link, "href", "") or "").strip()
    if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
        return "non_http_or_empty"
    try:
        parts = urlsplit(href)
    except ValueError:
        return "unparseable_url"
    if parts.scheme not in ("http", "https", ""):
        return "non_http_or_empty"
    path = parts.path.lower()
    if _is_boilerplate_or_bare_link(path):
        return "boilerplate_or_bare"
    child_host = (parts.netloc or page_host).lower().split(":")[0]
    if not _same_or_related_host(child_host, page_host):
        return "cross_host"
    text = re.sub(r"\s+", " ", (getattr(link, "text", "") or getattr(link, "title", "") or "").strip())
    if len(text) < _LINK_MIN_ANCHOR_CHARS or _GENERIC_ANCHOR_RE.match(text):
        return "short_or_generic_anchor"
    # Reject a child link that is itself another category/listing page (e.g. a
    # "Jobs in Manchester" sub-filter link, or page-2 pagination with
    # real-looking anchor text) -- reuses the existing detector instead of
    # inventing a second heuristic.
    if _looks_like_category_page(text, href, child_host):
        return "looks_like_category_page"
    return None


def _is_plausible_posting_link(link, page_host: str) -> bool:
    """Filters crawl4ai's raw result.links.internal down to links that look
    like individual job postings, not nav/boilerplate/pagination/other
    category pages."""
    return _posting_link_reject_reason(link, page_host) is None


def _fetch_google_jobs_serpapi(query: str, clean_loc: str, pages: int) -> List[Dict]:
    """SerpAPI's *structured* Google-Jobs engine (jobs_results). Used only when
    serpapi is the selected provider or the only key present."""
    if not SERPAPI_KEY:
        return []
    jobs: List[Dict] = []
    token = None
    for _ in range(pages):
        params = {"engine": "google_jobs", "q": query, "location": clean_loc, "api_key": SERPAPI_KEY}
        if token:
            params["next_page_token"] = token
        try:
            r = requests.get("https://serpapi.com/search", params=params, timeout=12)
            data = r.json()
        except Exception as e:
            emit(f"   [!] Google Jobs API Error: {e}")
            break
        if "error" in data:
            emit(f"   [!] SerpAPI Error: {data['error']}")
            break

        for job in data.get("jobs_results", []):
            apply_options = job.get("apply_options") or []
            url = apply_options[0].get("link") if apply_options else ""
            jobs.append({
                "board": "google_jobs",
                "title": job.get("title", ""),
                "company": job.get("company_name", ""),
                "url": url,
                "location": job.get("location", ""),
                "snippet": job.get("description", "")
            })

        token = (data.get("serpapi_pagination") or {}).get("next_page_token")
        if not token:
            break
    return jobs


def fetch_google_jobs(query: str, location: str = "United Kingdom", pages: int = 3) -> List[Dict]:
    """Board-discovery via Google. Default path is organic search through
    GOOGLE_SEARCH_PROVIDER (serper/serpent) -- cheap and avoids the SerpAPI limit;
    SerpAPI's structured Google-Jobs engine is used when it's the selected
    provider or the only key present. Broad-tier: reaches the Workday /
    SmartRecruiters / custom-career-page / niche-board (e.g. CharityJob) tier we
    can't integrate directly, by reading whatever Google indexes."""
    clean_loc = normalize_location(location)

    # Structured SerpAPI path: only when explicitly chosen or the sole key.
    if GOOGLE_SEARCH_PROVIDER == "serpapi" or (
            SERPAPI_KEY and not (SERPER_DEV_API_KEY or SERPENT_API_KEY)):
        return _fetch_google_jobs_serpapi(query, clean_loc, pages)

    gl = country_of(location) or "gb"
    results = _google_organic(f"{query} jobs {clean_loc}", gl=gl, location=clean_loc,
                              num=pages * 10 if pages else 10)
    jobs: List[Dict] = []
    n_category = 0
    for res in results:
        link = res.get("link", "")
        if not link:
            continue
        try:
            host = urlsplit(link).netloc.lower()
        except ValueError:
            host = ""
        if _skip_organic_host(host):
            continue
        raw_title = res.get("title", "")
        if _looks_like_category_page(raw_title, link, host):
            n_category += 1
            # Followed up rather than dropped: engine.py partitions these out
            # by the tag below and hands them to expand_category_pages, which
            # crawls the page and extracts individual postings from it.
            jobs.append({
                "board": "google_jobs", "title": raw_title, "company": "",
                "url": link, "location": clean_loc, "snippet": res.get("snippet", ""),
                "_is_category_page": True, "_category_host": host,
            })
            continue
        # Organic titles are often "Role - Company | Board" -- keep the role
        # part; company is unknown here (the final scrape hydrates the page).
        title = re.split(r"\s[-–|]\s", raw_title, 1)[0].strip()
        jobs.append({
            "board": "google_jobs", "title": title or raw_title,
            "company": "", "url": link, "location": clean_loc,
            "snippet": res.get("snippet", ""),
        })
    if n_category:
        emit(f"   [google_jobs] found {n_category} board category/search-listing page(s) "
             f"(not individual postings) -- queued for expansion")
    return jobs


def fetch_jsearch(query: str, location: str = "United Kingdom") -> List[Dict]:
    """Fetches high-density results using the JSearch endpoint structure."""
    if not RAPIDAPI_KEY:
        return []
        
    clean_loc = normalize_location(location)
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    params = {"query": f"{query} in {clean_loc}", "page": 1, "num_pages": 1}

    # RapidAPI retired /search in favour of /search-v2 (jobs now nested under
    # data.jobs instead of data directly) -- same field names. /search-v2 runs
    # noticeably slower than the old endpoint, so a single fixed 12s timeout
    # was dropping this source's results outright on ordinary slow responses,
    # not just real outages. Retry once with a longer timeout before giving up.
    data = None
    for attempt, timeout in enumerate((15, 25), start=1):
        try:
            r = requests.get("https://jsearch.p.rapidapi.com/search-v2", headers=headers,
                              params=params, timeout=timeout)
            data = r.json()
            break
        except requests.exceptions.Timeout:
            if attempt == 2:
                emit(f"   [!] JSearch API Error: timed out after {attempt} attempts")
                return []
            emit(f"   [!] JSearch API timeout (attempt {attempt}/2), retrying with a longer timeout...")
        except Exception as e:
            emit(f"   [!] JSearch API Error: {e}")
            return []

    try:
        if data.get("status") != "OK":
            emit(f"   [!] JSearch API Error: {data.get('message') or data}")
            return []
        jobs = []
        for job in data.get("data", {}).get("jobs", []):
            loc = ", ".join(x for x in [job.get("job_city"), job.get("job_state"),
                                        job.get("job_country")] if x)
            jobs.append({
                "board": "jsearch",
                "title": job.get("job_title", ""),
                "company": job.get("employer_name", ""),
                "url": job.get("job_apply_link", ""),
                "location": "Remote" if job.get("job_is_remote") else loc,
                "salary_min": job.get("job_min_salary"),
                "salary_max": job.get("job_max_salary"),
                "snippet": job.get("job_description", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] JSearch API Error: {e}")
        return []


# ── Source protocol + tiers ──────────────────────────────────────────────────
# "fast" = cheap, text-complete, safe for the first (latency-sensitive) run.
# "broad" = slower / credit-heavy; only included in the rotation on later runs.
from typing import Protocol


class JobSource(Protocol):
    name: str
    tier: str
    def fetch(self, profile: dict, since: Optional[datetime]) -> List[Dict]: ...


def _terms(profile: Dict) -> List[str]:
    """The term(s) this run should query. select_sources_for_run fills
    search_terms_batch: several terms on the first run (fast tier only), a
    single rotated term on later runs (keeps broad-tier credit use bounded)."""
    return [t for t in (profile.get("search_terms_batch") or []) if t]


# 2-letter code -> display name, for geo-scoped sources that need a *non-empty*
# location string (Google/JSearch). Adzuna/Reed instead take an empty string.
_CC_DISPLAY = {
    "gb": "United Kingdom", "us": "United States", "ca": "Canada", "au": "Australia",
    "de": "Germany", "fr": "France", "in": "India", "it": "Italy", "nl": "Netherlands",
    "at": "Austria", "pl": "Poland", "sg": "Singapore", "za": "South Africa",
}


def _geo_scoped_location(profile: Dict) -> str:
    """Location to hand the country-scoped board APIs (Adzuna/Reed). ONLY a
    'local' scope should narrow to the candidate's city -- for national /
    international, passing a city as Adzuna's `where` / Reed's `locationName`
    locks the whole search to that one town (the root cause of Adzuna returning
    only hyper-local jobs that never match a national/remote target role). Return
    '' for national/international so the country endpoint searches the whole country."""
    scope = (profile.get("location_scope") or "national").lower()
    if scope == "local":
        return profile.get("local_place") or profile.get("location") or ""
    return ""


def _google_location(profile: Dict) -> str:
    """Like _geo_scoped_location, but for sources that need a non-empty location
    string (Google organic / JSearch): city for local, else the country display
    name so the search stays country-wide instead of city-locked."""
    scope = (profile.get("location_scope") or "national").lower()
    if scope == "local":
        return profile.get("local_place") or profile.get("location") or ""
    cc = (profile.get("adzuna_country_code") or "gb").lower()
    return _CC_DISPLAY.get(cc, "United Kingdom")


# Each source's fetch() is a plain sequential loop over fetch_term(), so the
# JobSource protocol (and the legacy standalone path that calls fetch())
# behaves exactly as before -- but gather_jobs fans the per-term calls out as
# individual pool tasks instead (see its task construction), which is what
# turned e.g. Reed's 6-terms-x-3-pages = 18 sequential HTTP calls in a single
# pool slot into 6 concurrent one-page calls. Reed/Adzuna pass the
# *_PAGES_PER_TERM caps explicitly; the fetchers' own pages=3 defaults are the
# legacy path's behavior and stay untouched.
class ReedSource:
    name, tier = "reed", "fast"
    def fetch_term(self, profile, term):
        return fetch_reed(term, _geo_scoped_location(profile),
                          profile.get("adzuna_country_code", "gb"),
                          pages=REED_PAGES_PER_TERM)
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(self.fetch_term(profile, term))
        return out


class AdzunaSource:
    name, tier = "adzuna", "fast"
    def fetch_term(self, profile, term):
        return fetch_adzuna(term, _geo_scoped_location(profile),
                            profile.get("adzuna_country_code", "gb"),
                            pages=ADZUNA_PAGES_PER_TERM)
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(self.fetch_term(profile, term))
        return out


class GoogleJobsSource:
    name, tier = "google_jobs", "broad"   # organic discovery via serper.dev/SerpAPI
    def fetch_term(self, profile, term):
        return fetch_google_jobs(term, _google_location(profile))
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(self.fetch_term(profile, term))
        return out


class JSearchSource:
    name, tier = "jsearch", "broad"
    def fetch_term(self, profile, term):
        return fetch_jsearch(term, _google_location(profile))
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(self.fetch_term(profile, term))
        return out


class RemotiveSource:
    name, tier = "remotive", "broad"
    def fetch_term(self, profile, term):
        return fetch_remotive(term)
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(self.fetch_term(profile, term))
        return out


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    """Plain-text a snippet that may carry raw markup (Greenhouse/Workable/
    Recruitee return HTML in their description field). Idempotent on
    already-plain text, so it's safe to apply uniformly across every ATS
    vendor rather than special-casing which ones are actually HTML."""
    if not text:
        return ""
    no_tags = _HTML_TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


# ── ATS feeds ─────────────────────────────────────────────────────────────────
# Public, no-auth JSON endpoints returning every open role at a company, with
# clean fields and updated_at. "fast" tier: free, fast, full description - roles
# sourced here never need scraping.
ATS_FEEDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
    "lever":      "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby":      "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
    # Same public, no-auth pattern, but these platforms skew toward the startups,
    # scale-ups and remote-first / EU orgs that hire for non-eng roles.
    "workable":   "https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
    "recruitee":  "https://{token}.recruitee.com/api/offers/",
    # Personio is the odd one out: it publishes an XML positions feed (not JSON)
    # and exposes no per-job URL, so it's parsed separately (see _fetch_personio).
    "personio":   "https://{token}.jobs.personio.com/xml",
}


def _fetch_personio(url: str, token: str) -> List[Dict]:
    """Personio exposes an XML positions feed rather than JSON, and gives no
    per-job URL, so the apply URL is constructed from the position id."""
    import xml.etree.ElementTree as ET
    try:
        r = requests.get(url, timeout=12)
        root = ET.fromstring(r.content)
    except Exception as e:
        emit(f"   [!] ATS personio/{token} error: {e}")
        return []

    base = url.rsplit("/xml", 1)[0]  # https://{token}.jobs.personio.com
    out = []
    for pos in root.findall(".//position"):
        job_id = pos.findtext("id") or ""
        # description is a list of <jobDescription><name/><value/></jobDescription>
        desc = " ".join(
            jd.findtext("value") or "" for jd in pos.findall("./jobDescriptions/jobDescription")
        )
        out.append({"board": f"personio:{token}", "title": pos.findtext("name") or "",
                    "company": token,
                    "url": f"{base}/job/{job_id}" if job_id else base,
                    "location": pos.findtext("office") or "",
                    "snippet": _strip_html(desc)[:3000],
                    "updated_at": pos.findtext("createdAt") or pos.findtext("createDate")})
    return out


def fetch_ats(vendor: str, token: str) -> List[Dict]:
    tmpl = ATS_FEEDS.get(vendor)
    if not tmpl:
        return []
    url = tmpl.format(token=token)

    if vendor == "personio":
        return _fetch_personio(url, token)

    try:
        data = requests.get(url, timeout=12).json()
    except Exception as e:
        emit(f"   [!] ATS {vendor}/{token} error: {e}")
        return []

    if vendor == "lever":
        # Lever's Postings API returns a raw JSON array, not {"postings": [...]}.
        rows = data if isinstance(data, list) else (data.get("postings", []) if isinstance(data, dict) else [])
    else:
        rows = data.get("jobs") if vendor == "greenhouse" else \
               data.get("offers") if vendor == "recruitee" else \
               data.get("jobs") if vendor == "workable" else \
               data.get("jobs", data)  # ashby
    out = []
    for j in rows or []:
        if vendor == "greenhouse":
            out.append({"board": f"gh:{token}", "title": j.get("title", ""),
                        "company": token, "url": j.get("absolute_url", ""),
                        "location": (j.get("location") or {}).get("name", ""),
                        "snippet": _strip_html(j.get("content", ""))[:3000],
                        "updated_at": j.get("updated_at")})
        elif vendor == "lever":
            out.append({"board": f"lever:{token}", "title": j.get("text", ""),
                        "company": token, "url": j.get("hostedUrl", ""),
                        "location": (j.get("categories") or {}).get("location", ""),
                        "snippet": _strip_html(j.get("descriptionPlain", ""))[:3000],
                        "updated_at": j.get("createdAt")})
        elif vendor == "workable":
            loc = j.get("location") or {}
            out.append({"board": f"workable:{token}", "title": j.get("title", ""),
                        "company": token,
                        "url": j.get("url") or j.get("application_url", ""),
                        "location": loc.get("location_str") or ", ".join(
                            x for x in [loc.get("city"), loc.get("country")] if x),
                        "snippet": _strip_html(j.get("description", ""))[:3000],
                        "updated_at": j.get("published_on") or j.get("created_at")})
        elif vendor == "recruitee":
            out.append({"board": f"recruitee:{token}", "title": j.get("title", ""),
                        "company": token,
                        "url": j.get("careers_url") or j.get("careers_apply_url", ""),
                        "location": j.get("location") or ", ".join(
                            x for x in [j.get("city"), j.get("country")] if x),
                        "snippet": _strip_html(j.get("description", ""))[:3000],
                        "updated_at": j.get("published_at")})
        else:  # ashby
            out.append({"board": f"ashby:{token}", "title": j.get("title", ""),
                        "company": token, "url": j.get("jobUrl", ""),
                        "location": j.get("location", ""),
                        "snippet": _strip_html(j.get("descriptionPlain", ""))[:3000],
                        "updated_at": j.get("publishedAt")})
    return out


def validate_ats_token(vendor: str, token: str) -> int:
    """Live-validates one (vendor, token) via fetch_ats; returns the number of
    jobs found (0 = dead/wrong token or feed error). Shared by seed_ats.py's
    curated-candidate validation and harvest_ats_tokens' live-validation of
    freshly-harvested tokens, so both paths use one implementation instead of
    duplicating the try/except-on-fetch_ats pattern."""
    try:
        jobs = fetch_ats(vendor, token)
    except Exception:
        return 0
    return len(jobs)


# ── Rotation + tiered first run ──────────────────────────────────────────────

def _cursor_key(profile: Dict) -> str:
    """boards_cache.db is shared by every profile, so the rotation cursor must be
    scoped per-profile -- otherwise two profiles searching share one rotation
    position and each run's term/ATS-batch window depends on unrelated profiles'
    run history. 'default' covers the standalone CLI path, which builds its own
    profile dict with no profile_id and has always been a single implicit profile."""
    return f"rotation_cursor_{profile.get('profile_id', 'default')}"


def _load_cursor(profile: Dict) -> int:
    conn = get_db()
    row = conn.execute("SELECT value FROM profile_cache WHERE key=?", (_cursor_key(profile),)).fetchone()
    conn.close()
    return int(row["value"]) if row else 0


def _save_cursor(profile: Dict, cursor: int) -> None:
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO profile_cache(key, value) VALUES(?,?)",
                 (_cursor_key(profile), str(cursor)))
    conn.commit()
    conn.close()


TERMS_PER_RUN = 6   # size of the rotating term window queried each run


def _interleave_by_cluster(terms: List[str], role_clusters: List[Dict]) -> List[str]:
    """Reorder search terms so consecutive terms come from different role
    clusters where possible (round-robin). Without this, a flat rotating
    window (TERMS_PER_RUN) can skip an entire role cluster for several runs
    in a row just because its terms happen to sit together at one end of the
    list -- e.g. a profile targeting both "Data Analyst" roles and "Research
    Assistant" roles could have every window land on Data Analyst terms only.
    A single/no cluster is a no-op (matches today's flat order). role_clusters
    is the snapshot-built list of {"roles": [...], "weighted_text": "..."}."""
    if not role_clusters or len(role_clusters) <= 1:
        return terms

    def _norm(s: str) -> str:
        return s.strip().lower()

    used: set = set()
    buckets: List[List[str]] = []
    for cluster in role_clusters:
        wanted = {_norm(r) for r in (cluster.get("roles") or [])}
        bucket = [t for t in terms if _norm(t) in wanted and t not in used]
        used.update(bucket)
        if bucket:
            buckets.append(bucket)
    leftover = [t for t in terms if t not in used]  # e.g. past_roles, unclustered
    if leftover:
        buckets.append(leftover)

    interleaved: List[str] = []
    i = 0
    while len(interleaved) < len(terms):
        progressed = False
        for bucket in buckets:
            if i < len(bucket):
                interleaved.append(bucket[i])
                progressed = True
        if not progressed:
            break
        i += 1
    return interleaved


def select_sources_for_run(profile: Dict) -> List[JobSource]:
    terms = profile.get("search_terms") or []
    terms = _interleave_by_cluster(terms, profile.get("role_clusters") or [])
    # Query-relevant: these search by the actual terms, so they run EVERY run.
    # They are what makes niche / non-tech profiles work (the ATS feeds are
    # company-centric and only help when a seeded company is in the user's field).
    # Google Jobs is promoted here because its indexer reads the JobPosting markup
    # that Workday / SmartRecruiters / custom career pages publish -- i.e. it's how
    # we reach the "unreachable" tier we can't integrate directly. Costs SerpAPI
    # credits, but the per-source toggle lets the user disable it if spend matters.
    always = [AdzunaSource(), ReedSource(), GoogleJobsSource()]
    # Remaining credit-heavy / overlapping sources: one per run on rotation keeps
    # RapidAPI spend bounded while still adding breadth.
    rotation = [JSearchSource(), RemotiveSource()]

    cur = _load_cursor(profile)
    # First run uses the always-on sources (Adzuna, Reed, Google Jobs) across
    # several terms so one narrow term can't zero it out. Google Jobs adds some
    # SerpAPI latency/credits here, but it's the first run's best shot at the
    # Workday/SmartRecruiters/custom-site tier we can't integrate directly.
    if profile.get("first_run"):
        profile["search_terms_batch"] = terms[:TERMS_PER_RUN]
        return always

    # Later runs: slide a window over the term list so successive runs explore
    # different terms, and add one rotating broad source. Wrap the window with
    # modulo indexing rather than a plain slice -- a slice near the tail of
    # `terms` silently returns fewer than TERMS_PER_RUN terms (the `or
    # terms[:TERMS_PER_RUN]` fallback below only fires when the slice is fully
    # empty, not merely short), so a run could under-query without any signal.
    n = len(terms)
    if n <= TERMS_PER_RUN:
        profile["search_terms_batch"] = list(terms)
    else:
        start = (cur * TERMS_PER_RUN) % n
        profile["search_terms_batch"] = [terms[(start + i) % n] for i in range(TERMS_PER_RUN)]
    picked = always + [rotation[cur % len(rotation)]]
    _save_cursor(profile, cur + 1)
    return picked


# Word-level match makes the batch profile-aware: harvested rows carry the phrase
# that found them, so a profile whose terms/sectors share a word gets those
# companies first. Drop role-shape words that don't carry sector signal.
_ATS_MATCH_STOP = {"the", "and", "for", "with", "junior", "senior", "lead", "mid",
                   "level", "manager", "assistant", "coordinator", "officer",
                   "associate", "intern", "graduate", "specialist", "role", "jobs"}


def _profile_match_words(profile: Dict) -> set:
    parts = list(profile.get("search_terms") or []) + list(profile.get("sectors") or [])
    words = set()
    for p in parts:
        for w in re.findall(r"[a-z0-9]+", str(p).lower()):
            if len(w) > 2 and w not in _ATS_MATCH_STOP:
                words.add(w)
    return words


def _ats_keyword_matches(keyword, words: set) -> bool:
    if not keyword or keyword == "curated" or not words:
        return False
    kw = {w for w in re.findall(r"[a-z0-9]+", str(keyword).lower()) if len(w) > 2}
    return bool(kw & words)


def select_ats_batch_for_run(profile: Dict) -> List[tuple]:
    """Return (company, vendor, token) rows, profile-aware. company_ats is a
    single store shared by all profiles, so favour companies whose harvest
    keyword matches this profile's sector, then fill the batch from the rest by
    rotation (so the shared, multi-sector store doesn't dilute any one profile)."""
    rows = load_company_ats()  # (company, vendor, token, keyword)
    if not rows:
        return []
    size = 40
    words = _profile_match_words(profile)
    preferred = [r for r in rows if _ats_keyword_matches(r[3], words)]
    others = [r for r in rows if not _ats_keyword_matches(r[3], words)]

    cur = _load_cursor(profile)

    def _rotate(lst: List[tuple]) -> List[tuple]:
        # Wrap-around slice so large sets still cycle across runs (first run: cur=0).
        if not lst:
            return []
        start = (cur * size) % len(lst)
        return (lst[start:] + lst[:start])[:size]

    # Preferred first (up to the cap), then fill with rotated others.
    ordered = _rotate(preferred) + _rotate(others)
    return [(c, v, t) for (c, v, t, _kw) in ordered[:size]]


# ── Parallel discovery ───────────────────────────────────────────────────────

def gather_jobs(profile: Dict) -> List[Dict]:
    """Orchestrates job harvesting: rotated source tier(s) + a batch of ATS
    feeds, all fetched concurrently. Tiered + cheap on the first run."""
    # Per-source visibility toggle: the backend passes the set of disabled source
    # keys (API source names like "adzuna"/"google_jobs" and ATS vendor names like
    # "greenhouse"/"lever"). Anything in it is skipped for this run.
    disabled = set(profile.get("disabled_sources") or [])
    # select_sources_for_run must run before _terms(): it fills
    # profile["search_terms_batch"] as a side effect (which engine.py also
    # reads back after this returns).
    sources = [s for s in select_sources_for_run(profile) if s.name not in disabled]
    terms = _terms(profile)
    # One pool task per (source, term) rather than one per source: a source's
    # old whole-run task was up to TERMS_PER_RUN sequential per-term calls
    # occupying a single pool slot (Reed at 3 pages/term was 18 sequential
    # HTTP calls -- the measured long pole of a 55s discovery phase).
    tasks: List[tuple] = []
    for s in sources:
        if hasattr(s, "fetch_term") and terms:
            tasks += [("term", (s, t)) for t in terms]
        else:
            tasks.append(("src", s))
    tasks += [("ats", (vendor, token))
              for (_company, vendor, token) in select_ats_batch_for_run(profile)
              if vendor not in disabled]

    # Visibility: which ATS vendors this run actually queried. A vendor absent
    # here (e.g. lever) contributed 0 jobs because it wasn't in this run's
    # 40-company rotation slice -- not because the fetch failed. A vendor present
    # here but missing from the board breakdown means its tokens returned nothing.
    from collections import Counter
    ats_mix = Counter(payload[0] for kind, payload in tasks if kind == "ats")
    if ats_mix:
        emit(f"   [ats] querying {sum(ats_mix.values())} companies this run by vendor: {dict(ats_mix)}")

    def run_task(t):
        kind, payload = t
        started = time.monotonic()
        if kind == "src":
            result = payload.fetch(profile, since=profile.get("since")) or []
            elapsed = time.monotonic() - started
            emit(f"   [source] {payload.name}: {len(result)} jobs in {elapsed:.1f}s")
            return kind, payload.name, result, elapsed
        if kind == "term":
            src, term = payload
            result = src.fetch_term(profile, term) or []
            elapsed = time.monotonic() - started
            emit(f"   [source] {src.name} ('{term}'): {len(result)} jobs in {elapsed:.1f}s")
            return kind, src.name, result, elapsed
        vendor, token = payload
        return kind, vendor, (fetch_ats(vendor, token) or []), time.monotonic() - started

    all_jobs: List[Dict] = []
    term_counts: Counter = Counter()
    ats_counts: Counter = Counter()
    slowest: Dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=12) as ex:
        for fut in as_completed([ex.submit(run_task, t) for t in tasks]):
            try:
                kind, name, result, elapsed = fut.result()
                all_jobs.extend(result)
                key = f"ats:{name}" if kind == "ats" else name
                slowest[key] = max(slowest.get(key, 0.0), elapsed)
                if kind == "ats":
                    ats_counts[name] += len(result)
                elif kind == "term":
                    term_counts[name] += len(result)
            except Exception as e:
                emit(f"   [!] discovery task failed: {e}")

    # Aggregated per-source totals (the per-term lines above are the detail).
    # "slowest term" is the source's latency floor at full parallelism -- the
    # number to look at when discovery is slow.
    for name, count in sorted(term_counts.items()):
        emit(f"   [source] {name}: {count} jobs total (slowest term {slowest.get(name, 0.0):.1f}s)")
    # One aggregated per-vendor count, comparable to the [source] lines above --
    # without this, ATS source performance was only inferable indirectly from the
    # post-filter board breakdown further down the pipeline.
    for vendor, count in sorted(ats_counts.items()):
        emit(f"   [source] ats:{vendor}: {count} jobs (slowest board {slowest.get(f'ats:{vendor}', 0.0):.1f}s)")

    if DEBUG_SAVE_RAW:
        with open(os.path.join(BASE_DIR, "raw_api_jobs.json"), "w") as f:
            json.dump(all_jobs, f, indent=2)

    return all_jobs


# ── Database Lifecycle ──────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    # timeout=30 (default 5): the gate_cache writers (_gate_cache_store) now run
    # from several threads at once -- screen_gate and rank_gate each fan their
    # batches out over a pool, and engine.py runs a whole gate round per cluster
    # concurrently. Each caller opens and closes its own connection, so reads are
    # fine, but two concurrent commits can collide; without a generous busy
    # timeout that surfaces as an outright "database is locked" and loses a
    # batch's cache entries (re-paying for them next run).
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initializes schema definitions automatically for zero-config deployment."""
    conn = get_db()
    # Create profile cache framework
    conn.execute("""
        CREATE TABLE IF NOT EXISTS profile_cache (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    # Create verified output table tracking
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            url TEXT,
            ai_summary TEXT
        )
    """)
    # Cheap-model gate decisions, keyed by (gate, profile signature, job id) so an
    # unchanged backlog job isn't re-judged every run. Cleared implicitly when the
    # profile signature changes (skills/sectors/seniority edited).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS gate_cache (
            cache_key TEXT PRIMARY KEY,
            keep INTEGER,
            reason TEXT,
            requirements_json TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Additive migration for gate_cache rows created before key_requirements
    # extraction existed (see screen_gate/screen_v5).
    gate_cache_cols = {r["name"] for r in conn.execute("PRAGMA table_info(gate_cache)")}
    if "requirements_json" not in gate_cache_cols:
        conn.execute("ALTER TABLE gate_cache ADD COLUMN requirements_json TEXT")
    conn.commit()
    conn.close()
    emit("[db] Tables ready and schema initialized.")


# ── ATS token store ──────────────────────────────────────────────────────────

# The ATS company store is UNIFIED with the backend's DB, so a harvest triggered
# from the API (on profile create/edit) lands in the same table discovery reads.
# Resolve the backend sqlite file from DATABASE_URL when it's sqlite, else fall
# back to backend/jobmatch.db. (Raw sqlite3 keeps full_auto standalone -- it must
# still work when run from seed_ats.py without importing the backend package.
# A non-sqlite DATABASE_URL, e.g. Postgres, isn't supported by this bridge.)
def _ats_db_path() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url.startswith("sqlite:///"):
        return url[len("sqlite:///"):]
    return os.path.join(BASE_DIR, "backend", "jobmatch.db")


def _ats_db() -> sqlite3.Connection:
    conn = sqlite3.connect(_ats_db_path())
    conn.row_factory = sqlite3.Row
    # Schema matches the backend CompanyATS ORM model so both agree on the table
    # whichever process creates it first.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS company_ats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL,
            vendor TEXT NOT NULL,
            token TEXT NOT NULL,
            keyword TEXT,
            created_at DATETIME,
            UNIQUE(vendor, token)
        )
    """)
    # Additive migration for stores created before the profile-aware batch tag:
    # `keyword` records which harvest phrase found the company ("curated" for the
    # hand-seeded set) so select_ats_batch_for_run can favour a profile's sector.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(company_ats)")}
    if "keyword" not in cols:
        conn.execute("ALTER TABLE company_ats ADD COLUMN keyword TEXT")
    return conn


def load_company_ats() -> List[tuple]:
    """Returns a list of (company, vendor, token, keyword) rows from the
    bootstrapped ATS token store (shared with the backend DB)."""
    conn = _ats_db()
    rows = conn.execute("SELECT company, vendor, token, keyword FROM company_ats").fetchall()
    conn.close()
    return [(r["company"], r["vendor"], r["token"], r["keyword"]) for r in rows]


def save_company_ats(rows: List[tuple], default_keyword: str = "curated") -> None:
    """Upserts rows into the ATS token store. Each row is (company, vendor, token)
    or (company, vendor, token, keyword); a missing keyword defaults to
    `default_keyword` (the hand-seeded set is tagged "curated")."""
    conn = _ats_db()
    for row in rows:
        company, vendor, token = row[0], row[1], row[2]
        keyword = row[3] if len(row) > 3 and row[3] else default_keyword
        conn.execute(
            "INSERT OR IGNORE INTO company_ats(company, vendor, token, keyword) VALUES(?,?,?,?)",
            (company, vendor, token, keyword)
        )
    conn.commit()
    conn.close()


def delete_company_ats(vendor: str, token: str) -> None:
    """Removes one dead token from the store. Used by seed_ats.py --revalidate
    to prune boards that no longer return any live postings."""
    conn = _ats_db()
    conn.execute("DELETE FROM company_ats WHERE vendor=? AND token=?", (vendor, token))
    conn.commit()
    conn.close()


def harvest_ats_tokens(sector_keywords: List[str]) -> List[tuple]:
    """Maintenance job (not run on every search): finds company ATS board tokens
    via a domain-targeted search, then upserts them into company_ats. Prefers
    serper.dev (cheap, avoids the SerpAPI limit) and falls back to SerpAPI. Run
    occasionally, e.g. once when onboarding a new sector.

    serper.dev's free tier rejects `site:`-operator queries outright ("Query
    pattern not allowed for free accounts"), so by default this builds a plain
    keyword+domain query instead (lower precision, but it actually runs on a
    free plan) -- see SERPER_SITE_OPERATOR_OK. A per-call query budget
    (ATS_HARVEST_MAX_QUERIES) protects the credit balance, and a circuit
    breaker stops the whole run the first time serper rejects a query pattern,
    instead of repeating the same rejection for every remaining combo."""
    if not (SERPER_DEV_API_KEY or SERPAPI_KEY):
        emit("   [!] No SERPER_DEV_API_KEY or SERPAPI_KEY set; cannot harvest ATS tokens.")
        return []

    site_domains = {
        "greenhouse": "boards.greenhouse.io",
        "lever":      "jobs.lever.co",
        "ashby":      "jobs.ashbyhq.com",
        "workable":   "apply.workable.com",
        "recruitee":  "recruitee.com",
        "personio":   "jobs.personio.com",
    }
    # Path-segment vendors carry the token after the domain; subdomain vendors
    # (recruitee, personio) carry it before the domain -- hence per-vendor regex.
    token_res = {
        "greenhouse": re.compile(r"greenhouse\.io/([A-Za-z0-9_-]+)"),
        "lever":      re.compile(r"lever\.co/([A-Za-z0-9_-]+)"),
        "ashby":      re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)"),
        "workable":   re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
        "recruitee":  re.compile(r"https?://([A-Za-z0-9_-]+)\.recruitee\.com"),
        "personio":   re.compile(r"https?://([A-Za-z0-9_-]+)\.jobs\.personio\."),
    }

    # Keyed by (vendor, token) rather than appended to a list: the same board is
    # routinely surfaced by more than one keyword combo, and validating it once
    # per combo used to mean re-fetching (and re-logging an error for) the same
    # dead/slow token 2-3x in a single harvest run -- collapsing here so each
    # unique board is only ever probed once below, with every keyword that found
    # it preserved for select_ats_batch_for_run's sector matching.
    found: Dict[tuple, set] = {}
    # Keyword-major (not vendor-major): with a query budget below the full
    # vendor x keyword product, this way still samples every vendor at least
    # once instead of exhausting the budget on the first vendor alone.
    combos = [(vendor, keyword) for keyword in sector_keywords for vendor in site_domains]
    budget = ATS_HARVEST_MAX_QUERIES
    skipped = 0
    serper_blocked = False
    for vendor, keyword in combos:
        if budget <= 0 or serper_blocked:
            skipped += 1
            continue
        domain = site_domains[vendor]
        token_re = token_res[vendor]
        if SERPER_DEV_API_KEY:
            query = f"site:{domain} {keyword}" if SERPER_SITE_OPERATOR_OK else f"{keyword} {domain}"
            links_raw, rejected = _serper_search_raw(query, num=20)
            if rejected:
                serper_blocked = True
                emit("   [!] serper.dev rejected the ATS-harvest query pattern; "
                     "stopping this harvest run early (set SERPER_SITE_OPERATOR_OK=true "
                     "only once the serper.dev plan actually supports site: search).")
                skipped += 1
                continue
            links = [res.get("link", "") for res in links_raw]
        else:
            # SerpAPI's free tier isn't blocked on site:, so keep using it there.
            query = f"site:{domain} {keyword}"
            try:
                r = requests.get("https://serpapi.com/search",
                                 params={"engine": "google", "q": query, "api_key": SERPAPI_KEY},
                                 timeout=12)
                links = [res.get("link", "") for res in r.json().get("organic_results", [])]
            except Exception as e:
                emit(f"   [!] ATS harvest error ({vendor}/{keyword}): {e}")
                continue
        budget -= 1
        for link in links:
            m = token_re.search(link or "")
            if m:
                token = m.group(1)
                # Tag the row with every phrase that found it so the batch
                # selector can favour it for profiles in this sector.
                found.setdefault((vendor, token), set()).add(keyword)

    deduped = [(token, vendor, token, ", ".join(sorted(kws)))
               for (vendor, token), kws in found.items()]
    # Live-validate before inserting: harvested tokens come from a regex match
    # against a search-result URL, not a guaranteed-real board (e.g. a hit on a
    # vendor's own marketing site, or a company that's since moved off that
    # ATS) -- unlike seed_ats.py's curated candidates, which were always
    # validated this way before this fix. An unvalidated dead token otherwise
    # sits in company_ats forever, re-erroring on every run that selects it.
    live: List[tuple] = []
    n_dead = 0
    if deduped:
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = {ex.submit(validate_ats_token, v, t): (c, v, t, kw) for c, v, t, kw in deduped}
            for fut in as_completed(futs):
                c, v, t, kw = futs[fut]
                if fut.result() >= 1:
                    live.append((c, v, t, kw))
                else:
                    n_dead += 1
    save_company_ats(live)
    deduped = live
    if n_dead:
        emit(f"   [ats] harvest validation dropped {n_dead} dead token(s) before insert")
    if skipped:
        emit(f"[ats] Harvest budget/circuit-breaker skipped {skipped} of "
             f"{len(combos)} vendor/keyword combos this run.")
    emit(f"[ats] Harvested {len(deduped)} ATS tokens across {len(site_domains)} vendors.")
    return deduped


def get_profile_status() -> dict:
    """Returns whether the DB has enough data to allow a search to run."""
    try:
        conn = get_db()
        profile_row = conn.execute(
            "SELECT value FROM profile_cache WHERE key='profile'"
        ).fetchone()

        # Also check for any card memory stored in DB
        card_rows = conn.execute(
            "SELECT COUNT(*) as n FROM profile_cache WHERE key LIKE 'cards_%'"
        ).fetchone()
        conn.close()

        has_profile  = profile_row is not None
        has_cv_file  = os.path.exists(CV_PATH)
        has_cards    = (card_rows["n"] if card_rows else 0) > 0

        return {
            "has_profile": has_profile,
            "has_cv_file": has_cv_file,
            "has_cards":   has_cards,
            "ready":       has_profile or has_cv_file or has_cards,
        }
    except Exception:
        return {"has_profile": False, "has_cv_file": False, "has_cards": False, "ready": False}


def save_card_memory(mem: dict) -> None:
    """Persists the card swipe memory dict {pref,titles,skills: {yes,no}} to the DB."""
    init_db()
    conn = get_db()
    for section, data in mem.items():
        conn.execute(
            "INSERT OR REPLACE INTO profile_cache(key, value) VALUES(?,?)",
            (f"cards_{section}", json.dumps(data))
        )
    conn.commit()
    conn.close()


def load_card_memory() -> dict:
    """Loads persisted card memory from DB. Returns MEM-shaped dict."""
    blank = {
        "pref":   {"yes": [], "no": []},
        "titles": {"yes": [], "no": []},
        "skills": {"yes": [], "no": []},
    }
    try:
        conn = get_db()
        for section in ["pref", "titles", "skills"]:
            row = conn.execute(
                "SELECT value FROM profile_cache WHERE key=?", (f"cards_{section}",)
            ).fetchone()
            if row:
                blank[section] = json.loads(row["value"])
        conn.close()
    except Exception:
        pass
    return blank


# ── Utilities ──────────────────────────────────────────────────────────────────

def make_job_id(board: str, url: str) -> str:
    return hashlib.md5(f"{board}|{url}".encode()).hexdigest()[:16]


from typing import Sequence

def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    arr_a = np.asarray(a)
    arr_b = np.asarray(b)
    
    norm = np.linalg.norm(arr_a) * np.linalg.norm(arr_b)
    
    # Safely check norm before dividing
    if norm == 0.0:
        return 0.0
        
    return float(np.dot(arr_a, arr_b) / norm)


def clean_json(raw: str) -> str:
    return raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def llm(prompt: str, system: str = "", model: str = CHEAP_MODEL,
        require_json: bool = False, temperature: float = 0.2) -> str:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    args = {"model": model, "messages": msgs, "temperature": temperature}
    if require_json:
        args["response_format"] = {"type": "json_object"}
        
    resp = client.chat.completions.create(**args)
    return resp.choices[0].message.content.strip()


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    cleaned = [t[:8000] for t in texts]
    resp = client.embeddings.create(model=EMBED_MODEL, input=cleaned)
    return [e.embedding for e in resp.data]


# ── Phase 1: Dynamic Profile Extraction ────────────────────────────────────────

def get_profile() -> dict:
    """Reads exp.txt and extracts a globally compatible profile with strict location metadata."""
    conn = get_db()
    row = conn.execute("SELECT value FROM profile_cache WHERE key='profile'").fetchone()
    conn.close()
    if row:
        emit("[phase 1] Profile loaded from cache.")
        return json.loads(row["value"])

    if not os.path.exists(CV_PATH):
        raise FileNotFoundError(f"Candidate experience file missing at targeted location: {CV_PATH}")

    emit("[phase 1] Extracting profile from CV...")
    cv_text = open(CV_PATH, encoding="utf-8").read()

    prompt = f"""Read this CV/experience document. Extract parameters fitting ANY professional industry or geographical region.
Output ONLY valid JSON (no markdown fences) matching this structure precisely:
{{
  "sectors": ["list of relevant domains e.g. software, accounting, marketing, hospitality"],
  "seniority": "entry-level / junior / mid-level / senior / director",
  "key_skills": ["up to 10 core tracking skills"],
  "location": "Preferred city/country string or 'United Kingdom' / 'United States'",
  "adzuna_country_code": "2-letter lowercase ISO country code where Adzuna operates: gb, us, ca, za, au, de, fr, in, it, nl, at, pl, sg (Pick closest matching region to location field, default to 'gb')",
  "search_terms": ["10-14 hyper-specific role title search queries tailored for job boards based on this background"]
}}

CV Data:
{cv_text}"""

    raw = llm(prompt, require_json=True)
    profile = json.loads(clean_json(raw))

    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO profile_cache(key, value) VALUES(?,?)", ("profile", json.dumps(profile)))
    conn.commit()
    conn.close()

    emit(f"[phase 1] Strategy built. Target Region: {profile['location']} (Adzuna Node: {profile['adzuna_country_code']})")
    return profile


def get_profile_embedding(profile: dict) -> list[float]:
    conn = get_db()
    row = conn.execute("SELECT value FROM profile_cache WHERE key='profile_embedding'").fetchone()
    conn.close()
    if row:
        return json.loads(row["value"])

    text = " ".join([*profile["key_skills"], *profile["sectors"], profile["seniority"], " ".join(profile["search_terms"])])
    embedding = get_embeddings_batch([text])[0]

    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO profile_cache(key, value) VALUES(?,?)", ("profile_embedding", json.dumps(embedding)))
    conn.commit()
    conn.close()
    return embedding


def api_candidates(jobs, profile_embedding):
    texts = [f"{j['title']} {j['company']} {j['snippet']}" for j in jobs]
    BATCH_SIZE = 100
    embeddings = []

    for i in range(0, len(texts), BATCH_SIZE):
        chunk = texts[i:i+BATCH_SIZE]
        embeddings.extend(get_embeddings_batch(chunk))

    candidates = []
    for job,emb in zip(jobs, embeddings):
        score = cosine_similarity(profile_embedding, emb)
        if score < RELEVANCE_THRESHOLD:
            continue
        job["embed_score"] = score
        candidates.append(job)
    return candidates


# ── Phase 4: Two binary gates + deterministic selection ─────────────────────────
# Replaces the old single "rank the best 25 of 60" LLM pass. Ranking/selecting
# from a long unordered list is exactly where a cheap model shows position bias
# and run-to-run inconsistency; and the backend already narrows to <=25 before
# this stage, so that pass wasn't doing real narrowing anyway. Instead:
#   Pass 1 sector/domain gate   -> independent keep/discard per listing
#   Pass 2 gap/seniority gate   -> independent keep/discard per listing
#   deterministic top-N by embed_score (no LLM call)
# Both gates are temperature 0 and cached by (gate, profile signature, job id).

MAX_MUST_HAVE_GAPS = 3   # discard when a role states more hard gaps than this
_GATE_BATCH = 20         # listings per LLM call


def _profile_signature(profile: dict) -> str:
    """Stable hash of the profile facets a gate decision depends on. When any of
    these change the cache naturally misses and the job is re-judged."""
    basis = json.dumps({
        "sectors": sorted(s.lower() for s in (profile.get("sectors") or [])),
        "seniority": (profile.get("seniority") or "").lower(),
        "key_skills": sorted(s.lower() for s in (profile.get("key_skills") or [])),
        "search_terms": sorted(s.lower() for s in (profile.get("search_terms") or [])),
        "requirements": sorted(r.lower() for r in (profile.get("requirements") or [])),
        "avoid": sorted(a.lower() for a in (profile.get("avoid") or [])),
        "must_have": sorted(m.lower() for m in (profile.get("must_have") or [])),
    }, sort_keys=True)
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def _gate_job_id(job: dict) -> str:
    """Stable per-job key. Prefer the backend's cross-source identity when present.

    Carries a two-state TEXT-RICHNESS marker, because _gate_cache_key below keys on
    (gate, profile signature, this) and NOT on the text that was actually judged.
    The same job can reach a gate with wildly different amounts of text: a ~455-char
    Reed/Adzuna search teaser when it's brand new, or its full description once
    fetch_reed_details or a Phase 5 scrape has supplied one (see engine.py's
    _has_full_text). Without the marker, a verdict reached on the teaser would be
    served forever for a job we can now actually read -- silently cancelling the
    enrichment for exactly the jobs that most needed re-judging."""
    ident = job.get("_identity") or make_job_id(job.get("board", ""), job.get("url", ""))
    return f"{ident}:full" if job.get("_has_full_text") else ident


def _gate_cache_key(gate: str, sig: str, job_id: str) -> str:
    return hashlib.sha1(f"{gate}|{sig}|{job_id}".encode()).hexdigest()


def _gate_cache_lookup(keys: list[str]) -> dict[str, tuple[bool, str, str | None]]:
    """Third tuple element is requirements_json (screen_gate's key_requirements,
    JSON-encoded) -- None for every other gate, which never writes it."""
    if not keys:
        return {}
    conn = get_db()
    out: dict[str, tuple[bool, str, str | None]] = {}
    # SQLite caps variables per statement; chunk to stay well under it.
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        placeholders = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT cache_key, keep, reason, requirements_json FROM gate_cache "
            f"WHERE cache_key IN ({placeholders})",
            chunk,
        ).fetchall():
            out[r["cache_key"]] = (bool(r["keep"]), r["reason"] or "", r["requirements_json"])
    conn.close()
    return out


def _gate_cache_store(entries: list[tuple[str, bool, str, str | None]]) -> None:
    """Fourth element is requirements_json (screen_gate only) -- pass None from
    every other gate."""
    if not entries:
        return
    conn = get_db()
    conn.executemany(
        "INSERT OR REPLACE INTO gate_cache(cache_key, keep, reason, requirements_json) VALUES(?,?,?,?)",
        [(k, 1 if keep else 0, reason, req_json) for (k, keep, reason, req_json) in entries],
    )
    conn.commit()
    conn.close()


def _run_gate(gate: str, candidates: list[dict], profile: dict,
              build_prompt, system: str) -> list[dict]:
    """Generic per-listing binary gate. Returns the KEPT candidates (order
    preserved). Cached, temperature 0, batched. Fails OPEN: on any parse/LLM
    error the batch is kept, since a gate should never silently lose good roles."""
    if not candidates:
        return []
    sig = _profile_signature(profile)
    keys = [_gate_cache_key(gate, sig, _gate_job_id(c)) for c in candidates]
    cached = _gate_cache_lookup(keys)

    kept: list[dict] = []
    to_judge: list[tuple[int, dict]] = []   # (original index, job) needing an LLM call
    for c, key in zip(candidates, keys):
        if key in cached:
            keep, _reason, _req_json = cached[key]
            if keep:
                kept.append(c)
        else:
            to_judge.append((c, key))

    n_cached = len(candidates) - len(to_judge)
    new_entries: list[tuple[str, bool, str, str | None]] = []
    for start in range(0, len(to_judge), _GATE_BATCH):
        batch = to_judge[start:start + _GATE_BATCH]
        listing_block = "\n".join(
            f"{i+1}. {c['title']} @ {c.get('company','')} | "
            f"{(c.get('location') or 'location unknown')} | {(c.get('snippet') or '')[:450]}"
            for i, (c, _k) in enumerate(batch)
        )
        prompt = build_prompt(profile, listing_block)
        decisions: dict[int, tuple[bool, str]] = {}
        try:
            raw = llm(prompt, system=system, require_json=True, temperature=0)
            for d in json.loads(clean_json(raw)).get("decisions", []):
                n = d.get("n")
                if isinstance(n, int):
                    decisions[n] = (bool(d.get("keep", True)), str(d.get("reason", "")))
        except Exception as e:
            emit(f"[gate:{gate}] batch parse failed ({e}); keeping batch (fail-open).")
            decisions = {i + 1: (True, "gate_error") for i in range(len(batch))}

        for i, (c, key) in enumerate(batch):
            keep, reason = decisions.get(i + 1, (True, "missing_decision"))
            new_entries.append((key, keep, reason, None))
            if keep:
                kept.append(c)

    _gate_cache_store(new_entries)
    dropped = len(candidates) - len(kept)
    emit(f"[gate:{gate}] {len(candidates)} in -> {len(kept)} kept "
         f"({dropped} dropped; {n_cached} from cache)")
    return kept


def _sector_prompt(profile: dict, listing_block: str) -> str:
    return f"""You are screening job listings for ONE candidate. Judge ONLY sector/domain fit.

Candidate target sectors/domains: {', '.join(profile.get('sectors') or []) or 'n/a'}
Candidate target roles: {', '.join(profile.get('search_terms') or []) or 'n/a'}

For EACH listing decide keep or discard:
- keep=true  if the role is in one of these sectors/domains, or clearly adjacent.
- keep=false if it is in an unrelated field.
When genuinely unsure, keep=true (later stages judge finer fit).

Output ONLY JSON: {{"decisions":[{{"n":1,"keep":true}},{{"n":2,"keep":false}}]}}
Include one object per listing, numbered exactly as shown.

Listings:
{listing_block}"""


def _seniority_prompt(profile: dict, listing_block: str) -> str:
    return f"""You are screening job listings for ONE candidate on SENIORITY and HARD REQUIREMENTS only.

Candidate seniority: {profile.get('seniority', 'mid-level')}
Candidate core skills: {', '.join(profile.get('key_skills') or []) or 'n/a'}
(Skill exposure durations, if stated, indicate depth - treat undated/brief mentions as weaker
signal than multi-year stated experience.)

For EACH listing set keep=false (discard) if EITHER:
- the title/description clearly implies a seniority level well ABOVE or well BELOW
  the candidate (e.g. Director/VP/Head/Principal for a mid-level candidate, or
  Intern/Graduate/Entry for a senior candidate), OR
- it states more than {MAX_MUST_HAVE_GAPS} hard must-have requirements the candidate
  clearly lacks.
Otherwise keep=true. When unsure, keep=true.

Give a short reason code: "ok" | "seniority_high" | "seniority_low" | "too_many_gaps".
Output ONLY JSON: {{"decisions":[{{"n":1,"keep":true,"reason":"ok"}}]}}
Include one object per listing, numbered exactly as shown.

Listings:
{listing_block}"""


def sector_gate(candidates: list[dict], profile: dict) -> list[dict]:
    """Pass 1: binary sector/domain gate (cheap model, temperature 0)."""
    return _run_gate("sector", candidates, profile, _sector_prompt,
                     system="You screen job listings for sector fit. Be inclusive when unsure.")


def seniority_gate(candidates: list[dict], profile: dict) -> list[dict]:
    """Pass 2: binary gap/seniority gate (cheap model, temperature 0)."""
    return _run_gate("seniority", candidates, profile, _seniority_prompt,
                     system="You screen job listings for seniority and hard-requirement fit.")


_SENIORITY_BAD_CODES = ("seniority_high", "seniority_low", "too_many_gaps")


def _annotate_with_weight_tiers(
    values: list[str], tiers: dict[str, str] | None, evidence_tiers: dict[str, str] | None = None
) -> str:
    """Comma-joined list, appending each value's feedback-weight priority tier
    (from tick/cross history on past results, see snapshot.py's _weight_tier)
    and, when given, its weak-evidence tier (see snapshot.py's _evidence_tier --
    shallow proficiency and/or non-commercial evidence_origin) -- so the cheap
    gate/rank models get some signal both from the candidate's own feedback and
    from how their evidence for a skill was earned, not just a flat attribute
    list. Values with neither annotation are rendered plain."""
    if not values:
        return "n/a"
    tiers = tiers or {}
    evidence_tiers = evidence_tiers or {}
    parts = []
    for v in values:
        labels = [t for t in (tiers.get(v), evidence_tiers.get(v)) if t]
        parts.append(f"{v} ({'; '.join(labels)})" if labels else v)
    return ", ".join(parts)


def _candidate_background_block(profile: dict) -> str:
    """Evidence-focused narrative brief (parsing.py's cv_summary schema key,
    see snapshot.py's candidate_brief), when available -- named projects/tools/
    outcomes the coarse tier labels above can't carry. "" (whole block omitted)
    when the CV gave nothing concrete beyond the typed attribute rows, or
    before any CV/text has ever been parsed."""
    brief = (profile.get("candidate_brief") or "").strip()
    if not brief:
        return ""
    return f"""
CANDIDATE BACKGROUND (concrete evidence -- use this to judge depth/fit, not just the tags above)
{brief}
"""


def _screen_prompt(profile: dict, listing_block: str) -> str:
    # The candidate's own requirement chips reach this prompt in two groups (see
    # snapshot.build_snapshot): the ones they marked Hard drive the unconditional
    # hard_gate_ok drop, the ones they marked Soft are folded into the
    # candidate-specific requirements axis, which is soft. Only the wording
    # differs here -- the actual drop/demote decision is engine.py's.
    reqs = list(profile.get("requirements") or [])
    for m in profile.get("soft_must_have") or []:
        reqs.append(f"Prefers a role that offers: {m} (a preference, not a hard requirement)")
    for a in profile.get("soft_avoid") or []:
        reqs.append(f"Would rather avoid: {a} (a preference, not a hard exclusion)")
    req_block = "\n".join(f"- {r}" for r in reqs) if reqs else "None stated."
    salary_floor = profile.get("salary_floor") or 0
    salary_line = (f"Candidate stated salary floor: {salary_floor}"
                   if salary_floor else "Candidate stated salary floor: none stated.")
    avoids = profile.get("avoid") or []
    must_haves = profile.get("must_have") or []
    avoid_block = ", ".join(avoids) if avoids else "none stated"
    must_block = ", ".join(must_haves) if must_haves else "none stated"
    # Axes the candidate promoted to non-negotiable. The model still judges each
    # axis the same way (a CLEAR mismatch only) -- this just tells it the stakes,
    # so it doesn't wave through a borderline case on an axis the caller is about
    # to hard-drop on. See snapshot._hard_axes / engine._hard_enforced_axes.
    hard_axes = set(profile.get("hard_axes") or [])
    def _strict(axis: str) -> str:
        return (" The candidate has marked this NON-NEGOTIABLE: a clear mismatch here removes the "
                "role outright, so judge it carefully -- but still only say false on a CLEAR "
                "mismatch, never on silence or ambiguity." if axis in hard_axes else "")
    return f"""You are screening job listings for ONE candidate. For EACH listing judge EIGHT things independently.

ROLE FUNCTION FIT
Candidate target roles: {_annotate_with_weight_tiers(profile.get('search_terms') or [], profile.get('target_role_weight_tiers'))}
- sector_confidence="match" if the listing is CLEARLY the same underlying job function as one of these
  target roles, or a closely adjacent one (e.g. a different seniority phrasing of the same function, or
  a near-identical role at a different type of employer). A listing whose TITLE matches one of the
  target roles above word-for-word is presumptively sector_confidence="match" -- only override this if
  the listing's actual described duties clearly diverge from that function despite the title (e.g. a
  titled "Research Analyst" role whose duties are entirely sales or admin).
- sector_confidence="mismatch" if it is CLEARLY a different job function -- e.g. the candidate targets
  Data Analyst/Insights roles and the listing is a Product Manager, Communications Officer, or Intelligence/
  Security Analyst role: even in a related or adjacent industry, or with a shared word like "Analyst" in
  the title, a different underlying job function is not a match. Judge on FUNCTION, not industry or
  job-tooling -- a role in a totally different industry doing the same job the candidate wants is
  sector_confidence="match"; a role in the candidate's own industry (or one that uses the candidate's own
  tools) doing a different job is sector_confidence="mismatch". Do NOT judge this axis using the
  candidate's skills list below -- a listing that reads as technical/data-flavored because it happens to
  mention the candidate's own tools is not thereby a function match, and skills inform CORE SKILLS
  OVERLAP only.
- sector_confidence="ambiguous" when you genuinely cannot tell either way -- e.g. a vague or generic
  title, a thin description that could plausibly be either the candidate's target function or a
  different one, or a title used inconsistently across employers. This is NOT a mismatch: the listing
  still proceeds exactly like a match (it is never dropped for this alone) -- it only flags the
  uncertainty so a later stage with more of the listing's text can take a closer look, instead of a
  thin, ambiguous signal being silently treated as a confirmed fit.
(sector_confidence != "mismatch", together with CANDIDATE HARD FILTERS and REAL LISTING CHECK below,
acts as an unconditional hard drop -- the other five are judged independently and the caller decides
how many failures a listing can tolerate.)

CANDIDATE HARD FILTERS (the candidate's OWN stated non-negotiables)
Will REJECT a role that involves any of (avoid): {avoid_block}
REQUIRES a role to satisfy all of (must-have): {must_block}
- hard_gate_ok=false only if the listing CLEARLY involves one of the avoid items, OR clearly
  contradicts / cannot satisfy one of the must-have items (e.g. a must-have of "fully remote" but
  the listing is plainly on-site with no remote option). When the listing is silent on the point or
  it is genuinely ambiguous, hard_gate_ok=true -- do NOT drop on absence of confirmation. If both
  lists are "none stated", hard_gate_ok=true always.

REAL LISTING CHECK
- listing_ok=false only if the text CLEARLY is not one specific job posting the candidate could
  apply to -- e.g. a job board's own search-results/category page (several different salary ranges
  or role titles run together, "N jobs found", "browse our vacancies"), a board's generic blurb
  describing the kind of jobs it lists rather than describing one role, or similar junk/placeholder
  text with no actual single-role content.
- A real posting that is simply thin, vague, or informally written is NOT covered by this -- only
  content that clearly is not describing one specific role at all. When unsure or genuinely
  ambiguous, listing_ok=true.

SENIORITY / EXPERIENCE / HARD REQUIREMENTS
Candidate seniority: {profile.get('seniority', 'mid-level')}
Candidate core skills: {_annotate_with_weight_tiers(profile.get('key_skills') or [], profile.get('skill_weight_tiers'), profile.get('skill_evidence_tiers'))}
(Stated multi-year durations are stronger evidence than brief/undated mentions. A target role or skill
tagged "strongly preferred"/"preferred" reflects the candidate's own past tick feedback -- weigh sector fit
a little more favorably toward it. One tagged "deprioritize"/"lower priority" reflects past cross feedback
-- don't let it alone satisfy a hard requirement or sector match. A skill tagged "familiar evidence only",
"one-time evidence only", "self-directed evidence only", "academic evidence only", or "ai-assisted evidence
only" means the candidate's evidence for it is shallow and/or not from paid/commercial work -- don't let it
alone satisfy a requirement that clearly expects professional/production-level competency.)
{_candidate_background_block(profile)}- seniority_ok=false, reason="seniority_high", if the listing clearly implies a level well ABOVE
  the candidate -- e.g. Director/VP/Head/Principal/Lead in the title, or duties like "lead the
  elicitation of requirements" / "facilitate workshops" / manage a team -- or it states a minimum
  years-of-experience requirement (e.g. "3+ years") the candidate clearly doesn't meet, or more
  than {MAX_MUST_HAVE_GAPS} hard must-have requirements the candidate clearly lacks.
- seniority_ok=false, reason="seniority_low", if the listing clearly implies a level well BELOW the
  candidate's stated seniority (e.g. Intern/Work-experience for a Mid/Senior candidate), or the role
  is a production-level professional role in a field the candidate has only shallow/non-commercial
  evidence for (e.g. "Data Scientist" expecting production ML/DS work from a candidate whose only DS
  evidence is academic-tagged).
  (These two directions are opposite failures -- "_high" always means the ROLE outranks the
  CANDIDATE, "_low" always means the CANDIDATE outranks the role's real level or lacks the
  professional depth it expects. Do not mix them up.)
- A stated salary/pay figure is also a real signal of the listing's TRUE seniority band, often
  more reliable than the title -- a title can be inflated or watered down, a number the employer
  is actually paying usually can't. If the listing states a salary, weigh it alongside the title
  and description: a rate that reads as clearly entry-level pay for the sector/region points to a
  junior/graduate role even under a fancier title, and a rate that reads as clearly senior pay
  points to a more senior role even under a modest-sounding title. Use ordinary judgement for the
  sector/region/currency shown -- there is no fixed number, it varies by country and field. Don't
  invent a mismatch from salary alone when the figure is genuinely ambiguous or absent.
- Before setting seniority_ok=false, you MUST identify which ONE concrete signal it rests on -- an
  explicit seniority word in the TITLE, an explicit years-of-experience or degree-level bar in the
  text, a stated salary that clearly reads as the wrong band, or (for seniority_low only) a named
  professional-level skill the candidate's evidence for is shallow/non-commercial. Record that
  signal in "seniority_signal" (a short quote or phrase, e.g. "\"Lead the elicitation...\" implies
  team/process ownership beyond junior scope"). A plain "Officer"/"Assistant"/"Coordinator"/
  "Executive"-style title with no such signal, or a general impression that the role "sounds senior"
  or "sounds junior" without one, is NOT enough -- in that case seniority_ok=true and omit
  "seniority_signal".
- otherwise seniority_ok=true. When unsure or genuinely ambiguous, seniority_ok=true.{_strict("_seniority_ok")}

CANDIDATE-SPECIFIC REQUIREMENTS
{req_block}
- requirements_ok=false only if the listing CLEARLY conflicts with one of these. If none are
  listed, or it doesn't clearly conflict, requirements_ok=true. When unsure or genuinely
  ambiguous, requirements_ok=true. Sector/domain fit and seniority are judged separately
  above -- these are the candidate's OWN additional requirements, don't repeat either of
  those here.

CORE SKILLS OVERLAP
Candidate core skills: {_annotate_with_weight_tiers(profile.get('key_skills') or [], profile.get('skill_weight_tiers'), profile.get('skill_evidence_tiers'))}
- skills_ok=false only if the listing's stated requirements clearly share almost NONE of the
  candidate's core skills (a fundamentally different toolset/discipline), not merely a partial
  overlap or a skill or two missing. When unsure or genuinely ambiguous, skills_ok=true.
- skills_ok=false also when the listing clearly expects professional/production-level competency
  (not just familiarity) in one of the candidate's core skills specifically, AND that exact skill is
  tagged above as shallow/non-commercial evidence ("familiar evidence only", "one-time evidence
  only", "self-directed evidence only", "academic evidence only", or "ai-assisted evidence only")
  with no other candidate skill covering the same requirement -- this applies even if other skills
  overlap fine; a single professionally-required skill the candidate only has shallow evidence for
  is enough by itself. Don't apply this to skills with no evidence tag at all (untagged = ordinary/
  trusted evidence).

SALARY FIT
{salary_line}
- salary_ok=false only if the listing states a salary/range and it is CLEARLY below the
  candidate's stated floor. If the listing states no salary, or the candidate has no stated
  floor, or the ranges could plausibly overlap, salary_ok=true.{_strict("_salary_ok")}

WORK ARRANGEMENT
Candidate stated work-type preference: {', '.join(profile.get('work_types') or []) or 'none stated'}
Candidate location: {profile.get('location') or 'n/a'}
- First classify the LISTING's own work arrangement as remote, hybrid, or on-site: if it explicitly
  says remote/distributed/work-from-home, it's remote; if it explicitly says hybrid, it's hybrid;
  otherwise -- including when it states a specific city/office location and simply doesn't mention
  remote/hybrid/work-from-home at all -- treat it as on-site at that location. Don't default an
  unlabeled listing to remote just because remote wasn't ruled out.
- work_arrangement_ok=false only if the candidate stated a work-type preference above AND the
  listing's classified arrangement clearly conflicts with it (e.g. candidate wants remote-only and
  the listing is on-site with no remote mention). If the candidate stated no preference, or the
  listing's arrangement could match, work_arrangement_ok=true.{_strict("_work_arrangement_ok")}

KEY REQUIREMENTS (context for the final judge -- not a filter here)
Also extract up to 4 of the listing's most important, CONCRETE requirements (a specific tool,
technology, certification, or a stated years/type-of-experience bar) as "key_requirements". For
each: "necessity" is "required" if the listing states or clearly implies it's mandatory, else
"nice_to_have"; "professional_level_expected" is true only if the listing's own wording implies
real-world/production/paid-level competency is expected for it (not just familiarity or exposure).
Omit the list entirely if the listing states no concrete requirements worth flagging.

reason: short code for the MOST significant failure (or "ok" if all pass) -- "ok" |
"seniority_high" | "seniority_low" | "too_many_gaps" | "requirement_gap" | "skills_gap" |
"salary_mismatch" | "arrangement_mismatch" | "off_sector" | "hard_filter" | "not_a_real_listing".
Output ONLY JSON: {{"decisions":[{{"n":1,"sector_confidence":"match","hard_gate_ok":true,"listing_ok":true,"seniority_ok":true,"seniority_signal":null,"requirements_ok":true,"skills_ok":true,"salary_ok":true,"work_arrangement_ok":true,"reason":"ok","key_requirements":[{{"item":"SQL","necessity":"required","professional_level_expected":true}}]}}]}}
Include one object per listing, numbered exactly as shown.

Listings:
{listing_block}"""


_REQUIREMENTS_BAD_CODE = "requirement_gap"
_SKILLS_BAD_CODE = "skills_gap"
_SALARY_BAD_CODE = "salary_mismatch"
_WORK_ARRANGEMENT_BAD_CODE = "arrangement_mismatch"
_HARD_GATE_BAD_CODE = "hard_filter"
_LISTING_BAD_CODE = "not_a_real_listing"
# packed_reason's 8th (sector) code -- not a hard/soft-axis failure like the
# others above, just an auxiliary confidence note: "ok" (match), "sector_mismatch"
# (would already be hard-dropped via _sector_ok=False before reaching rank_gate/
# the judge, so this code mostly matters for the raw gate-output diagnostic), or
# _SECTOR_AMBIGUOUS_CODE for the genuinely-unsure case that still proceeds like a
# match but carries the uncertainty forward (see screen_gate's docstring).
_SECTOR_AMBIGUOUS_CODE = "sector_ambiguous"
_SECTOR_MISMATCH_CODE = "sector_mismatch"
# Soft axes (everything except sector_ok AND hard_gate_ok, which are the two
# unconditional hard drops) -- a listing failing 2 or more of these is
# hard-dropped rather than merely demoted. Kept as a tuple of attribute names so
# engine.py's gate loop and this module agree on exactly what counts without
# duplicating the list. hard_gate_ok (the candidate's own avoid/must-have
# non-negotiables) is deliberately NOT here: like sector_ok it drops
# unconditionally in engine.py, not on a 2-failure threshold.
SOFT_GATE_AXES = ("_seniority_ok", "_requirements_ok", "_skills_ok", "_salary_ok",
                   "_work_arrangement_ok")


def dynamic_hard_drop_threshold(soft_fail_counts: list[int]) -> int:
    """Given each in-sector candidate's soft-axis failure count for one gate
    round, decide how many failures should hard-drop a listing. Fixed at 2+
    normally (two independent LLM signals agreeing on a mismatch), but when
    over half the round is sailing through every axis clean, that's a sign the
    round is thin on real mismatches rather than that everyone genuinely fits
    -- tighten to 1+ so a single confirmed mismatch is enough, instead of
    waiting for a second signal that a lax round is unlikely to produce.
    Shared by screen_gate's own diagnostic log and engine.py's actual
    hard-drop decision so the two can't disagree on what "hard-dropped" means."""
    if not soft_fail_counts:
        return 2
    clean = sum(1 for n in soft_fail_counts if n == 0)
    return 1 if clean / len(soft_fail_counts) > 0.5 else 2


def _sanitize_key_requirements(raw) -> list[dict]:
    """Validate/cap the cheap model's key_requirements output (see _screen_prompt)
    -- at most 4 concrete requirement items, each normalised to a fixed shape, so
    a malformed or oversized response can't corrupt gate_cache or blow up the
    final-eval hint text."""
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:4]:
        if not isinstance(r, dict):
            continue
        item = str(r.get("item", "")).strip()
        if not item:
            continue
        necessity = "required" if str(r.get("necessity", "")).lower() == "required" else "nice_to_have"
        out.append({
            "item": item[:60],
            "necessity": necessity,
            "professional_level_expected": bool(r.get("professional_level_expected", False)),
        })
    return out


def screen_gate(candidates: list[dict], profile: dict) -> list[dict]:
    """Merged role-function+hard-filter+real-listing+seniority+requirements+skills+
    salary+work-arrangement screen (cheap model, temperature 0) that replaces the
    two separate gate calls in the backend path. Unlike sector_gate/seniority_gate
    it does NOT filter -- it
    ANNOTATES every candidate in place with `_sector_ok`, `_sector_ambiguous`,
    `_hard_gate_ok`, and `_listing_ok` plus the 5 SOFT_GATE_AXES
    booleans, `_gate_reason`, and `_key_requirements`, and returns the full list.
    Despite the field name (kept for schema/cache stability), `_sector_ok` no
    longer judges INDUSTRY sector -- see _screen_prompt's ROLE FUNCTION FIT block:
    it used to also weigh an LLM-inferred `profile["sectors"]` guess (from
    snapshot._infer_region, invisible/uneditable in the UI, recomputed fresh each
    run from skills+past/target roles), which could drift from the candidate's
    actual current target roles and let same-industry-different-function listings
    (e.g. a Lead Product Manager role for a Data & Insights candidate) through as
    "adjacent enough". It now judges purely on job-FUNCTION similarity to the
    candidate's own stated target roles, which were already part of this same
    axis's judgment anyway. The model reports its confidence as a 3-way
    sector_confidence ("match"/"ambiguous"/"mismatch") rather than a flat boolean;
    `_sector_ok` is derived as `sector_confidence != "mismatch"` (so the drop
    decision below is unchanged), and `_sector_ambiguous` separately flags the
    "genuinely unsure" case -- previously indistinguishable from a confident
    match, which meant a thin/ambiguous listing was silently treated downstream
    as though role-function fit were confirmed. `_sector_ambiguous` is surfaced as
    a hint to rank_gate (see _score_rank_batch's listing_block) and reaches the
    final judge for free via the existing `_gate_reason`-derived hint mechanism
    (see _final_eval_job_block) -- no separate plumbing needed there since the
    packed reason string already carries it as its 8th code.
    The backend hard-drops off-sector jobs unconditionally, and likewise any job
    failing `_hard_gate_ok` (the candidate's own avoid/must-have non-negotiables)
    or `_listing_ok` (the text clearly isn't one specific job posting -- a board's
    own search-results/category page or generic aggregator blurb that slipped past
    discovery-time filtering); the 5 soft axes are
    individually informational, but engine.py hard-drops a job that fails 2 or
    more of them (see engine.py's gate loop) rather than merely demoting it,
    since two independent LLM signals agreeing on a clear mismatch is confident
    enough to skip rank_gate/the judge.

    Cached per (profile signature, job id) in the shared gate_cache under
    gate="screen_v10" (bumped from "screen_v9": a live audit found the sector axis
    anchoring on the candidate's skills list instead of the target-role list -- a
    listing titled exactly "Research Analyst" (one of the candidate's own named
    target roles) was wrongly called sector_confidence="mismatch" because its
    description read as too technical/data-tooling-flavored -- so the prompt now
    presumes a word-for-word title match is sector_confidence="match" and
    explicitly bars using the skills list to judge this axis. Separately, the
    seniority axis's "seniority_high"/"seniority_low" reason codes were being
    picked essentially at random by the model despite the docstring/enum implying
    a clear ABOVE/BELOW split -- live output showed both a too-senior TQR listing
    ("lead the elicitation...facilitate workshops") and a too-senior Data
    Scientist listing coded "seniority_low", which is backwards. The prompt now
    spells out which direction maps to which code plus a worked example each, and
    requires the model to name the concrete anchor it's relying on in a new
    "seniority_signal" field before setting seniority_ok=false (not persisted to
    the cache -- see the fresh-judge loop below -- purely to keep the model
    honest about having a real anchor rather than a vibe, closing the same class
    of gap tightened for the final judge's "weakest_link"). A previously-cached
    v9 verdict was reached under the old, direction-confused seniority guidance
    and a skills-anchored sector read, and must not be reused as if it still
    means the same thing. "screen_v9" was bumped from "screen_v8": sector_ok's flat boolean became a
    3-way sector_confidence match/ambiguous/mismatch judgment, and
    GATE_LISTING_TEXT_CHARS was raised 900 -> 2000 -- see its own comment for the
    live Reed.co.uk audit that motivated both; a previously-cached verdict was
    reached under a materially smaller text window and a coarser sector signal,
    and must not be reused as if it still means the same thing. "screen_v8" was
    bumped from "screen_v7" purely for wording changes plus tightened seniority/
    skills instructions -- no schema change that time; "screen_v7" was the bump
    for the real-listing-check axis's 7th packed code, "screen_v6" before that for
    the hard-filter axis, "screen_v5" before that for the capped key_requirements
    breakdown per listing -- necessity (required/nice_to_have) and whether
    professional-level evidence is expected).
    `keep` column stores sector_ok (True for both "match" and "ambiguous",
    False only for "mismatch"); `reason` stores
    "{seniority_code}|{requirements_code}|{skills_code}|{salary_code}|
    {arrangement_code}|{hard_filter_code}|{listing_code}|{sector_code}" --
    gate_cache has no dedicated column per axis, so this keeps reusing the
    existing reason-text column, now packing 8 codes instead of 7 (sector_code is
    "ok"/"sector_ambiguous"/"sector_mismatch", appended last so any code reading
    only the first 7 -- e.g. an older diagnostic script -- still decodes those
    unchanged). `requirements_json` stores the JSON-encoded key_requirements list
    (or NULL when the listing had none worth flagging).

    Note the candidate's avoid/must_have values are part of _profile_signature, so
    editing either naturally misses the cache and re-screens every job."""
    if not candidates:
        return []
    sig = _profile_signature(profile)
    # screen_v10 (from screen_v9): sector axis no longer anchors on the skills
    # list and presumes a verbatim target-role title match; seniority_high/
    # seniority_low direction guidance fixed and now requires a named
    # seniority_signal anchor -- old v9 rows were judged under the confused
    # direction guidance and the skills-anchored sector read, and must not be
    # reused as if they still mean the same thing.
    keys = [_gate_cache_key("screen_v10", sig, _gate_job_id(c)) for c in candidates]
    cached = _gate_cache_lookup(keys)

    to_judge: list[tuple[dict, str]] = []
    for c, key in zip(candidates, keys):
        if key in cached:
            sector_ok, packed_reason, req_json = cached[key]
            (seniority_code, req_code, skills_code, salary_code, arr_code,
             hard_code, listing_code, sector_code) = (list(packed_reason.split("|")) + ["ok"] * 8)[:8]
            c["_sector_ok"] = sector_ok
            c["_sector_ambiguous"] = sector_code == _SECTOR_AMBIGUOUS_CODE
            c["_seniority_ok"] = seniority_code not in _SENIORITY_BAD_CODES
            # seniority_signal is prompt-forcing only (see the fresh-judge loop
            # below) and isn't persisted to gate_cache, so a cache-hit row never
            # has one to restore.
            c["_seniority_signal"] = None
            c["_requirements_ok"] = (req_code or "ok") != _REQUIREMENTS_BAD_CODE
            c["_skills_ok"] = (skills_code or "ok") != _SKILLS_BAD_CODE
            c["_salary_ok"] = (salary_code or "ok") != _SALARY_BAD_CODE
            c["_work_arrangement_ok"] = (arr_code or "ok") != _WORK_ARRANGEMENT_BAD_CODE
            c["_hard_gate_ok"] = (hard_code or "ok") != _HARD_GATE_BAD_CODE
            c["_listing_ok"] = (listing_code or "ok") != _LISTING_BAD_CODE
            c["_gate_reason"] = packed_reason
            try:
                c["_key_requirements"] = json.loads(req_json) if req_json else []
            except (TypeError, ValueError):
                c["_key_requirements"] = []
        else:
            to_judge.append((c, key))

    n_cached = len(candidates) - len(to_judge)

    def _screen_one_batch(
        batch: list[tuple[dict, str]]
    ) -> list[tuple[str, bool, str, str | None]]:
        """One _GATE_BATCH-sized LLM call. Annotates its own batch's candidate
        dicts in place and RETURNS its cache entries rather than appending to a
        shared list, so several of these can run concurrently (see the pool
        below) without two threads touching the same object."""
        new_entries: list[tuple[str, bool, str, str | None]] = []
        listing_block = "\n".join(
            f"{i+1}. {c['title']} @ {c.get('company','')} | "
            f"{(c.get('location') or 'location unknown')}"
            f"{_listing_salary_suffix(c)} | "
            f"{(c.get('full_text') or c.get('snippet') or '')[:GATE_LISTING_TEXT_CHARS]}"
            for i, (c, _k) in enumerate(batch)
        )
        prompt = _screen_prompt(profile, listing_block)
        decisions: dict[int, tuple[str, bool, bool, bool, str | None, bool, bool, bool, bool, str]] = {}
        key_reqs_by_n: dict[int, list[dict]] = {}
        try:
            raw = llm(prompt, require_json=True, temperature=0,
                      system="You screen job listings for role-function fit (match/ambiguous/mismatch), "
                             "the candidate's own hard filters, whether the text is even a real single "
                             "job posting, seniority, requirements, skills, salary, and work-arrangement "
                             "fit. Be inclusive when unsure.")
            for d in json.loads(clean_json(raw)).get("decisions", []):
                n = d.get("n")
                if isinstance(n, int):
                    sector_confidence = str(d.get("sector_confidence", "match")).strip().lower()
                    if sector_confidence not in ("match", "ambiguous", "mismatch"):
                        sector_confidence = "match"
                    seniority_signal = d.get("seniority_signal")
                    decisions[n] = (sector_confidence,
                                    bool(d.get("hard_gate_ok", True)),
                                    bool(d.get("listing_ok", True)),
                                    bool(d.get("seniority_ok", True)),
                                    str(seniority_signal) if seniority_signal else None,
                                    bool(d.get("requirements_ok", True)),
                                    bool(d.get("skills_ok", True)),
                                    bool(d.get("salary_ok", True)),
                                    bool(d.get("work_arrangement_ok", True)),
                                    str(d.get("reason", "ok")))
                    key_reqs_by_n[n] = _sanitize_key_requirements(d.get("key_requirements"))
        except Exception as e:
            emit(f"[gate:screen] batch parse failed ({e}); keeping batch (fail-open).")
            decisions = {i + 1: ("match", True, True, True, None, True, True, True, True, "gate_error")
                         for i in range(len(batch))}

        for i, (c, key) in enumerate(batch):
            (sector_confidence, hard_gate_ok, listing_ok, seniority_ok, seniority_signal,
             requirements_ok, skills_ok, salary_ok, work_arrangement_ok, reason) = decisions.get(
                i + 1, ("match", True, True, True, None, True, True, True, True, "missing_decision")
            )
            sector_ok = sector_confidence != "mismatch"
            c["_sector_ok"] = sector_ok
            c["_sector_ambiguous"] = sector_confidence == "ambiguous"
            c["_hard_gate_ok"] = hard_gate_ok
            c["_listing_ok"] = listing_ok
            c["_seniority_ok"] = seniority_ok
            c["_seniority_signal"] = seniority_signal if not seniority_ok else None
            c["_requirements_ok"] = requirements_ok
            c["_skills_ok"] = skills_ok
            c["_salary_ok"] = salary_ok
            c["_work_arrangement_ok"] = work_arrangement_ok
            # Normalise every axis into one packed code so cache-hit and fresh-judge
            # paths always agree on _gate_reason's shape (previously the fresh path
            # stored a raw model string here while cache-hit produced a normalised
            # one -- this removes that asymmetry too).
            if seniority_ok:
                seniority_code = "ok"
            elif reason in _SENIORITY_BAD_CODES:
                seniority_code = reason
            else:
                seniority_code = "too_many_gaps"
            req_code = "ok" if requirements_ok else _REQUIREMENTS_BAD_CODE
            skills_code = "ok" if skills_ok else _SKILLS_BAD_CODE
            salary_code = "ok" if salary_ok else _SALARY_BAD_CODE
            arr_code = "ok" if work_arrangement_ok else _WORK_ARRANGEMENT_BAD_CODE
            hard_code = "ok" if hard_gate_ok else _HARD_GATE_BAD_CODE
            listing_code = "ok" if listing_ok else _LISTING_BAD_CODE
            sector_code = ("ok" if sector_confidence == "match"
                           else _SECTOR_AMBIGUOUS_CODE if sector_confidence == "ambiguous"
                           else _SECTOR_MISMATCH_CODE)
            packed_reason = (f"{seniority_code}|{req_code}|{skills_code}|{salary_code}|"
                              f"{arr_code}|{hard_code}|{listing_code}|{sector_code}")
            c["_gate_reason"] = packed_reason
            key_reqs = key_reqs_by_n.get(i + 1, [])
            c["_key_requirements"] = key_reqs
            new_entries.append((key, sector_ok, packed_reason, json.dumps(key_reqs) if key_reqs else None))
        return new_entries

    batches = [to_judge[start:start + _GATE_BATCH]
               for start in range(0, len(to_judge), _GATE_BATCH)]
    new_entries: list[tuple[str, bool, str, str | None]] = []
    if batches:
        # Capped concurrency, mirroring rank_gate's identical batch pool below --
        # and bounded for the same reason. Serial batches were most of this
        # stage's wall-clock time (each one a blocking LLM call, and a full gate
        # round routinely runs several per cluster), but firing every batch at
        # once risks tripping a short-window rate cap on the screening model.
        # Safe to parallelize: each call only writes to its own batch's candidate
        # dicts, and every cache write is collected here and stored once, below,
        # on this thread.
        with ThreadPoolExecutor(max_workers=min(3, len(batches))) as pool:
            for entries in pool.map(_screen_one_batch, batches):
                new_entries.extend(entries)

    _gate_cache_store(new_entries)
    in_sector = sum(1 for c in candidates if c.get("_sector_ok"))
    ambiguous_sector = sum(1 for c in candidates if c.get("_sector_ambiguous"))
    soft_fail_counts = [
        sum(1 for axis in SOFT_GATE_AXES if not c.get(axis, True))
        for c in candidates if c.get("_sector_ok")
    ]
    all_soft_ok = sum(1 for n in soft_fail_counts if n == 0)
    threshold = dynamic_hard_drop_threshold(soft_fail_counts)
    hard_dropped = sum(1 for n in soft_fail_counts if n >= threshold)
    # Visibility for the seniority_high/seniority_low direction fix (screen_v10):
    # a batch that's still suspiciously lopsided toward one code is worth a
    # second look at the prompt rather than assuming the model is just always
    # finding one direction of mismatch.
    seniority_high = sum(1 for c in candidates if (c.get("_gate_reason") or "").split("|")[0] == "seniority_high")
    seniority_low = sum(1 for c in candidates if (c.get("_gate_reason") or "").split("|")[0] == "seniority_low")
    emit(f"[gate:screen] {len(candidates)} in -> {in_sector} in-sector ({ambiguous_sector} ambiguous), "
         f"{all_soft_ok} pass all soft axes, {hard_dropped} hard-dropped ({threshold}+ soft-axis "
         f"failures), seniority drops: {seniority_high} high / {seniority_low} low ({n_cached} from cache)")
    return candidates


def _listing_salary_suffix(c: dict) -> str:
    salary_min, salary_max = c.get("salary_min"), c.get("salary_max")
    if not salary_min and not salary_max:
        return ""
    if salary_min and salary_max:
        return f" | Salary: {salary_min}-{salary_max}"
    return f" | Salary: {salary_min or salary_max}"


def _rank_prompt(profile: dict, listing_block: str) -> str:
    multi_note = (
        "\nNote: the candidate has more than one distinct role interest; judge fit ONLY against the "
        "target roles listed above for THIS batch, not any other goals they may have listed elsewhere -- "
        "don't penalize a listing for not matching an unrelated interest of theirs.\n"
        if profile.get("_multi_cluster") else ""
    )
    salary_floor = profile.get("salary_floor") or 0
    return f"""You are estimating how well each job listing fits ONE candidate, as a rough numeric score.
This score gates which listings proceed to detailed review -- a wrong score buries a job silently, so
when genuinely unsure between two scores, prefer the higher one.

Candidate target roles: {_annotate_with_weight_tiers(profile.get('search_terms') or [], profile.get('target_role_weight_tiers'))}
Candidate seniority: {profile.get('seniority', 'mid-level')}
Candidate core skills: {_annotate_with_weight_tiers(profile.get('key_skills') or [], profile.get('skill_weight_tiers'), profile.get('skill_evidence_tiers'))}
Candidate location: {profile.get('location') or 'none stated'}
Candidate work-type preference: {', '.join(profile.get('work_types') or []) or 'none stated'}
Candidate stated salary floor: {salary_floor if salary_floor else 'none stated'}
{_candidate_background_block(profile)}{multi_note}
Some listing text is scraped from a web page and may include unrelated boilerplate around the actual job:
site navigation, a page footer, a "Similar jobs" list or a salary histogram carrying OTHER roles' figures.
Judge only the posting itself -- never treat a salary, requirement or location from such a footer as this
role's (it would misfire the SALARY/LOCATION downgrades below).

HARD DOWNGRADES -- check these FIRST. If a listing's visible text CLEARLY shows any of the following,
that listing scores 15 or below no matter how well the function matches, and its note must name which
one fired. Each needs CLEAR, explicit evidence in the text shown -- some listings below are tagged as
truncated source teasers; silence, ambiguity, or truncation NEVER triggers a downgrade, and a listing
is never downgraded for merely failing to confirm something.
a. EXPERIENCE BAR: the listing states a minimum professional-experience requirement (e.g. "2+ years
   as a data analyst", "proven commercial experience in X") that the candidate's evidence above
   clearly does not meet. Evidence tagged "self-directed", "academic", "ai-assisted", "one-time", or
   "familiar evidence only" -- or a background of degree/personal projects with no paid role in the
   function -- is NOT professional experience here: matching the listed tools does not clear a stated
   experience bar. Wording like "internships count" only helps if the candidate actually evidences one.
b. REQUIRED CREDENTIAL OR TOOL: the listing names a specific certification, qualification level, or
   tool as REQUIRED (not nice-to-have) and nothing in the candidate's skills/background above
   evidences it or plainly covers it.
c. CLOSED LISTING: the text says the vacancy is closed -- e.g. "the application deadline has now
   passed", "no longer accepting applications".
d. LOCATION / WORK ARRANGEMENT: first classify the LISTING's own arrangement -- explicit remote/
   distributed/work-from-home wording means remote; explicit hybrid wording means hybrid; a stated
   city/office with no remote/hybrid mention means ON-SITE there (never remote-by-default). Downgrade
   only if that classified arrangement clearly cannot work given the candidate's stated location and
   work-type preference above (e.g. on-site or hybrid in another country), or the listing requires an
   already-held work permit / right-to-work in a country that clearly isn't the candidate's.
e. SALARY: the listing states a salary clearly below the candidate's stated floor (never a downgrade
   when either is unstated or the ranges could plausibly overlap).

SCORING -- two components, in this order (for listings with no hard downgrade):
1. FUNCTION MATCH (the primary driver of the score): does the role's actual day-to-day work match the
   target roles above -- a same-function role in a different industry is a good match; a different-
   function role in the candidate's own industry is not. Within "analyst"-type titles specifically,
   distinguish analytical work (interpreting data, building insights, reporting) from operational work
   (data entry, processing, validation, administration) -- a role titled "Analyst" or "Technician" that is
   mostly the latter is a WEAKER function match than the title alone suggests, even within the right field.
   A listing tagged "[gate note: role-function fit vs target roles was ambiguous, not a confirmed match]"
   means an earlier, shorter-text screening pass could not confidently tell -- judge FUNCTION MATCH
   yourself from the fuller text below rather than assuming it's already settled; score it on what you
   actually see, high or low.
2. DEPTH FIT (a secondary adjustment -- do NOT let it override a poor function match): given the
   candidate's background evidence above, how plausible is it they can do THIS role's tasks at its stated
   seniority? Use this to move the score up or down a moderate amount within a function-match band, not to
   rescue a role whose core function doesn't match. A target role or skill tagged "strongly
   preferred"/"preferred" reflects the candidate's own past tick feedback -- nudge the score up a little for
   a strong match on it. One tagged "deprioritize"/"lower priority" reflects past cross feedback -- nudge
   the score down a little if the listing leans heavily on it.

Give each listing a fit_score from 0 (clearly wrong fit) to 100 (excellent fit). Judge relatively across
the whole batch -- spread scores out rather than clustering everything near one number.

Output ONLY JSON: {{"scores":[{{"n":1,"fit_score":72,"note":"..."}},{{"n":2,"fit_score":40,"note":"..."}}]}}
"note": one short phrase (under 12 words) naming the main driver of the score -- e.g. "strong function +
title match" or "operational role, weak function match despite title". For a hard-downgraded listing,
the note MUST name the downgrade, e.g. "hard: 3+ years paid experience bar" or "hard: on-site Cyprus,
candidate UK". For audit purposes only, never shown to the candidate. Include one object per listing,
numbered exactly as shown.

Listings:
{listing_block}"""


def _score_rank_batch(
    batch: list[tuple[dict, str]], profile: dict
) -> tuple[dict[int, float], dict[int, str], bool]:
    """Scores one rank_gate batch. Returns (scores, notes, batch_failed) without
    mutating `batch`'s candidate dicts or any shared cache/entry list, so rank_gate
    can run this concurrently across batches -- the caller applies the result back
    onto its own candidates/new_entries in the main thread. `notes` is the judge's
    short audit phrase per listing (see _rank_prompt's "note" field) -- for a
    borderline drop, this is the only record of WHY it scored low enough to be cut
    before the expensive judge ever saw it (see engine.py's Snapshot panel)."""
    def _teaser_tag(c: dict) -> str:
        # See RANK_TEASER_MARKER_CHARS: a job with only its source API's ~500-char
        # teaser gets tagged so the model knows the requirements section isn't
        # visible (vs. a short posting that genuinely says this little).
        text = c.get("full_text") or c.get("snippet") or ""
        if not c.get("_has_full_text") and len(text) < RANK_TEASER_MARKER_CHARS:
            return ("[truncated source teaser -- only the posting's opening is visible; "
                    "its requirements section is likely cut off] ")
        return ""

    listing_block = "\n".join(
        f"{i+1}. {c['title']} @ {c.get('company','')} | "
        f"{(c.get('location') or 'location unknown')}{_listing_salary_suffix(c)} | "
        f"{_teaser_tag(c)}"
        f"{'[gate note: role-function fit vs target roles was ambiguous, not a confirmed match] ' if c.get('_sector_ambiguous') else ''}"
        f"{(c.get('full_text') or c.get('snippet') or '')[:RANK_LISTING_TEXT_CHARS]}"
        for i, (c, _k) in enumerate(batch)
    )
    prompt = _rank_prompt(profile, listing_block)
    scores: dict[int, float] = {}
    notes: dict[int, str] = {}
    # See _FIXED_TEMPERATURE_MODELS -- gpt-5.6-luna 400s on temperature=0 just
    # like gpt-5.6-terra does on 0.2, which was silently tripping the
    # fail-open except branch below on every single rank_gate batch (every
    # job scored a flat neutral 50.0, i.e. no real ranking signal at all)
    # until this was added.
    rank_temperature = 1 if MID_MODEL in _FIXED_TEMPERATURE_MODELS else 0
    rank_system = ("You estimate rough candidate-job fit scores. Spread scores out; "
                    "don't cluster everything near one value.")

    raw = None
    try:
        raw = llm(prompt, require_json=True, temperature=rank_temperature, model=MID_MODEL,
                  system=rank_system)
    except Exception as e:
        status = getattr(e, "status_code", None)
        resp = getattr(e, "response", None)
        req_id = resp.headers.get("x-request-id") if resp is not None else None
        retry_after = resp.headers.get("retry-after") if resp is not None else None
        try:
            wait = float(retry_after) if retry_after else 2.0
        except (TypeError, ValueError):
            wait = 2.0
        # A permission-flavored error on MID_MODEL that only hits some batches
        # (not every call) looks like a burst/short-window cap rather than a
        # persistent per-key model restriction -- retry the SAME model once
        # after a short pause before falling back, since a burst cap should
        # clear within a second or two while a real scoping error wouldn't.
        emit(f"[gate:rank] {MID_MODEL} call failed (status={status}, "
             f"request_id={req_id}, retry_after={retry_after}): {e}; "
             f"retrying same model after {wait}s.")
        time.sleep(wait)
        try:
            raw = llm(prompt, require_json=True, temperature=rank_temperature, model=MID_MODEL,
                      system=rank_system)
        except Exception as e2:
            status2 = getattr(e2, "status_code", None)
            emit(f"[gate:rank] {MID_MODEL} retry also failed (status={status2}): {e2}; "
                 f"falling back to {CHEAP_MODEL}.")
            try:
                raw = llm(prompt, require_json=True, temperature=0, model=CHEAP_MODEL,
                          system=rank_system)
            except Exception as e3:
                emit(f"[gate:rank] {CHEAP_MODEL} fallback also failed ({e3}); "
                     f"no rank signal for this batch.")
                raw = None

    batch_failed = raw is None
    if raw is not None:
        try:
            for d in json.loads(clean_json(raw)).get("scores", []):
                n = d.get("n")
                if isinstance(n, int):
                    try:
                        scores[n] = max(0.0, min(100.0, float(d.get("fit_score", 50))))
                    except (TypeError, ValueError):
                        scores[n] = 50.0
                    # "|" is the delimiter rank_gate uses to pack score+note into
                    # gate_cache's single reused text column -- strip any so a
                    # note can never corrupt that encoding.
                    note = str(d.get("note", "") or "").strip().replace("|", "/")[:100]
                    if note:
                        notes[n] = note
        except Exception as e4:
            emit(f"[gate:rank] batch response parse failed ({e4}); no rank signal for this batch.")
            batch_failed = True

    if batch_failed:
        emit(f"[gate:rank] no rank signal for {len(batch)} candidate(s) in this batch -- "
             f"fail-open (bypassing RANK_REJECT_SCORE_FLOOR).")
    return scores, notes, batch_failed


def rank_gate(candidates: list[dict], profile: dict) -> list[dict]:
    """Middle-tier (MID_MODEL) numeric fit ranking over the post-gate survivor
    pool, so the expensive full-text judge only ever sees a curated top slice
    instead of every gate survivor. Runs on MID_MODEL rather than CHEAP_MODEL
    (unlike screen_gate) because a well-calibrated relative ordering across the
    whole batch benefits more from extra reasoning power than screen_gate's
    coarser binary sector/seniority check does -- and unlike screen_gate, this
    stage only ever sees the much smaller post-gate survivor pool, so it gets a
    larger per-listing excerpt (RANK_LISTING_TEXT_CHARS, see its own comment)
    deliberately reaching further into the JD than screen_gate's coarser check
    needs, while still well short of the judge's full FINAL_EVAL_JOB_TEXT_CHARS
    budget -- the cost delta over the cheap tier stays small even so, since this
    stage runs on a fraction of screen_gate's volume. Annotates each candidate
    with `_rank_score` (0-100, higher is better) and `_rank_note` (short audit
    phrase, "" if none) in place and returns the full list unfiltered -- the
    caller applies its own cutoff (e.g. drop the bottom fraction, cap at N).
    Cached per (profile signature, job id) in gate_cache under gate="rank_v7"
    (bumped from "rank_v6": the prompt gained a boilerplate-scope guard telling the
    model to ignore a scraped page's nav/footer/"Similar jobs"/salary-histogram
    sections so a neighbouring role's salary can't misfire the SALARY/LOCATION
    downgrades -- a score computed without that guard, against text that may carry
    such a footer, isn't comparable and must not be served stale. "rank_v6" was
    bumped from "rank_v5": the prompt gained the candidate's location/work-type
    preference/salary floor -- previously never in this prompt at all, so an
    on-site-abroad listing could score 84 with no way to know it was even a
    candidate for rejection -- plus a HARD DOWNGRADES section (a clearly-stated
    experience bar the candidate's evidence doesn't meet, a required named
    credential/tool with no evidence, a closed/expired listing, a clear
    location/arrangement conflict, or salary clearly under the stated floor now
    cap the score at 15, mirroring the final judge's DISQUALIFIER rules that a
    live mismatch audit showed this stage scoring 80+ against), a truncated-
    teaser tag on listings that only carry their source API's ~500-char opening
    blurb, and the structured salary line in the listing block. "rank_v5" was
    bumped from "rank_v4": the prompt gained a paragraph on how to weigh a
    "[gate note: ... ambiguous ...]" tag (see screen_gate's sector_confidence and
    _score_rank_batch's listing_block), and the per-listing text budget grew from
    GATE_LISTING_TEXT_CHARS to the new, larger RANK_LISTING_TEXT_CHARS -- a score
    computed under the old, shorter excerpt and without the gate-note context
    isn't comparable and must not be served stale. "rank_v4" was bumped from
    "rank_v3" when the prompt was restructured into an explicit FUNCTION MATCH
    (primary) / DEPTH FIT (secondary) split with an operational-vs-analytical
    distinction and an audit "note" field -- see _rank_prompt; a score computed
    under the old flattened wording isn't comparable and must not be served
    stale. "rank_v3" was bumped from "rank_v2" when the prompt dropped the
    separate LLM-inferred `sectors` guess and switched to judging role fit purely
    by FUNCTION against the candidate's target roles -- see _screen_prompt's
    ROLE FUNCTION FIT block for why. "rank_v2" itself was bumped from "rank" when
    this moved to MID_MODEL, to force re-scoring instead of serving stale
    cheap-tier scores); reuses the `reason` text column to hold "score|note" (no
    schema change needed) since `keep` has no binary meaning here -- "|" is
    stripped out of any note before storage (see _score_rank_batch) so the
    encoding can't be corrupted by the model's own output."""
    if not candidates:
        return []
    sig = _profile_signature(profile)
    # "rank_v7" (not "rank_v6"): the gate name doubles as part of the cache key, and
    # _gate_cache_key has no model field -- bumping it forces every previously
    # scored job to be re-ranked under the reworded prompt (now with the boilerplate-
    # scope guard, see the docstring) instead of serving a stale score forever. Bump
    # again if the rank model/prompt changes again.
    keys = [_gate_cache_key("rank_v7", sig, _gate_job_id(c)) for c in candidates]
    cached = _gate_cache_lookup(keys)

    to_judge: list[tuple[dict, str]] = []
    for c, key in zip(candidates, keys):
        if key in cached:
            _keep, reason, _req_json = cached[key]
            score_part, _, note_part = (reason or "").partition("|")
            try:
                c["_rank_score"] = float(score_part)
            except (TypeError, ValueError):
                c["_rank_score"] = 50.0
            c["_rank_note"] = note_part
        else:
            to_judge.append((c, key))

    n_cached = len(candidates) - len(to_judge)
    new_entries: list[tuple[str, bool, str, str | None]] = []
    batches = [to_judge[start:start + _GATE_BATCH] for start in range(0, len(to_judge), _GATE_BATCH)]
    if batches:
        # Capped concurrency, not serial and not full concurrency: serial batches
        # were most of this stage's wall-clock time (each a blocking LLM call), but
        # _score_rank_batch's own 401 retry path above already suspects a burst/
        # short-window rate cap on MID_MODEL -- firing every batch at once risks
        # making that worse rather than better, so this stays bounded.
        with ThreadPoolExecutor(max_workers=min(3, len(batches))) as pool:
            futures = [pool.submit(_score_rank_batch, batch, profile) for batch in batches]
            for batch, fut in zip(batches, futures):
                scores, notes, batch_failed = fut.result()
                for i, (c, key) in enumerate(batch):
                    if batch_failed:
                        # Cosmetic placeholder only -- _rank_gate_failed (not this score) is
                        # what engine.py's floor check actually keys off of. Not cached: a
                        # failure that isn't fully understood yet should retry fresh next
                        # run instead of permanently poisoning gate_cache with no signal.
                        c["_rank_score"] = 50.0
                        c["_rank_note"] = ""
                        c["_rank_gate_failed"] = True
                    else:
                        score = scores.get(i + 1, 50.0)
                        note = notes.get(i + 1, "")
                        c["_rank_score"] = score
                        c["_rank_note"] = note
                        new_entries.append((key, True, f"{score}|{note}", None))

    _gate_cache_store(new_entries)
    # Score-distribution diagnostic: a rank stage that never rejects anything
    # is indistinguishable from a healthy one in the old "(N from cache)"-only
    # log line -- this surfaces the actual spread so a run where the mid-tier
    # model is clustering everything above the reject floor is visible without
    # having to separately query gate_cache.
    all_scores = [c.get("_rank_score", 50.0) for c in candidates]
    n_failed = sum(1 for c in candidates if c.get("_rank_gate_failed"))
    if all_scores:
        emit(f"[gate:rank] scored {len(candidates)} candidates ({n_cached} from cache, "
             f"{n_failed} fail-open/no-signal) -- "
             f"min={min(all_scores):.0f} max={max(all_scores):.0f} "
             f"avg={sum(all_scores)/len(all_scores):.0f}")
    return candidates


def select_top_n(candidates: list[dict], n: int = TOP_CANDIDATES) -> list[dict]:
    """Stage 5: deterministic top-N by embed_score. No LLM call -- a stable sort
    on a score we already computed, not a subjective 'pick your best 25' task."""
    return sorted(candidates, key=lambda x: x.get("embed_score", 0), reverse=True)[:n]


def rank_candidates(candidates: list[dict], profile: dict) -> list[dict]:
    """Standalone-path convenience: run both gates then take the deterministic
    top-N. The backend (engine.py) calls the stages individually for per-stage
    logging; this keeps `python full_auto.py` working end-to-end."""
    if not candidates:
        emit("[phase 4] No candidates found passing basic structural vector checks.")
        return []
    emit(f"[phase 4] Gating {len(candidates)} candidates (sector -> seniority)...")
    survivors = seniority_gate(sector_gate(candidates, profile), profile)
    top = select_top_n(survivors, TOP_CANDIDATES)
    emit(f"[phase 4] {len(top)} listings selected for extraction hydration.")
    return top


# ── Phase 5: Anti-Bot Bypassing Full Detail Scrape ──────────────────────────────

def _scrape_host(url: str) -> str:
    try:
        return urlsplit(url).netloc.lower().split(":")[0]
    except ValueError:
        return ""


_REDIRECT_STUB_RE = re.compile(
    r"you(?:'re| are) (?:now )?being redirect|if you(?:'re| are) not redirected|redirecting you to",
    re.I,
)


def _looks_like_redirect_stub(markdown: str) -> bool:
    """Some aggregator click-through links (e.g. Adzuna's /jobs/land/ad/...
    tracking redirect) resolve to a real 200 OK, but the page is just a short
    'you are being redirected to X' interstitial, not the actual posting.
    crawl4ai has no way to know that isn't real content -- without this check
    it passes the length>150 success bar and gets persisted as if it were the
    job description. Length-gated so a genuinely long posting that happens to
    mention "redirect" in passing is never mistaken for a stub."""
    return len(markdown) < 2000 and bool(_REDIRECT_STUB_RE.search(markdown))


def _effective_status_code(result) -> int | None:
    """Status of the page markdown was actually extracted from. Prefers
    redirected_status_code (the final landed page after any redirect chain)
    over status_code, which this crawl4ai build sets to the FIRST hop's status
    -- a 301/302 when a redirect occurred, not the real final-page status. A
    dead listing that redirects to a 200 OK "closed" page would otherwise read
    as a 3xx here and never be caught."""
    return getattr(result, "redirected_status_code", None) or getattr(result, "status_code", None)


_EXPIRED_LISTING_RE = re.compile(
    r"no longer (?:accepting|taking) applications|"
    r"applications? (?:are|is) now closed|"
    r"this (?:vacancy|job|role|position|posting|listing|advert(?:isement)?) "
        r"(?:has|have) (?:now )?(?:closed|expired)|"
    r"this (?:vacancy|job|role|position|posting|listing) is no longer (?:available|active|live)|"
    r"(?:vacancy|position|role) has (?:already )?been filled|"
    r"job (?:posting|listing|advert) has expired|"
    r"(?:this )?posting has been removed|"
    r"sorry,? this job is no longer (?:available|live)",
    re.I,
)


def _looks_like_expired_listing(markdown: str) -> bool:
    """True if the rendered page reads as a notice that the listing itself has
    closed/expired/been filled/removed, on an otherwise-normal 200 OK page --
    crawl4ai has no way to flag this as a failure since markdown/status look
    fine on their own. Deliberately BACKWARD-looking phrasing only ("has
    closed", "no longer accepting applications", "has been filled") -- never
    forward-looking deadline language ("applications close 15 August", "apply
    by Friday"), which real, live postings use routinely and must never trip
    this. Length-gated like _looks_like_redirect_stub, sized larger: a genuine
    closure notice page usually still carries site nav/related-jobs
    boilerplate around it, unlike a bare redirect stub."""
    return len(markdown) < 3000 and bool(_EXPIRED_LISTING_RE.search(markdown))


def _dead_listing_signal(result, markdown: str) -> str | None:
    """A short machine-readable reason ('status_404'/'status_410'/
    'expired_phrase') ONLY when this fetch is a HIGH-CONFIDENCE signal the
    listing itself is gone -- as opposed to an ambiguous failure (anti-bot
    block, rate-limit, timeout, generic 4xx/5xx, empty shell) that must stay
    on the existing fail-open snippet-fallback path. 403/429/5xx are
    deliberately excluded: those mean "blocked/rate-limited/erroring", not
    "gone". Only 404/410 (HTTP-spec "not found"/"permanently gone") and an
    explicit closure-phrase match count."""
    status = _effective_status_code(result)
    if status in (404, 410):
        return f"status_{status}"
    if markdown and _looks_like_expired_listing(markdown):
        return "expired_phrase"
    return None


def _scrape_succeeded(result, markdown: str) -> bool:
    """Whether this fetch counts as a real-posting scrape success. A >=400
    status is NEVER a success regardless of markdown length or content --
    crawl4ai has no way to know an anti-bot/soft-404 error page's rendered
    text isn't real content."""
    status = _effective_status_code(result)
    if status is not None and status >= 400:
        return False
    return bool(result.success and markdown and len(markdown) > 150
                and not _looks_like_redirect_stub(markdown)
                and not _looks_like_expired_listing(markdown))


# Every scrape's CrawlerRunConfig sets markdown_generator=DefaultMarkdownGenerator
# (content_filter=PruningContentFilter()), so crawl4ai also produces a nav/ads/
# footer-stripped `fit_markdown` alongside the raw page markdown -- see
# _best_markdown. Without this, result.markdown is the ENTIRE page's raw markdown
# with no content filtering at all: a live audit of a real Reed.co.uk posting
# (https://www.reed.co.uk/jobs/junior-sql-data-analyst-south-manchester/57063092)
# found its raw markdown led with the full site nav (Jobs/Courses/Career advice/
# Post a job/Register CV/Saved jobs/Sign in/search box/breadcrumbs/...) before the
# actual job title and description started -- roughly 250+ characters of pure
# navigation noise. That pushed the real posting content substantially further
# into the text than GATE_LISTING_TEXT_CHARS's truncation window expected, and the
# weak gate's listing_ok check misread the truncated nav-heavy excerpt as board
# boilerplate/a category page rather than a real single posting.
# Aggregator/job-board pages append a footer that is NOT part of the posting: a
# salary histogram ("Stats for this job" / "The number of jobs in each salary
# range"), a "Similar jobs" list carrying OTHER roles' salaries, and email-alert
# chrome. crawl4ai's density-based PruningContentFilter does not reliably drop it
# (real headings + real figures survive), so it can land in full_text and mislead
# the final judge -- e.g. reading a neighbouring job's "£50,000 - £55,000" as THIS
# role's salary. Cut everything from the earliest high-confidence footer marker
# that appears as its own line; these strings essentially never occur inside a real
# JD body, and line-anchoring (tolerating markdown heading/emphasis punctuation)
# avoids mid-sentence false hits.
_FOOTER_MARKERS = [
    "stats for this job",
    "receive similar jobs by email",
    "the number of jobs in each salary range",
    "similar jobs",
    "create alert",
]
_FOOTER_MARKER_RE = re.compile(
    r"(?im)^[\s#>*_+.\-]*(?:" + "|".join(re.escape(m) for m in _FOOTER_MARKERS) + r")[\s:*_.\-]*$"
)


def _strip_boilerplate_footer(md: str) -> str:
    """Trim an aggregator's non-posting footer (salary histogram / "Similar jobs" /
    alert chrome) from scraped markdown -- see _FOOTER_MARKERS. No-op when no marker
    is found, or when trimming would leave almost nothing (the marker probably
    matched real content, or the page wasn't a real posting to begin with)."""
    if not md:
        return md
    m = _FOOTER_MARKER_RE.search(md)
    if not m:
        return md
    trimmed = md[:m.start()].rstrip()
    if len(trimmed) < 200:
        return md
    return trimmed


def _best_markdown(result) -> str:
    """Prefers crawl4ai's content-filtered `fit_markdown` (main content only) over
    the raw page markdown, falling back to raw when fit_markdown is missing or
    implausibly short (a handful of words) -- an unusual page layout the pruning
    heuristic mishandles should still yield *something* to judge against rather
    than an empty/near-empty string. Either way, a known aggregator footer is
    stripped (see _strip_boilerplate_footer)."""
    md = result.markdown
    if md is None:
        return ""
    fit = getattr(md, "fit_markdown", None)
    if fit and len(fit.strip()) >= 200:
        return _strip_boilerplate_footer(fit.strip())
    return _strip_boilerplate_footer(str(md).strip())


async def _find_alternate_posting(
    job: dict, crawler: AsyncWebCrawler, country_code: str = "gb",
) -> str:
    """Search for the same job posting on a different site when the original link
    can't be scraped -- a dead link, a click-tracking redirect stub, or a page an
    anti-bot wall blocked. Tries each organic result's real page in turn (skipping
    the original host, which by definition already failed), and returns the first
    one that reads like a genuine posting. "" if the search itself fails or every
    candidate page also fails -- callers fall back to the snippet as before."""
    query = f"{job.get('title', '')} {job.get('company', '')}".strip()
    if not query:
        return ""
    try:
        results = _google_organic(query, gl=country_code, num=5)
    except Exception:
        return ""
    orig_host = _scrape_host(job.get("url", ""))
    for r in results:
        link = r.get("link", "")
        host = _scrape_host(link)
        if not link or not host or host == orig_host:
            continue
        try:
            run_config = CrawlerRunConfig(
                cache_mode=CacheMode.BYPASS, wait_until="networkidle", page_timeout=20000,
                markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
            )
            result = await crawler.arun(url=link, config=run_config)
            markdown = _best_markdown(result)
            if _scrape_succeeded(result, markdown):
                return markdown[:8000]
        except Exception:
            continue
    return ""


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n\s*\n")


def _distinctive_sentence(text: str) -> str | None:
    """Pick one ~12-30 word sentence from a job's scraped body text, suitable
    for a quoted duplicate-content search -- prefers a real prose sentence (an
    "About Us" blurb or similar) over a title/location line or bullet
    fragment. Splits on blank lines as well as sentence punctuation, so a
    header block (title/location with no terminal punctuation) doesn't get
    glued onto the first real prose sentence that follows it. None if nothing
    in the text looks distinctive enough to search on (verification is
    skipped in that case, not forced)."""
    for s in _SENTENCE_SPLIT_RE.split((text or "")[:2000]):
        s = s.strip()
        words = s.split()
        if 12 <= len(words) <= 30 and 60 <= len(s) <= 220 and s[:1].isupper():
            return s
    return None


# Generic job-board/aggregator sites (careerjet, bebee, opcionempleo, indeed, etc.)
# mirror real postings verbatim as their normal SEO/aggregation business model --
# scraping ATS pages, other boards, and Google Jobs. Finding a listing's own text
# duplicated on one of these is expected of ANY real posting (the more widely a
# genuine role is distributed, the MORE likely this is, not less) and is not
# evidence of a lead-gen/CV-farming scam, so a hit on one of these must not count
# as corroboration. Matched as a substring of the host so one fragment (e.g.
# "careerjet") covers every country-code TLD variant (careerjet.com.qa,
# careerjet.co.uk, careerjet.fr, ...) without listing each one. Also includes
# reed/adzuna since this pipeline already treats those as legitimate primary
# sources in their own right -- a cross-post there is a positive signal, if anything.
_KNOWN_JOB_AGGREGATOR_FRAGMENTS = (
    "careerjet", "bebee", "opcionempleo", "indeed", "glassdoor", "ziprecruiter",
    "jooble", "trovit", "jobrapido", "whatjobs", "simplyhired", "linkedin",
    "monster", "talent.com", "jobisjob", "jobted", "mitula", "neuvoo",
    "learn4good", "adzuna", "reed.co.uk", "jora.com", "receptix",
)


def _is_known_job_aggregator(host: str) -> bool:
    host = (host or "").lower()
    return any(frag in host for frag in _KNOWN_JOB_AGGREGATOR_FRAGMENTS)


def verify_not_duplicated(job: dict, country_code: str = "gb") -> str | None:
    """Cross-site corroboration for a judge-flagged `scam_suspect` pick: search a
    distinctive sentence from the listing's own scraped text in quotes, and check
    whether it verbatim-appears on an unrelated, differently-hosted site that ISN'T
    a known job-board/aggregator (see _is_known_job_aggregator -- those mirror real
    postings verbatim as routine SEO/aggregation, so a hit there proves nothing).
    The caller (engine.py) gates this to only the rare already-suspicious pick,
    bounded by SCAM_VERIFY_MAX_PER_RUN, since it spends a real search call. Fails
    open: any search/parse issue, or no distinctive sentence available, returns
    None -- never itself a disqualifier."""
    sentence = _distinctive_sentence(job.get("full_text", ""))
    if not sentence:
        return None
    orig_host = _scrape_host(job.get("url", ""))
    try:
        results = _google_organic(f'"{sentence}"', gl=country_code, num=5)
    except Exception:
        return None
    needle = sentence.lower()
    for r in results:
        host = _scrape_host(r.get("link", ""))
        if not host or host == orig_host or _is_known_job_aggregator(host):
            continue
        haystack = f"{r.get('title','')} {r.get('snippet','')}".lower()
        if needle in haystack or needle[:40] in haystack:
            return f"verbatim duplicate content found on {host}"
    return None


async def scrape_full_details(
    jobs: list[dict], crawler: AsyncWebCrawler, blocked_domains: frozenset[str] | set[str] = frozenset(),
    total_budget_seconds: float = 60.0, country_code: str = "gb",
    sem: "asyncio.Semaphore | None" = None, alt_budget: list[int] | None = None,
) -> list[dict]:
    """Fetches each job's real page, capped concurrency, with retries. Every
    source shares one concurrency lane (Adzuna used to be forced single-lane
    "anti-bot caution", but live testing showed it scrapes fine alongside
    everything else -- the serialization was only adding minutes of pure sleep
    on runs with several Adzuna candidates, not avoiding any actual blocking).
    blocked_domains (exact host or subdomain match) skip straight to the
    snippet fallback -- no point paying retries/timeouts for a domain already
    known bad. total_budget_seconds caps the WHOLE phase's wall-clock time (not
    just one job's timeout) -- whatever hasn't finished by then falls back to
    its snippet rather than letting a handful of slow pages stretch the run.
    A job that exhausts its own retries tries _find_alternate_posting once
    before falling back to the snippet, spending from the shared
    ALT_SOURCE_LOOKUP_MAX_PER_RUN budget -- not applied to blocklist skips,
    which are a deliberate user opt-out, not a failure worth searching around.

    `sem` and `alt_budget` let a caller that invokes this SEVERAL TIMES
    CONCURRENTLY (engine.py scrapes each role cluster on its own task now, so
    each cluster's judge can start as soon as its own pages are ready) share one
    concurrency lane and one alt-source lookup budget across those calls. Left at
    None -- the standalone path and any single-call caller -- each call gets its
    own, exactly as before."""
    emit(f"\n[phase 5] Fetching full pages for {len(jobs)} jobs...")

    if sem is None:
        sem = asyncio.Semaphore(MAX_CONCURRENT)
    blocked_hit = 0
    alt_found = 0
    dead_confirmed = 0
    if alt_budget is None:
        alt_budget = [ALT_SOURCE_LOOKUP_MAX_PER_RUN]

    async def fetch_one(job: dict) -> dict:
        nonlocal blocked_hit, alt_found, dead_confirmed
        url = job.get("url", "")
        host = _scrape_host(url)
        if host and any(host == d or host.endswith("." + d) for d in blocked_domains):
            blocked_hit += 1
            job["full_text"] = job.get("snippet", "")
            return job

        async with sem:
            max_retries = 2
            dead_signal = None
            for attempt in range(1, max_retries + 1):
                try:
                    await asyncio.sleep(random.uniform(1.5, 3.5))

                    # Force browser instance to pause until asynchronous JS redirect tracking resolves
                    run_config = CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        wait_until="networkidle",
                        page_timeout=28000,
                        markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
                    )

                    result = await crawler.arun(url=url, config=run_config)
                    markdown = _best_markdown(result)
                    dead_signal = _dead_listing_signal(result, markdown)

                    if _scrape_succeeded(result, markdown):
                        job["full_text"] = markdown[:8000]
                        if attempt > 1:
                            emit(f"   [RETRY SUCCESS] Bypassed script wall for {job['company']} on attempt #{attempt}")
                        break
                    elif dead_signal:
                        raise ValueError(f"Listing appears dead/expired ({dead_signal}).")
                    elif markdown and _looks_like_redirect_stub(markdown):
                        raise ValueError("Scraper landed on a click-tracking redirect stub, not the real posting.")
                    else:
                        raise ValueError("Scraper returned an empty page shell or incomplete markup structure.")

                except Exception:
                    if attempt == max_retries:
                        alt_text = ""
                        if alt_budget[0] > 0:
                            alt_budget[0] -= 1
                            alt_text = await _find_alternate_posting(job, crawler, country_code)
                        if alt_text:
                            job["full_text"] = alt_text
                            alt_found += 1
                            emit(f"   [ALT-SOURCE] Recovered {job['company']} posting via search after scrape failure")
                        elif dead_signal:
                            job["_dead_reason"] = dead_signal
                            job["full_text"] = job.get("snippet", "")
                            dead_confirmed += 1
                            emit(f"   [CONFIRMED DEAD] {job['company']} -- {dead_signal}; "
                                 f"no alternate posting found, excluding before final judge")
                        else:
                            emit(f"   [!] [PHASE 5 FAILURE] Blocked at {job['company']}. Preserving snippet summary.")
                            job["full_text"] = job.get("snippet", "")
                    else:
                        emit(f"   [BLOCKED/SHELL] Cool-down applied for {job['company']} (Attempt #{attempt}). Retrying...")

        return job

    try:
        await asyncio.wait_for(
            asyncio.gather(*[fetch_one(j) for j in jobs], return_exceptions=True),
            timeout=total_budget_seconds,
        )
    except asyncio.TimeoutError:
        emit(f"   [phase 5] hit the {total_budget_seconds:.0f}s total scrape budget; "
             f"falling back to snippets for whatever didn't finish in time")

    # Anything cancelled by the budget timeout above (or skipped for any other
    # reason) never got its full_text set -- make sure every job still has
    # *something* to judge against.
    for j in jobs:
        if not j.get("full_text"):
            j["full_text"] = j.get("snippet", "")

    if blocked_hit:
        emit(f"   [phase 5] skipped {blocked_hit} already-blocklisted domain(s), no scrape attempted")
    if alt_found:
        emit(f"   [phase 5] recovered {alt_found} otherwise-failed page(s) via alternate-source search")
    if dead_confirmed:
        emit(f"   [phase 5] confirmed {dead_confirmed} listing(s) dead/expired (404/410 or explicit "
             f"closure notice, no alternate posting found) -- excluded before the final judge")
    return jobs


# ── Category-page expansion (discovery-adjacent, not Phase 5) ───────────────

async def expand_category_pages(
    category_hits: list[dict], crawler: AsyncWebCrawler,
    max_pages: int = CATEGORY_EXPAND_MAX_PAGES,
    max_links_per_page: int = CATEGORY_EXPAND_MAX_LINKS_PER_PAGE,
    total_budget_seconds: float = CATEGORY_EXPAND_BUDGET_SECONDS,
) -> list[dict]:
    """Follows a bounded number of Google-organic hits that
    _looks_like_category_page flagged as board category/search-listing pages,
    and extracts individual job-posting links from each, instead of dropping
    the whole page. Cheap/bounded by design: no snippet is fetched here --
    extracted candidates flow into the normal pipeline with an empty snippet,
    and _needs_full_scrape (engine.py) naturally queues them for Phase 5 like
    any other thin-snippet candidate."""
    if not category_hits:
        return []
    batch = category_hits[:max_pages]
    if len(category_hits) > max_pages:
        emit(f"   [category_expand] {len(category_hits)} category page(s) this run; "
             f"expanding the first {max_pages} (CATEGORY_EXPAND_MAX_PAGES cap)")

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    out: list[dict] = []

    async def expand_one(hit: dict) -> None:
        url = hit.get("url", "")
        host = hit.get("_category_host") or _scrape_host(url)
        async with sem:
            try:
                run_config = CrawlerRunConfig(
                    cache_mode=CacheMode.BYPASS, wait_until="networkidle",
                    # Matches the standard Phase 5 scrape timeout (full_auto.py:2289)
                    # rather than a shorter one-off -- 15s was tighter than every
                    # other networkidle wait in the pipeline, and these are the
                    # same kind of JS-heavy listing pages Phase 5 already budgets
                    # 20s for.
                    page_timeout=20000,
                )
                result = await crawler.arun(url=url, config=run_config)
            except Exception as e:
                emit(f"   [!] [category_expand] failed to fetch {url}: {e}")
                return
            if not result or not result.success:
                emit(f"   [category_expand] {url} -> fetch reported failure, 0 links available")
                return
            # crawl4ai classifies internal/external by comparing each <a href>'s
            # netloc against the page's FINAL resolved URL, not the requested
            # `url` above -- a bare-domain-to-www (or http-to-https) redirect on
            # the category page can put every real posting link into `external`
            # instead of `internal`, which a fetch-succeeded 0-internal-links
            # result can't distinguish from "the page genuinely has no links yet"
            # (e.g. not-yet-hydrated JS). Pull both lists and let
            # _posting_link_reject_reason's own _same_or_related_host check
            # (which already tolerates a subdomain relationship) do the actual
            # host filtering, rather than trusting crawl4ai's split.
            links_obj = getattr(result, "links", None)
            internal_links = list(getattr(links_obj, "internal", None) or [])
            external_links = list(getattr(links_obj, "external", None) or [])
            links = internal_links + external_links
            from collections import Counter
            reject_reasons: Counter = Counter()
            kept, seen_urls = 0, set()
            for link in links:
                reason = _posting_link_reject_reason(link, host)
                if reason is not None:
                    reject_reasons[reason] += 1
                    continue
                if kept >= max_links_per_page:
                    reject_reasons["over_page_cap"] += 1
                    continue
                href = link.href
                if href in seen_urls:
                    reject_reasons["duplicate_on_page"] += 1
                    continue
                seen_urls.add(href)
                text = re.sub(r"\s+", " ", (link.text or link.title or "").strip())[:140]
                out.append({
                    "board": "google_jobs", "title": text, "company": "",
                    "url": href, "location": hit.get("location", ""), "snippet": "",
                })
                kept += 1
            if kept:
                emit(f"   [category_expand] {url} -> {kept} individual posting(s) extracted")
            else:
                emit(f"   [category_expand] {url} -> 0 kept; raw links={len(links)} "
                     f"(internal={len(internal_links)}, external={len(external_links)}), "
                     f"rejected breakdown={dict(reject_reasons)}")

    try:
        await asyncio.wait_for(
            asyncio.gather(*[expand_one(h) for h in batch], return_exceptions=True),
            timeout=total_budget_seconds,
        )
    except asyncio.TimeoutError:
        emit(f"   [category_expand] hit the {total_budget_seconds:.0f}s budget; "
             f"stopping (partial results kept)")

    emit(f"   [category_expand] expanded {len(batch)} page(s) -> {len(out)} new candidate posting(s)")
    return out


# ── Phase 6: Final Evaluation ────────────────────────────────────────────────────

# Bumped whenever DISQUALIFIERS/STRONG_RULES/WORDING/SCHEMA changes meaningfully --
# engine.py folds this into eval_sig so a prompt edit re-opens every already-persisted
# verdict on the next run instead of serving it stale forever. Same fix as rank_gate's
# "rank_v2" cache-key bump when its model/prompt changed.
FINAL_EVAL_PROMPT_VERSION = 15

_FINAL_EVAL_QUOTE_PROTOCOL = """QUOTE-THEN-CLASSIFY (applies to every disqualifier below before you exclude a role under
it): quote the exact clause you're relying on, verbatim, max 20 words, then classify it HARD
(stated as mandatory -- "must have", "required", "X+ years required", a named eligibility
restriction) or SOFT (a preference or ideal-candidate sketch -- "would suit", "ideal for",
"we'd love", "looking for someone with roughly", "a nice to have"). "Would suit X" / "ideal
candidate has X" / "would suit someone with roughly N months/years" is SOFT regardless of any
number attached -- it describes who tends to apply, not a condition of hire. Only a HARD
clause may disqualify a role; a SOFT one is judged as a normal fit signal instead (and noted
in "concerns" if material, never used to exclude). When you DO disqualify, the quoted clause
must appear inside the "reason" string in "disqualified" so the exclusion stays auditable --
see the SCHEMA below."""

_FINAL_EVAL_DISQUALIFIERS = """1. SENIORITY/EXPERIENCE: Check whether the job states an explicit experience/seniority requirement
   (years of experience, "senior"/"lead"/"principal" in the title, or prior experience in a specific
   sector/domain). If the job clearly requires meaningfully more experience or specific sector
   experience the candidate does not have, treat that as disqualifying, not a minor caveat - exclude
   the role unless the candidate's transferable experience genuinely closes the gap. Apply the
   QUOTE-THEN-CLASSIFY protocol above: a requirement phrased as who the role "would suit" or as an
   "ideal candidate" sketch is SOFT even when it names a number of years, and must not disqualify - if
   the requirement is soft, negotiable, or not stated, judge fit on skills/interests as normal, don't
   invent a seniority objection that isn't in the text.
   A stated salary/pay figure is a real signal of the role's TRUE seniority band and often more
   trustworthy than the title itself (titles get inflated or watered down; what an employer is
   actually paying usually doesn't). Weigh it alongside the title/description when judging the real
   bar in axis A below -- ordinary judgement for the sector/region/currency shown, not a fixed
   number, and never a disqualifier from salary alone when the figure is ambiguous or absent.
   If the job specifically requires COMMERCIAL, PROFESSIONAL, or PAID employment experience (e.g. "1-2
   years commercial software development experience"), personal projects, academic coursework,
   hackathons, and other unpaid/self-initiated work do NOT satisfy it, even if they demonstrate real
   skill - treat that as a genuine gap unless the candidate has actual paid/commercial evidence closing
   it. The candidate's background profile marks an unpaid/self-initiated past role explicitly as
   "(Informal)", and a self-directed/academic/AI-assisted skill explicitly with an origin tag like
   "(Self-directed)", "(Academic)", or "(AI-assisted)" next to it (see EVIDENCE STRENGTH below) - use
   those tags to tell commercial from non-commercial evidence rather than assuming.

2. LOCATION/VISA/RELOCATION: First classify the listing's own work arrangement: if it explicitly says
   remote/distributed/work-from-home, treat it as remote; if it explicitly says hybrid, treat it as
   hybrid; otherwise -- including when it states a specific city/office location and simply doesn't
   mention remote/hybrid/work-from-home at all -- treat it as on-site at that location, not remote.
   If the role's location under that classification (or an explicit on-site/relocation/visa/work-
   authorization requirement in the text) clearly puts it outside where the candidate can realistically
   work, exclude it. If location is remote, or plainly compatible with the candidate's location above,
   do not raise a location objection.
   If the listing's own location/eligibility signals are internally contradictory (e.g. a "compatible
   timezone" framing alongside an explicit country-selector or eligibility list that excludes the
   candidate's country), do not silently resolve the contradiction either way - keep the role but add a
   concern naming the specific contradiction so the candidate can verify eligibility before applying.

3. LISTING TYPE: The listing must be a real, direct job vacancy the candidate could be hired into. If
   it is actually a paid training course, "traineeship"/placement programme, bootcamp, or any scheme
   where the candidate enrols in (or pays/finances) training and is only promised a job or interview
   afterwards rather than being hired directly, exclude it entirely - it is not a job.

4. SCAM / CV-FARMING RISK: Some listings are not genuine hiring employers but lead-generation or
   CV-harvesting operations designed to collect applications/CVs rather than fill a real role. Judge on
   the COMBINATION of signals below, the same way you weigh cumulative fit gaps elsewhere - no single
   softer signal is automatically disqualifying alone (a genuine small/informal employer can trip one),
   but TWO OR MORE of the softer signals together should be treated as disqualifying:
   - Content-free "About Us"/company description: generic corporate language naming no company, no
     product/service, no domain specifics (e.g. "a leading organization at the forefront of [field],
     committed to innovation and excellence") - genuine postings, even from small companies, almost
     always name themselves or say concretely what they build/do.
   - A "lure" combination aimed at maximizing applicant volume rather than filtering for fit: visa
     sponsorship offered + a paid training/induction period + an unusually high salary, together, for a
     genuinely zero-experience graduate-level role.
   - A posting-volume note in the listing block (when present): the source has posted an unusually high
     number of differently-titled roles this run with the same templated structure - a pattern
     associated with template/lead-gen job boards rather than a single real hiring pipeline.
   These softer signals are ADDITIVE, not independently sufficient - set "scam_suspect": true (see
   schema below) whenever exactly ONE is present so it can be corroborated before the listing reaches
   the candidate, and only move straight to "disqualified" when two or more coincide.
   IMPORTANT EXCEPTION for recruitment/staffing agencies: if the listing block carries a
   "[source: ...]" note identifying it as sourced via a registered ATS/careers-page account (Workable,
   Greenhouse, Lever, Ashby, Recruitee, Personio), that confirms a real, currently-operating company or
   agency account sits behind it - it is not a scraped or self-submitted page. For these, an anonymized
   "one of our clients is hiring" framing and a high same-run posting-volume note are BOTH normal,
   expected traits of a legitimate recruitment/staffing agency operating that account (agencies routinely
   advertise many similar roles for undisclosed end-clients) - do not count either one toward the
   two-signal threshold on its own for a listing carrying that source note. Only disqualify such a
   listing under this rule if an independently-sufficient hard signal below is also present, or if the
   content-free "About Us" signal describes the AGENCY itself with no concrete detail about what it
   recruits for or which sector it operates in.
   The following are independently sufficient - either ALONE disqualifies immediately, no combination
   needed: payment, purchase, bank/financial, or sensitive-ID (passport, National Insurance/SSN) requests
   as a condition of applying or being hired; contact/apply only via a personal Gmail/Yahoo/Outlook
   address, WhatsApp, Telegram, or SMS number, with no company website, careers page, or ATS link
   anywhere in the listing.
   Do NOT flag a listing merely for imperfect writing, being from a small/unfamiliar company that
   nonetheless names itself concretely with a specific role description, using a normal ATS apply link,
   or a normal post-offer background check - only the concrete patterns above, never a vague "feels off"
   impression.

5. SECTOR / DOMAIN FIT: Exclude a role whose core professional domain or job function is clearly in a
   different field from what the candidate is targeting - judged against their target roles, stated
   sector interests, and their own words about what they are looking for (all in the profile above).
   Use a HIGH bar: only genuinely unrelated professional fields disqualify (e.g. a hands-on nursing
   role for a marketing candidate, a field-sales role for someone targeting research/policy, a
   qualified-accountant role for a software engineer). Do NOT exclude adjacent, transferable, or
   specialisation-level differences within the same broad field, or a role that plausibly applies the
   candidate's core skills in a new domain - those are normal and acceptable. When the candidate targets
   more than one distinct field, judge sector fit against the NEAREST one, never penalise a role for not
   matching their OTHER field. If genuinely unsure whether the field is unrelated, do not raise a sector
   objection.
   Passing this rule does NOT mean the role matches the candidate's preferred sectors/causes - it only
   means the field isn't clearly wrong. Whether it actually lands in a sector the candidate said they
   want is judged separately, as a RANKING signal rather than a gate - see "sector_match" in reasoning
   step G and the SCHEMA below.

6. REQUIRED LANGUAGE / EXPLICIT HARD REQUIREMENT: If the listing states an explicit, mandatory
   requirement outside seniority/location - a required spoken or written language for the role (e.g.
   "work is conducted primarily in Japanese", "fluent Mandarin required"), a specific required
   certification/license, or a named tool/technology stated as mandatory (not "nice to have") - and the
   candidate's profile shows no evidence of it, treat this as disqualifying. If the requirement is
   phrased as preferred/a plus/negotiable, or the candidate's profile directly shows the
   language/certification/tool, do not raise this objection.

7. CANDIDATE HARD FILTERS: The candidate's profile may state their OWN non-negotiables as
   "HARD REQUIREMENTS (a role must satisfy all of these)" and/or "HARD EXCLUSIONS (reject a role that
   clearly involves any of these)". Exclude a role that CLEARLY involves one of the hard exclusions, or
   that clearly contradicts / cannot satisfy one of the hard requirements. Apply the same clear-violation
   bar as the rules above: when the listing is silent on the point or it is genuinely ambiguous, do NOT
   exclude on that basis - keep the role and, if the point is material, note it as a concern for the
   candidate to verify. If the profile states no such hard filters, this rule does not apply."""

_FINAL_EVAL_WORDING = """WORDING: When you reference the candidate's OWN background in "summary", "top_match_reason" or
"concerns", never state a leadership or founder title (e.g. president, chair, founder, co-founder,
cofounder, CEO, director, co-lead) on its own. If such a title came from a student club, society, campaign
group, fellowship, or other informal/unpaid activity, name the SPECIFIC organisation or activity it belongs
to, exactly as given in the candidate's profile (e.g. "co-lead of the Oxford AI Safety Society", "president
of the Debating Society", "co-founded the university's sustainability campaign") - do not flatten it into a
vague generic paraphrase that drops which specific thing it was (e.g. "ran a student society", "led an
initiative"), and never state the bare title with no object at all. If the profile text gives no specific
name to attach, leave the title out entirely rather than stating it bare - never phrase any of this so it
could read as company-founding or executive experience.

PLAIN LANGUAGE: "role_type" and "summary" are read by the candidate before anything else on the result
card, displayed as ONE continuous sentence pair (role_type immediately followed by summary, with
nothing in between) - so write them the way you'd explain the role to a friend outside the field,
concrete everyday words for what the person actually spends their day doing, and write them as two
halves of one thought rather than two independent descriptions. Avoid listing-style marketing language
("drive synergies", "stakeholder engagement", "dynamic self-starter") even when the JD itself uses it;
translate it into what that actually means in practice. Do not let "summary" restate "role_type" in
different words - see reasoning step F for the no-overlap rule between them.

NO LOCATION COMMENTARY: never mention location, remote/hybrid/on-site arrangement, relocation, or visa
status in "summary", "role_type", "can_do_fit", "concerns", or "top_match_reason" - that's already shown
to the candidate via the work_style fact and handled by the LOCATION/VISA/RELOCATION disqualifier above,
so repeating it in prose is redundant noise, not a fit signal."""

_FINAL_EVAL_STRONG_RULES = """8. EVIDENCE STRENGTH: The candidate's background profile may show one or two qualifiers in
   parentheses next to a skill or past role. For skills the first is a depth signal, e.g. "Python (Expert)"
   or "Excel (One-time)" - Expert/Proficient stated experience is strong evidence; Familiar/One-time
   exposure is weak evidence - weigh each accordingly. A skill may ALSO carry an origin tag - "Commercial",
   "Self-directed", "Academic", or "AI-assisted", e.g. "SQL (Proficient, AI-assisted)" or "Salesforce
   (Self-directed)" - showing where that depth was actually earned, separate from how deep it is.
   Read origin on TWO SEPARATE AXES, not one flattened word, and weigh them independently:
   - EXECUTION: how the skill was actually exercised - self-written/commercial execution is stronger
     evidence of unaided competency than AI-assisted or heavily-guided execution at the same stated depth.
   - CONTEXT: what the skill was applied to - a real, live, production system, real users, or a shipped
     product is stronger evidence of practical competency than a sandbox, tutorial, or coursework
     exercise, even when the execution itself was assisted.
   A short tag like "(Proficient, AI-assisted)" only carries the EXECUTION axis - read it at face value.
   A fuller clause elsewhere in the profile (e.g. in a "Skill evidence detail" or background-summary line,
   such as "wrote extraction queries via Python/psycopg2 against live production data -- AI-assisted, not
   from scratch, but functional and used in a real pipeline") can carry BOTH axes, and they can point
   opposite ways for the very same skill - do not collapse that down to the single weakest word. Credit
   the CONTEXT axis for a requirement about practical/real-world exposure even when EXECUTION was
   assisted; discount the EXECUTION axis for a requirement about unaided technical authorship; and when
   the split matters, name it explicitly in "concerns" (e.g. "SQL query authorship was AI-assisted, though
   built into a self-written production pipeline against live data" - a materially different, more
   favorable statement than "AI-assisted" alone).
   In general, still treat Self-directed/Academic/AI-assisted EXECUTION as weaker support than
   Commercial/untagged execution for any requirement that implies real-world or professional competency -
   this applies whenever the requirement itself implies professional-level use, NOT only when the job
   listing explicitly uses the word "commercial" (see the [key requirements] hint on each job below, where
   "professional-level expected" items should be weighed this way in particular). A candidate practicing a
   tool alone in a sandbox, on a personal project, in coursework, or leaning on AI assistance with no real
   context to show for it has NOT demonstrated the same thing as someone who used it professionally, even
   at similar stated depth.
   For past roles, the only qualifier used is "(Informal)", which flags a student-club, society, or
   volunteer position rather than paid employment, e.g. "President (Informal)". Treat an Informal-tagged
   past role as materially weaker evidence of professional/commercial competency than an untagged (real
   employment) past role of similar or even longer standing - a multi-year unpaid club position does not
   substitute for paid work experience.
   When the candidate's only support for a specific hard requirement (a named tool, a specific process
   like invoice/expense handling or diary/calendar management, a certification) is a generic or unrelated
   soft-skill/reliability anecdote (e.g. safety-critical responsibility, leadership of an unrelated
   activity, an Informal-tagged role, or a Self-directed/Academic/AI-assisted-tagged skill), that is NOT
   evidence the requirement is met unless the connection to the requirement is direct and explicitly
   stated - do not present it as satisfying the requirement in "can_do_fit" or "top_match_reason". Put
   any such gap in "concerns" instead, naming the specific origin/depth limitation (e.g. "Salesforce
   experience is self-directed/sandbox, not production or paid use").
   Also weigh CUMULATIVE nice-to-have gaps: several compounding smaller gaps (e.g. no fintech background
   AND no dbt AND no BI tooling) can together make a role a weak fit even when no single gap is
   disqualifying. Report each such gap separately in "concerns"."""

_FINAL_EVAL_REASONING = """HOW TO JUDGE EACH ROLE -- work through this reasoning before deciding which list a role belongs in.
This is a genuine fit assessment, NOT a keyword/similarity check: presence of a matching word is not
evidence the requirement is met.
A. READ THE JOB on three axes, not at face value (it is a marketing document as much as a spec):
   - Required vs nice-to-have: separate the genuinely mandatory requirements from the wish-list. Use the
     [key requirements] hint where present, and also read the full text -- listings pad their requirements.
   - The REAL seniority bar: an "entry-level"/"junior-friendly"/"graduate" label can be marketing. If the
     listed responsibilities, years, or scope imply a higher bar than the label, judge against the REAL bar.
     A stated salary is one of the more reliable signals here too -- see the SENIORITY/EXPERIENCE
     disqualifier above.
   - Actual day-to-day vs aspirational language: what will this person actually DO most days, as distinct
     from the mission/impact framing the listing leads with.
B. Assess WANT-FIT and CAN-DO-FIT SEPARATELY -- they are different questions:
   - want-fit: does the candidate actually WANT this role -- judged against their target roles, stated
     sector interests, and their OWN words about what they're looking for? A role the candidate is
     well-qualified for but clearly does NOT want (wrong function, a domain they've moved away from,
     something their own words rule out) is NOT a strong fit however well the skills line up. A strong
     can-do-fit must never paper over a weak want-fit. This assessment isn't reported in its own field --
     it feeds the strong/backup decision and the synthesis in "top_match_reason" (step E below).
   - can-do-fit: can the candidate actually DO the job to the REAL bar from A -- weighing evidence
     STRENGTH, not mere presence (see EVIDENCE STRENGTH). "Used professionally, 2 years" is strong
     evidence; "self-directed, one project" is weak evidence for the very same skill tag. Report this
     as a direct, second-person verdict in "can_do_fit" (e.g. "You're mostly qualified for this role,
     though..." or "You'd be a stretch here -- ..."), the way you'd tell the candidate to their face.
   A role belongs in "strong" only when BOTH want-fit and can-do-fit are genuinely strong.
C. List every notable gap in "concerns", ONE item per gap (a missing requirement, weak evidence for a
   load-bearing skill, a seniority gap, a want-fit mismatch worth flagging) -- put the single one most
   likely to sink this application FIRST, since the candidate sees these as a plain count before they
   expand the list.
D. Build a REQUIREMENTS CHECKLIST (internal reasoning only -- not shown to the candidate, used purely to
   keep this judgment disciplined): list the JD's individually-judgeable requirements (both explicitly
   stated and clearly implied), each tagged "core" (the requirements identified as genuinely mandatory in
   axis A -- the JD's "required" asks, or anything that would independently sink the application if
   entirely absent) or "secondary" (axis A's nice-to-have/preferred asks -- matters, but not independently
   disqualifying). For each, judge "met" true whenever the candidate's profile shows clear, genuine
   evidence for it -- including self-directed/academic/AI-assisted evidence, for a requirement that does
   NOT itself imply professional/production-level competency (e.g. "familiarity with X", "a relevant
   degree", "attention to detail", a tool the candidate has genuinely used even informally). Reserve "met":
   false for a requirement with no real evidence at all, OR one that specifically implies professional/
   production-grade competency where only weak/self-directed/AI-assisted evidence exists (see EVIDENCE
   STRENGTH). Do not let a strict evidence read collapse EVERY requirement to unmet just because the
   candidate is early-career -- an entry-level candidate genuinely satisfying most secondary asks and some
   core ones is the normal, expected outcome, not an exception; a checklist that comes back all-false
   carries no information for the candidate and should prompt you to re-check whether you're over-applying
   the professional-competency bar to requirements that never asked for it. Typically 4-10 core items and
   2-6 secondary items; never exceed 12 total -- don't pad to hit a count, and don't split one requirement
   into several near-duplicates to inflate it.
E. Write "top_match_reason" as a short (2-4 sentence), first-person narrative in your own voice explaining
   why you ranked this role the way you did -- e.g. "I rank this role as a strong fit because ..." --
   synthesizing the want-fit and can-do-fit reasoning from step B into flowing prose a candidate can read
   standalone, not a restatement of "concerns" or a bare list of keywords. If "sector_match" (step G) is
   false for this role, say so plainly here -- e.g. "this sits outside the [sector] work you said you
   want, but ..." -- so that trade-off is visible rather than silent.
F. Classify the FUNCTIONAL NATURE of the day-to-day work in "role_type" -- one short sentence naming the
   kind of role this is (e.g. "This is a programme delivery role featuring admin and facilitation tasks",
   "This is a technical individual-contributor engineering role", "This is a client-facing sales role").
   "role_type" and "summary" are displayed to the candidate as ONE continuous sentence pair, "role_type"
   immediately followed by "summary" - so they must be NON-OVERLAPPING: "role_type" stays at the
   functional-category level only, and "summary" (which describes what this SPECIFIC role/project/mission
   is for) must add NEW information the candidate doesn't already have from "role_type", never restate the
   same category in different words. For example, if "role_type" is "This is a charity data-and-impact role",
   "summary" must NOT also independently describe the work as "combining analysis, data-quality work, and
   support for colleagues" (that's the same functional-category information "role_type" already gave) -
   it should instead name the concrete duties/mission, e.g. "You would keep service records accurate,
   analyse outcomes, and turn evidence into reports for funders and partners."
G. SECTOR MATCH (a ranking signal, never a disqualifier -- see rule 5 above): for every role you include
   in "strong" or "backup", set "sector_match" true if its core domain/employer sits within one of the
   candidate's stated sector interests or causes (their sector_target values and their own words), false
   if it doesn't. This never excludes a role or changes "fit_level" on its own -- it only orders roles
   within the same fit_level ("sector_match": true first) and, when you include a role despite
   "sector_match": false, name that trade-off plainly in "top_match_reason" (step E) so it's visible to
   the candidate rather than silently absorbed into the verdict."""

_FINAL_EVAL_SCHEMA = """Output ONLY a valid JSON object (no markdown), with two required lists and one
optional list, using this item shape for "strong"/"backup":
{"strong": [
  {
    "job_number": 1, "title": "...", "company": "...", "url": "...",
    "role_type": "1 short sentence classifying the FUNCTIONAL NATURE of the day-to-day work -- see reasoning step F. Written FIRST, since it's shown immediately before \\"summary\\" as one continuous sentence pair.",
    "summary": "1 concise, PLAIN-LANGUAGE sentence on what this specific role/project/mission actually involves (not why it fits the candidate) -- see PLAIN LANGUAGE and NO LOCATION COMMENTARY above. Must add information NOT already given by \\"role_type\\" -- never restate its functional-category classification (see reasoning step F's no-overlap rule).",
    "fit_level": "very_strong" | "strong" | "ok" | "stretch",
    "sector_match": true,
    "can_do_fit": "a direct, second-person qualification verdict -- see reasoning step B.",
    "top_match_reason": "a short first-person narrative synthesizing why this role earned its verdict -- see reasoning step E.",
    "requirements": [{"text": "a JD requirement, short and concrete", "category": "core" | "secondary", "met": true}],
    "concerns": ["each notable gap, one per item, most sink-worthy first -- see reasoning step C; [] if none"],
    "role_salary": "the salary or range THIS posting's own description states, verbatim and short (e.g. \\"GBP 35,000-42,000\\"); null if this posting states none -- even when other salary figures appear elsewhere in the supplied text (a \\"Similar jobs\\" list or salary histogram, see SCOPE OF EACH POSTING'S TEXT)",
    "work_style": "Remote" | "Hybrid" | "On-site" | null,
    "role_seniority": "the role's REAL seniority bar from axis A (e.g. \\"Graduate\\", \\"Junior\\", \\"Mid\\", \\"Senior\\"); null if you genuinely can't tell",
    "deadline": "the application deadline the listing states, short (e.g. \\"15 August\\", \\"rolling\\"); null if it states none",
    "scam_suspect": false
  }
],
 "backup": [ {same item shape} ],
 "disqualified": [
   {"job_number": 3, "reason": "one short phrase naming which DISQUALIFIER applied, including the verbatim quoted clause you relied on (see QUOTE-THEN-CLASSIFY above)"}
 ]}
Include a "disqualified" entry for every job you excluded from BOTH lists above because it failed one of
the DISQUALIFIERS rules -- this is the only place that reasoning needs recording, so it stays auditable.
The "reason" must contain the exact quoted clause from QUOTE-THEN-CLASSIFY, not just a paraphrase of the
rule name, so a misfire can be checked against the listing text afterward. Do NOT add an entry for a job
that simply wasn't picked among the best-fitting options (one that passed the disqualifiers but wasn't
chosen for "strong"/"backup") -- leave those out of all three lists.
"scam_suspect" (on "strong"/"backup" items only): true if the SCAM/CV-FARMING rule's softer signals
raised exactly ONE flag on this listing (not enough alone to disqualify it into the list above); false
otherwise. Omit or leave false when you saw none of those signals.

"sector_match" (on "strong"/"backup" items only): see reasoning step G -- true if the role's core domain
sits within one of the candidate's stated sector interests, false otherwise. Never changes "fit_level" or
which list a role is in; only orders roles within a fit_level and is named in "top_match_reason" when
false for an included role.

"fit_level" grades the pick more finely than the list it's in, and must agree with that list:
items in "strong" are "very_strong" (both want-fit and can-do-fit are compelling, no serious
concerns) or "strong" (genuinely strong, one real but surmountable concern); items in
"backup" are "ok" (a plausible fit with real gaps) or "stretch" (they'd be reaching for it).
Grade honestly -- "very_strong" should be rare.

"role_salary"/"work_style"/"role_seniority"/"deadline" are FACTS READ OFF THIS POSTING, not
judgements about the candidate. Report only what this posting's own description actually says: use null
when it is silent, and never infer, estimate, or borrow a figure from another posting's text in the
payload (see SCOPE OF EACH POSTING'S TEXT). For "work_style" apply the same classification as the
LOCATION/VISA/RELOCATION rule -- a stated office location with no remote/hybrid/work-from-home
wording anywhere is "On-site", not null and not "Remote".

"can_do_fit" and "top_match_reason" are shown directly to the candidate as the headline "are you
qualified" and "why this matched" for this pick -- both must stand alone as short, readable, plain-
language text a candidate can understand without the rest of the analysis."""

# Static system prefix -- identical across every cluster/call, so it's a stable
# (prompt-cache-friendly) prefix instead of being rebuilt into each user prompt. It
# carries all the rules; the per-call user prompt is just the CV + the jobs payload.
_FINAL_EVAL_SYSTEM = f"""You are an elite talent placement advisor matching a candidate to open job vacancies.
You are given a candidate profile and a set of job postings, and must return TWO lists: "strong" and "backup".

SCOPE OF EACH POSTING'S TEXT -- read first. Each posting's text is scraped from a web page and may contain
unrelated boilerplate wrapped around the actual job description: site navigation, page footers, a "Similar jobs"
list, a "Stats for this job" salary histogram, "Receive similar jobs by email"/"Create alert" chrome, and
salaries, titles or locations belonging to OTHER postings. Judge ONLY the single posting named at the top of each
job block (its title/company/URL). Ignore anything that clearly belongs to a different job or to the site's own
chrome. In particular, never attribute a salary, deadline, location or requirement to this role unless it appears
in THIS posting's own description -- a figure from a "Similar jobs"/histogram footer is not this role's salary.

DISQUALIFIERS -- apply to EVERY role, for BOTH lists, first:
{_FINAL_EVAL_QUOTE_PROTOCOL}

{_FINAL_EVAL_DISQUALIFIERS}

"strong" -- genuinely strong fits ONLY. Beyond the disqualifiers above, also apply:
{_FINAL_EVAL_STRONG_RULES}
Include a role in "strong" only if it is a genuinely strong fit; never pad it with weak matches.

"backup" -- least-bad survivors, for when nothing (or too little) is strong. Include a role here only if
it passes the DISQUALIFIERS above. In this list, evidence weakness and cumulative nice-to-have gaps are
EXPECTED and ACCEPTABLE -- do NOT use them to exclude a role, only note them honestly in "concerns".
Leave "backup" empty when "strong" already gives good coverage, or when every role is disqualified.

{_FINAL_EVAL_REASONING}

{_FINAL_EVAL_WORDING}

{_FINAL_EVAL_SCHEMA}"""


def _final_eval_job_block(i: int, j: dict) -> str:
    """One job's payload block. A non-trivial screen note (the merged gate's seniority
    verdict, already computed upstream) is surfaced as a hint so the model focuses its
    seniority re-check rather than re-deriving it from scratch. This same mechanism
    also surfaces a "sector_ambiguous" note when screen_gate could not confidently
    tell whether the listing matches the candidate's target-role FUNCTION (see
    screen_gate's docstring) -- it flags the judge to look closely at role-function
    fit itself rather than assuming the gate already confirmed it (a confident
    mismatch never reaches here at all, since that's an unconditional gate drop). A
    posting-volume note
    (set upstream in engine.py when this job's company posted an unusually high number
    of differently-titled roles this run, see _company_title_counts) feeds the SCAM /
    CV-FARMING disqualifier's combination-of-signals check. A key-requirements note
    (screen_gate's capped requirements breakdown -- see _sanitize_key_requirements)
    gives the judge a pre-graded JD side to cross-reference against the candidate's
    own evidence_origin tags (see EVIDENCE STRENGTH), instead of re-deriving which
    requirements are load-bearing from the raw text on every call."""
    # _gate_reason is packed as "{seniority_code}|{requirements_code}|{skills_code}|
    # {salary_code}|{arrangement_code}|{hard_filter_code}|{listing_code}|
    # {sector_code}" (see screen_gate) -- split and surface only the non-"ok" parts
    # as the hint. sector_code is "sector_ambiguous" when relevant (a "mismatch"
    # never reaches this job block at all -- dropped before rank_gate/the judge).
    reason = j.get("_gate_reason") or ""
    parts = [p for p in reason.split("|") if p and p not in ("ok", "gate_error", "missing_decision")]
    hint = f"[screen note: {', '.join(parts)}]\n" if parts else ""
    # ATS-vendor board tags are "{vendor}:{token}" (see harvest_ats_tokens/company_ats
    # ingestion); API-sourced boards are flat names ("reed", "adzuna", "google_jobs",
    # ...) with no colon. Surfaced so the SCAM/CV-FARMING disqualifier (rule 4 above)
    # can tell a registered agency/employer ATS account apart from a scraped or
    # self-submitted listing -- see that rule's staffing-agency exception.
    board = j.get("board") or ""
    if ":" in board:
        vendor = board.split(":", 1)[0]
        hint += f"[source: sourced via a registered {vendor} ATS/careers-page account]\n"
    volume_hint = j.get("_posting_volume_hint")
    hint += f"[posting-volume note: {volume_hint}]\n" if volume_hint else ""
    key_reqs = j.get("_key_requirements") or []
    if key_reqs:
        req_text = ", ".join(
            f"{r['item']} ({r['necessity']}"
            + (", professional-level expected)" if r.get("professional_level_expected") else ")")
            for r in key_reqs
        )
        hint += f"[key requirements: {req_text}]\n"
    return (f"JOB {i+1}: {j['title']} at {j['company']}\n"
            f"Location: {j.get('location') or 'not stated'}\nURL: {j['url']}\n{hint}\n"
            f"{j.get('full_text','')[:FINAL_EVAL_JOB_TEXT_CHARS]}")


def _sanitize_requirements_checklist(raw) -> list[dict]:
    """Validate/cap the final judge's requirements-checklist output (see reasoning
    step D / _FINAL_EVAL_SCHEMA) -- at most 12 items, each normalised to a fixed
    shape, so a malformed or oversized response can't corrupt the persisted verdict
    or the card's core/secondary counts."""
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:12]:
        if not isinstance(r, dict):
            continue
        text = str(r.get("text", "")).strip()
        if not text:
            continue
        category = "secondary" if str(r.get("category", "")).lower() == "secondary" else "core"
        out.append({
            "text": text[:120],
            "category": category,
            "met": bool(r.get("met", False)),
        })
    return out


def _run_final_eval(jobs: list[dict], cv_text: str | None
                    ) -> tuple[list[dict], list[dict], list[dict]]:
    """One expensive-model call returning (strong, backup, disqualified) lists of merged
    job dicts. Replaces the old strict-then-relaxed two-call pattern: the single prompt
    asks for both a strict "strong" list and a lenient disqualifier-only "backup" list,
    so a round with no strong fits no longer costs a second full-payload call.
    "disqualified" carries a short AI-authored reason for any job hard-excluded by a
    DISQUALIFIERS rule -- without it, a reject verdict is persisted with zero reasoning,
    which made a past investigation into thin results unable to see why anything was
    excluded."""
    if not jobs:
        return [], [], []
    emit(f"[phase 6] Final matching processing matrix active ({EXP_MODEL})...")
    if cv_text is None:
        cv_text = open(CV_PATH, encoding="utf-8").read()
    cv_text = cv_text[:5000]

    jobs_block = "\n\n---\n\n".join(_final_eval_job_block(i, j) for i, j in enumerate(jobs))
    prompt = f"""Candidate Background Profile:
{cv_text}

Judge the {len(jobs)} complete job postings below. Return up to {FINAL_PICKS} genuinely strong fits in
"strong" (best first), and up to 3 least-bad disqualifier-only survivors in "backup" (best first; empty
if "strong" already covers it or nothing qualifies). For any job you hard-exclude from both lists via a
DISQUALIFIERS rule, add it to "disqualified" with a short reason.

Jobs Payload:
{jobs_block}"""

    # See _FIXED_TEMPERATURE_MODELS -- gpt-5.5/gpt-5.6-terra 400 on any
    # non-default temperature. Keyed off EXP_MODEL so switching models can't
    # silently 400 into the unverified-fallback path again without anyone
    # noticing (see 691cf89 -- that's exactly how this was missed for a full
    # day: the 400 was swallowed by the except branch below). Confirmed
    # gpt-5.6-terra also 400s on 0.2 -- it was silently hitting this fallback
    # on every single Phase 6 call until this was added.
    temperature = 1 if EXP_MODEL in _FIXED_TEMPERATURE_MODELS else 0.2
    try:
        raw = llm(prompt, system=_FINAL_EVAL_SYSTEM, model=EXP_MODEL, require_json=True, temperature=temperature)
    except Exception as e:
        status = getattr(e, "status_code", None)
        resp = getattr(e, "response", None)
        req_id = resp.headers.get("x-request-id") if resp is not None else None
        retry_after = resp.headers.get("retry-after") if resp is not None else None
        try:
            wait = float(retry_after) if retry_after else 2.0
        except (TypeError, ValueError):
            wait = 2.0
        # Same reasoning as rank_gate's _score_rank_batch retry: a permission/rate
        # error that doesn't hit every call looks like a transient burst cap rather
        # than a persistent scoping error, and a burst cap should clear within a
        # second or two -- retry the SAME model once instead of giving up this
        # cluster's entire judgment outright on a blip. Unlike rank_gate there is
        # no cheaper-model fallback after that: EXP_MODEL's judgment quality is the
        # whole point of this stage, and engine.py's caller already treats a
        # genuinely failed call correctly (falls back to an unverified top-N, see
        # the None sentinel below), so silently downgrading the judge model would
        # be worse than retrying once and otherwise failing honestly.
        emit(f"[phase 6] {EXP_MODEL} call failed (status={status}, request_id={req_id}, "
             f"retry_after={retry_after}): {e}; retrying same model after {wait}s.")
        time.sleep(wait)
        try:
            raw = llm(prompt, system=_FINAL_EVAL_SYSTEM, model=EXP_MODEL, require_json=True,
                      temperature=temperature)
        except Exception as e2:
            emit(f"[phase 6] {EXP_MODEL} retry also failed: {e2}")
            # None,None,None (not [],[],[]) -- a failed call must be distinguishable from a
            # real judgment that rejected everyone. The caller (engine.py) treats
            # the two very differently: a genuine rejection must never resurface,
            # while a failed call falls back to an unverified top-N so a transient
            # API error doesn't get silently recorded as a permanent rejection.
            return None, None, None

    try:
        data = json.loads(clean_json(raw))
    except Exception as e:
        emit(f"[phase 6] Final generation evaluation failed to parse: {e}")
        return None, None, None

    def _merge(entries, cap):
        out = []
        for entry in (entries or [])[:cap]:
            idx = entry.get("job_number", 1) - 1
            if 0 <= idx < len(jobs):
                merged = jobs[idx].copy()
                merged.update(entry)
                merged["requirements"] = _sanitize_requirements_checklist(merged.get("requirements"))
                # 700, not the old 300 -- top_match_reason is now a 2-4 sentence
                # synthesized narrative (reasoning step E), not a single short phrase.
                merged["top_match_reason"] = str(merged.get("top_match_reason") or "").strip()[:700]
                # Default true (not "unknown mismatch") when the model omits it -- this
                # is a ranking/display signal, never a gate, so a missing field should
                # never read as a silent sector-mismatch flag.
                merged["sector_match"] = bool(entry.get("sector_match", True))
                out.append(merged)
        return out

    return (_merge(data.get("strong"), FINAL_PICKS),
            _merge(data.get("backup"), min(3, len(jobs))),
            _merge(data.get("disqualified"), len(jobs)))


def final_evaluation_split(jobs: list[dict], profile: dict, cv_text: str | None = None
                           ) -> tuple[list[dict] | None, list[dict] | None, list[dict] | None]:
    """Backend entry point: (strong, backup, disqualified) from Phase 6. The backend
    uses `strong` when non-empty, else `backup` (tagged as a non-strong fallback);
    `disqualified` carries a short AI-authored reason for hard-excluded jobs, persisted
    into their reject verdict instead of leaving it blank. Returns (None, None, None)
    only if EVERY chunk's call failed -- see _run_final_eval's except branch -- so the
    caller can tell that apart from a real judgment that rejected everyone.

    A cluster within FINAL_EVAL_MAX_JOBS_PER_CALL is still exactly ONE call, same as
    before -- the model's own "best first" ordering (see the prompt above) is trusted
    as-is and nothing here re-sorts it. A larger cluster is split into concurrent chunk
    calls instead of one giant prompt risking the client's 90s read timeout -- each
    chunk is judged independently (no cross-chunk comparison), then chunk results are
    merged and FINAL_PICKS/backup caps re-applied across the merged cluster-level
    lists. Plain concatenation would only guarantee best-first WITHIN each chunk, not
    across the whole cluster (chunk order, not fit, would decide who ranks above whom)
    -- so the merged lists are re-sorted by `_rank_score`, the one comparable numeric
    signal every candidate already carries from the cheap rank_gate stage that ran
    before any chunking happened, giving the cluster a single true best-first order
    end to end. A chunk that fails simply contributes nothing (its jobs get no verdict
    this run, retried next run) rather than failing the whole cluster, as long as at
    least one other chunk succeeded."""
    if len(jobs) <= FINAL_EVAL_MAX_JOBS_PER_CALL:
        return _run_final_eval(jobs, cv_text)

    chunks = [jobs[i:i + FINAL_EVAL_MAX_JOBS_PER_CALL]
              for i in range(0, len(jobs), FINAL_EVAL_MAX_JOBS_PER_CALL)]
    emit(f"[phase 6] {len(jobs)} jobs split into {len(chunks)} concurrent batches "
         f"of <={FINAL_EVAL_MAX_JOBS_PER_CALL} (cluster exceeds single-call cap)")
    strong: list[dict] = []
    backup: list[dict] = []
    disqualified: list[dict] = []
    any_succeeded = False
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        futures = [pool.submit(_run_final_eval, chunk, cv_text) for chunk in chunks]
        for fut in futures:
            c_strong, c_backup, c_disqualified = fut.result()
            if c_strong is None:
                continue
            any_succeeded = True
            strong.extend(c_strong)
            backup.extend(c_backup)
            disqualified.extend(c_disqualified)

    if not any_succeeded:
        return None, None, None
    strong.sort(key=lambda j: j.get("_rank_score", 0.0), reverse=True)
    backup.sort(key=lambda j: j.get("_rank_score", 0.0), reverse=True)
    return strong[:FINAL_PICKS], backup[:min(3, len(jobs))], disqualified


def final_evaluation(jobs: list[dict], profile: dict, cv_text: str | None = None) -> list[dict]:
    """Judge `jobs`; return the best flat list -- strong fits, or the least-bad backups
    when nothing is strong. Standalone/CLI callers use this; `cv_text`, when given,
    overrides reading CV_PATH (the backend passes a role-cluster-scoped bio)."""
    strong, backup, _disqualified = _run_final_eval(jobs, cv_text)
    return (strong or backup) or []


# ── Phase 7: Save & Output ───────────────────────────────────────────────────────


def save_and_display(results: list[dict]):
    sep = "─" * 60
    emit(f"\n{'═'*60}\n  TOP PIPELINE MATCHES SUMMARY\n{'═'*60}")

    conn = get_db()
    for i, job in enumerate(results, 1):
        emit(f"\n#{i}  {job['title']}\n    {job['company']}\n    {job['url']}\n\n    {job.get('summary', '')}")
        if job.get("top_match_reason"):
            emit(f"    ✓  {job['top_match_reason']}")
        for c in job.get("concerns", []):
            emit(f"    ⚠  {c}")
        emit(f"\n{sep}")

        try:
            conn.execute(
                "INSERT OR IGNORE INTO jobs(job_id, title, company, url, ai_summary) VALUES(?,?,?,?,?)",
                (
                    job.get("job_id") or make_job_id(job.get("company", ""), job["url"]),
                    job["title"],
                    job["company"],
                    job["url"],
                    job.get("summary", ""),
                )
            )
        except Exception as e:
            emit(f"  [save] Local tracking DB write error: {e}")

    conn.commit()
    conn.close()

    with open(os.path.join(BASE_DIR, "results.md"), "w") as f:
        f.write("# Automated Job Placement Matching Output\n\n")
        f.write(f"*Generated Pipeline Sync: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n")
        for i, job in enumerate(results, 1):
            f.write(f"## {i}. {job['title']} — {job['company']}\n\n**Link:** {job['url']}\n\n{job.get('summary', '')}\n\n")
            if job.get("top_match_reason"):
                f.write(f"- ✓ {job['top_match_reason']}\n")
            for c in job.get("concerns", []):
                f.write(f"- ⚠ {c}\n")
            f.write("\n---\n\n")

    emit("\n[phase 7] Persistent artifacts successfully written to tracking DB and results.md.")


# ── Executable Lifecycle ────────────────────────────────────────────────────────

async def main():
    init_db()
    profile = get_profile()
    profile_embedding = get_profile_embedding(profile)

    raw_jobs = gather_jobs(profile)
    
    # Structural duplication pipeline cleaning
    seen = set()
    deduped = []
    for job in raw_jobs:
        key = (job["title"].lower(), job["company"].lower())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(job)

    candidates = api_candidates(deduped, profile_embedding)
    if not candidates:
        emit("[!] No jobs found aligning with target candidate embedding vectors.")
        return

    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
        viewport_width=1280,
        viewport_height=800,
        user_agent_mode="random",
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:
        top10 = rank_candidates(candidates, profile)
        top10 = await scrape_full_details(top10, crawler)

    final = final_evaluation(top10, profile)
    save_and_display(final)


if __name__ == "__main__":
    asyncio.run(main())