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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional, List, Dict
import numpy as np
from openai import OpenAI
from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CrawlerRunConfig
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

DEBUG_SAVE_RAW = True

# ── Dynamic Path Configuration ──────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "boards_cache.db")

# Automatically discover CV file in current directory or fallback gracefully
CV_PATH = os.path.join(BASE_DIR, "exp.txt")
if not os.path.exists(CV_PATH):
    CV_PATH = "/home/jamesstephens/Documents/search_auto/exp.txt"

# ── Global Tuning Hyperparameters ──────────────────────────────────────────────
CHEAP_MODEL         = "gpt-5.4-nano-2026-03-17"
EXP_MODEL           = "gpt-5.4"
EMBED_MODEL         = "text-embedding-3-small"

PROFILE_CACHE_DAYS  = 7
MAX_CONCURRENT      = 5       # Max general simultaneous crawl requests
RELEVANCE_THRESHOLD = 0.35    # Balanced threshold preventing snippet penalty
TOP_CANDIDATES      = 25      # Pool size handed to the final evaluator
FINAL_PICKS         = 12      # Max results returned, quality-gated

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))


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
        if len(results) < page_size:  # last page reached
            break
    return jobs


# Country-level location strings that Adzuna's `where` geocoder rejects (the cc
# endpoint already scopes the country, so these must be omitted, not passed).
_ADZUNA_COUNTRY_LEVEL = {
    "united kingdom", "great britain", "uk", "u.k.", "gb",
    "united states", "united states of america", "usa", "us", "u.s.",
    "canada", "australia", "germany", "france", "india", "italy",
    "netherlands", "austria", "poland", "singapore", "south africa",
}


def fetch_adzuna(query: str, location: str = "United Kingdom", country_code: str = "gb", pages: int = 3) -> List[Dict]:
    """Routes dynamically to the matching Adzuna global regional server. The page
    number is the last path segment (/search/{page}), so pagination just walks it."""
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []

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
    for page in range(1, pages + 1):
        url = f"https://api.adzuna.com/v1/api/jobs/{cc}/search/{page}"
        try:
            r = requests.get(url, params=base_params, timeout=12)
            body = r.json()
            results = body.get("results", [])
        except Exception as e:
            emit(f"   [!] Adzuna ({cc}) API Error: {e}")
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
        if len(results) < 50:  # last page reached
            break
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
        # Organic titles are often "Role - Company | Board" -- keep the role
        # part; company is unknown here (the final scrape hydrates the page).
        title = re.split(r"\s[-–|]\s", res.get("title", ""), 1)[0].strip()
        jobs.append({
            "board": "google_jobs", "title": title or res.get("title", ""),
            "company": "", "url": link, "location": clean_loc,
            "snippet": res.get("snippet", ""),
        })
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


class ReedSource:
    name, tier = "reed", "fast"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        country_code = profile.get("adzuna_country_code", "gb")
        location = _geo_scoped_location(profile)
        for term in _terms(profile):
            out.extend(fetch_reed(term, location, country_code))
        return out


class AdzunaSource:
    name, tier = "adzuna", "fast"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        location = _geo_scoped_location(profile)
        for term in _terms(profile):
            out.extend(fetch_adzuna(term, location,
                                    profile.get("adzuna_country_code", "gb")))
        return out


class GoogleJobsSource:
    name, tier = "google_jobs", "broad"   # organic discovery via serper.dev/SerpAPI
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        location = _google_location(profile)
        for term in _terms(profile):
            out.extend(fetch_google_jobs(term, location))
        return out


class JSearchSource:
    name, tier = "jsearch", "broad"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        location = _google_location(profile)
        for term in _terms(profile):
            out.extend(fetch_jsearch(term, location))
        return out


class RemotiveSource:
    name, tier = "remotive", "broad"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(fetch_remotive(term))
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


# ── Rotation + tiered first run ──────────────────────────────────────────────

def _load_cursor(profile: Dict) -> int:
    conn = get_db()
    row = conn.execute("SELECT value FROM profile_cache WHERE key='rotation_cursor'").fetchone()
    conn.close()
    return int(row["value"]) if row else 0


def _save_cursor(profile: Dict, cursor: int) -> None:
    conn = get_db()
    conn.execute("INSERT OR REPLACE INTO profile_cache(key, value) VALUES(?,?)",
                 ("rotation_cursor", str(cursor)))
    conn.commit()
    conn.close()


TERMS_PER_RUN = 4   # size of the rotating term window queried each run


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
    # different terms, and add one rotating broad source.
    start = (cur * TERMS_PER_RUN) % max(1, len(terms))
    profile["search_terms_batch"] = terms[start:start + TERMS_PER_RUN] or terms[:TERMS_PER_RUN]
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
    tasks: List[tuple] = [("src", s) for s in select_sources_for_run(profile)
                          if s.name not in disabled]
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
        if kind == "src":
            result = payload.fetch(profile, since=profile.get("since")) or []
            emit(f"   [source] {payload.name}: {len(result)} jobs")
            return result
        vendor, token = payload
        return fetch_ats(vendor, token)

    all_jobs: List[Dict] = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for fut in as_completed([ex.submit(run_task, t) for t in tasks]):
            try:
                all_jobs.extend(fut.result() or [])
            except Exception as e:
                emit(f"   [!] discovery task failed: {e}")

    if DEBUG_SAVE_RAW:
        with open(os.path.join(BASE_DIR, "raw_api_jobs.json"), "w") as f:
            json.dump(all_jobs, f, indent=2)

    return all_jobs


# ── Database Lifecycle ──────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
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
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
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
        "ashby":      re.compile(r"ashbyhq\.com/([A-Za-z0-9_-]+)"),
        "workable":   re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
        "recruitee":  re.compile(r"https?://([A-Za-z0-9_-]+)\.recruitee\.com"),
        "personio":   re.compile(r"https?://([A-Za-z0-9_-]+)\.jobs\.personio\."),
    }

    found: List[tuple] = []
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
                # Tag the row with the phrase that found it so the batch
                # selector can favour it for profiles in this sector.
                found.append((token, vendor, token, keyword))

    deduped = list({(c, v, t, kw) for c, v, t, kw in found})
    save_company_ats(deduped)
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
    }, sort_keys=True)
    return hashlib.sha1(basis.encode()).hexdigest()[:12]


def _gate_job_id(job: dict) -> str:
    """Stable per-job key. Prefer the backend's cross-source identity when present."""
    return job.get("_identity") or make_job_id(job.get("board", ""), job.get("url", ""))


def _gate_cache_key(gate: str, sig: str, job_id: str) -> str:
    return hashlib.sha1(f"{gate}|{sig}|{job_id}".encode()).hexdigest()


def _gate_cache_lookup(keys: list[str]) -> dict[str, tuple[bool, str]]:
    if not keys:
        return {}
    conn = get_db()
    out: dict[str, tuple[bool, str]] = {}
    # SQLite caps variables per statement; chunk to stay well under it.
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        placeholders = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT cache_key, keep, reason FROM gate_cache WHERE cache_key IN ({placeholders})",
            chunk,
        ).fetchall():
            out[r["cache_key"]] = (bool(r["keep"]), r["reason"] or "")
    conn.close()
    return out


def _gate_cache_store(entries: list[tuple[str, bool, str]]) -> None:
    if not entries:
        return
    conn = get_db()
    conn.executemany(
        "INSERT OR REPLACE INTO gate_cache(cache_key, keep, reason) VALUES(?,?,?)",
        [(k, 1 if keep else 0, reason) for (k, keep, reason) in entries],
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
            keep, _reason = cached[key]
            if keep:
                kept.append(c)
        else:
            to_judge.append((c, key))

    n_cached = len(candidates) - len(to_judge)
    new_entries: list[tuple[str, bool, str]] = []
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
            new_entries.append((key, keep, reason))
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


def _screen_prompt(profile: dict, listing_block: str) -> str:
    return f"""You are screening job listings for ONE candidate. For EACH listing judge TWO things independently.

SECTOR/DOMAIN FIT
Candidate target sectors/domains: {', '.join(profile.get('sectors') or []) or 'n/a'}
Candidate target roles: {', '.join(profile.get('search_terms') or []) or 'n/a'}
- sector_ok=true if the role is in one of these sectors/domains, or clearly adjacent.
- sector_ok=false only if it is in an unrelated field. When unsure, sector_ok=true.

SENIORITY / HARD REQUIREMENTS
Candidate seniority: {profile.get('seniority', 'mid-level')}
Candidate core skills: {', '.join(profile.get('key_skills') or []) or 'n/a'}
(Stated multi-year durations are stronger evidence than brief/undated mentions.)
- seniority_ok=false if the listing clearly implies a seniority level well ABOVE or well
  BELOW the candidate (e.g. Director/VP/Head/Principal for a mid-level candidate, or
  Intern/Graduate/Entry for a senior candidate), OR if it states more than
  {MAX_MUST_HAVE_GAPS} hard must-have requirements the candidate clearly lacks.
- otherwise seniority_ok=true. When unsure, seniority_ok=true.

reason: short code -- "ok" | "seniority_high" | "seniority_low" | "too_many_gaps" | "off_sector".
Output ONLY JSON: {{"decisions":[{{"n":1,"sector_ok":true,"seniority_ok":true,"reason":"ok"}}]}}
Include one object per listing, numbered exactly as shown.

Listings:
{listing_block}"""


def screen_gate(candidates: list[dict], profile: dict) -> list[dict]:
    """Merged sector+seniority screen (cheap model, temperature 0) that replaces the
    two separate gate calls in the backend path. Unlike sector_gate/seniority_gate it
    does NOT filter -- it ANNOTATES every candidate in place with `_sector_ok`,
    `_seniority_ok`, and `_gate_reason`, and returns the full list. The backend then
    hard-drops only off-sector jobs, DEMOTES (never drops) seniority failures, and
    guarantees a per-cluster floor, so a cheap gate can no longer starve a cluster to
    zero (the full-text final AI stays the authoritative seniority judge).

    Cached per (profile signature, job id) in the shared gate_cache under gate="screen":
    `keep` column stores sector_ok, `reason` stores the seniority verdict -- so no
    cache-table schema change is needed."""
    if not candidates:
        return []
    sig = _profile_signature(profile)
    keys = [_gate_cache_key("screen", sig, _gate_job_id(c)) for c in candidates]
    cached = _gate_cache_lookup(keys)

    to_judge: list[tuple[dict, str]] = []
    for c, key in zip(candidates, keys):
        if key in cached:
            sector_ok, reason = cached[key]
            c["_sector_ok"] = sector_ok
            c["_seniority_ok"] = reason not in _SENIORITY_BAD_CODES
            c["_gate_reason"] = reason or "ok"
        else:
            to_judge.append((c, key))

    n_cached = len(candidates) - len(to_judge)
    new_entries: list[tuple[str, bool, str]] = []
    for start in range(0, len(to_judge), _GATE_BATCH):
        batch = to_judge[start:start + _GATE_BATCH]
        listing_block = "\n".join(
            f"{i+1}. {c['title']} @ {c.get('company','')} | "
            f"{(c.get('location') or 'location unknown')} | {(c.get('snippet') or '')[:450]}"
            for i, (c, _k) in enumerate(batch)
        )
        prompt = _screen_prompt(profile, listing_block)
        decisions: dict[int, tuple[bool, bool, str]] = {}
        try:
            raw = llm(prompt, require_json=True, temperature=0,
                      system="You screen job listings for sector and seniority fit. Be inclusive when unsure.")
            for d in json.loads(clean_json(raw)).get("decisions", []):
                n = d.get("n")
                if isinstance(n, int):
                    decisions[n] = (bool(d.get("sector_ok", True)),
                                    bool(d.get("seniority_ok", True)),
                                    str(d.get("reason", "ok")))
        except Exception as e:
            emit(f"[gate:screen] batch parse failed ({e}); keeping batch (fail-open).")
            decisions = {i + 1: (True, True, "gate_error") for i in range(len(batch))}

        for i, (c, key) in enumerate(batch):
            sector_ok, seniority_ok, reason = decisions.get(i + 1, (True, True, "missing_decision"))
            c["_sector_ok"] = sector_ok
            c["_seniority_ok"] = seniority_ok
            c["_gate_reason"] = reason
            # Store a normalised seniority code so cache reads derive _seniority_ok
            # unambiguously (keep column already carries sector_ok).
            if seniority_ok:
                cache_reason = "ok"
            elif reason in _SENIORITY_BAD_CODES:
                cache_reason = reason
            else:
                cache_reason = "too_many_gaps"
            new_entries.append((key, sector_ok, cache_reason))

    _gate_cache_store(new_entries)
    in_sector = sum(1 for c in candidates if c.get("_sector_ok"))
    both_ok = sum(1 for c in candidates if c.get("_sector_ok") and c.get("_seniority_ok"))
    emit(f"[gate:screen] {len(candidates)} in -> {in_sector} in-sector, "
         f"{both_ok} also seniority-ok ({n_cached} from cache)")
    return candidates


def _rank_prompt(profile: dict, listing_block: str) -> str:
    return f"""You are estimating how well each job listing fits ONE candidate, as a rough numeric score.

Candidate target roles: {', '.join(profile.get('search_terms') or []) or 'n/a'}
Candidate target sectors/domains: {', '.join(profile.get('sectors') or []) or 'n/a'}
Candidate seniority: {profile.get('seniority', 'mid-level')}
Candidate core skills: {', '.join(profile.get('key_skills') or []) or 'n/a'}

For EACH listing, give a fit_score from 0 (clearly wrong fit) to 100 (excellent fit) for how well the
role, seniority, and sector align with the candidate. Judge relatively across the whole batch -- spread
scores out rather than clustering everything near one number.

Output ONLY JSON: {{"scores":[{{"n":1,"fit_score":72}},{{"n":2,"fit_score":40}}]}}
Include one object per listing, numbered exactly as shown.

Listings:
{listing_block}"""


def rank_gate(candidates: list[dict], profile: dict) -> list[dict]:
    """Cheap-model numeric fit ranking over the post-gate survivor pool, so the
    expensive full-text judge only ever sees a curated top slice instead of every
    gate survivor. Annotates each candidate with `_rank_score` (0-100, higher is
    better) in place and returns the full list unfiltered -- the caller applies
    its own cutoff (e.g. drop the bottom fraction, cap at N). Cached per (profile
    signature, job id) in gate_cache under gate="rank"; reuses the `reason` text
    column to hold the score (no schema change needed) since `keep` has no
    binary meaning here."""
    if not candidates:
        return []
    sig = _profile_signature(profile)
    keys = [_gate_cache_key("rank", sig, _gate_job_id(c)) for c in candidates]
    cached = _gate_cache_lookup(keys)

    to_judge: list[tuple[dict, str]] = []
    for c, key in zip(candidates, keys):
        if key in cached:
            _keep, reason = cached[key]
            try:
                c["_rank_score"] = float(reason)
            except (TypeError, ValueError):
                c["_rank_score"] = 50.0
        else:
            to_judge.append((c, key))

    n_cached = len(candidates) - len(to_judge)
    new_entries: list[tuple[str, bool, str]] = []
    for start in range(0, len(to_judge), _GATE_BATCH):
        batch = to_judge[start:start + _GATE_BATCH]
        listing_block = "\n".join(
            f"{i+1}. {c['title']} @ {c.get('company','')} | "
            f"{(c.get('location') or 'location unknown')} | {(c.get('snippet') or '')[:450]}"
            for i, (c, _k) in enumerate(batch)
        )
        prompt = _rank_prompt(profile, listing_block)
        scores: dict[int, float] = {}
        try:
            raw = llm(prompt, require_json=True, temperature=0,
                      system="You estimate rough candidate-job fit scores. Spread scores out; "
                             "don't cluster everything near one value.")
            for d in json.loads(clean_json(raw)).get("scores", []):
                n = d.get("n")
                if isinstance(n, int):
                    try:
                        scores[n] = max(0.0, min(100.0, float(d.get("fit_score", 50))))
                    except (TypeError, ValueError):
                        scores[n] = 50.0
        except Exception as e:
            emit(f"[gate:rank] batch parse failed ({e}); scoring batch as neutral (fail-open).")
            scores = {i + 1: 50.0 for i in range(len(batch))}

        for i, (c, key) in enumerate(batch):
            score = scores.get(i + 1, 50.0)
            c["_rank_score"] = score
            new_entries.append((key, True, str(score)))

    _gate_cache_store(new_entries)
    emit(f"[gate:rank] scored {len(candidates)} candidates ({n_cached} from cache)")
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


async def scrape_full_details(
    jobs: list[dict], crawler: AsyncWebCrawler, blocked_domains: frozenset[str] | set[str] = frozenset(),
    total_budget_seconds: float = 60.0,
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
    its snippet rather than letting a handful of slow pages stretch the run."""
    emit(f"\n[phase 5] Fetching full pages for {len(jobs)} jobs...")

    sem = asyncio.Semaphore(MAX_CONCURRENT)
    blocked_hit = 0

    async def fetch_one(job: dict) -> dict:
        nonlocal blocked_hit
        url = job.get("url", "")
        host = _scrape_host(url)
        if host and any(host == d or host.endswith("." + d) for d in blocked_domains):
            blocked_hit += 1
            job["full_text"] = job.get("snippet", "")
            return job

        async with sem:
            max_retries = 2
            for attempt in range(1, max_retries + 1):
                try:
                    await asyncio.sleep(random.uniform(1.5, 3.5))

                    # Force browser instance to pause until asynchronous JS redirect tracking resolves
                    run_config = CrawlerRunConfig(
                        cache_mode=CacheMode.BYPASS,
                        wait_until="networkidle",
                        page_timeout=28000,
                    )

                    result = await crawler.arun(url=url, config=run_config)
                    markdown = (result.markdown or "").strip()

                    if (result.success and markdown and len(markdown) > 150
                            and not _looks_like_redirect_stub(markdown)):
                        job["full_text"] = markdown[:8000]
                        if attempt > 1:
                            emit(f"   [RETRY SUCCESS] Bypassed script wall for {job['company']} on attempt #{attempt}")
                        break
                    elif markdown and _looks_like_redirect_stub(markdown):
                        raise ValueError("Scraper landed on a click-tracking redirect stub, not the real posting.")
                    else:
                        raise ValueError("Scraper returned an empty page shell or incomplete markup structure.")

                except Exception:
                    if attempt == max_retries:
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
    return jobs


# ── Phase 6: Final Evaluation ────────────────────────────────────────────────────

_FINAL_EVAL_DISQUALIFIERS = """1. SENIORITY/EXPERIENCE: Check whether the job states an explicit experience/seniority requirement
   (years of experience, "senior"/"lead"/"principal" in the title, or prior experience in a specific
   sector/domain). If the job clearly requires meaningfully more experience or specific sector
   experience the candidate does not have, treat that as disqualifying, not a minor caveat - exclude
   the role unless the candidate's transferable experience genuinely closes the gap. If the requirement
   is soft, negotiable, or not stated, judge fit on skills/interests as normal - don't invent a
   seniority objection that isn't in the text.
   If the job specifically requires COMMERCIAL, PROFESSIONAL, or PAID employment experience (e.g. "1-2
   years commercial software development experience"), personal projects, academic coursework,
   hackathons, and other unpaid/self-initiated work do NOT satisfy it, even if they demonstrate real
   skill - treat that as a genuine gap unless the candidate has actual paid/commercial evidence closing
   it. The candidate's background profile marks unpaid/self-initiated experience explicitly (an
   "(Informal)" past role, or an experience bullet tagged "(Personal project)") - use those tags to
   tell commercial from non-commercial evidence rather than assuming.

2. LOCATION/VISA/RELOCATION: If the role's location (or an explicit on-site/relocation/visa/work-
   authorization requirement in the text) clearly puts it outside where the candidate can realistically
   work, exclude it. If location is remote, unstated, or plainly compatible with the candidate's
   location above, do not raise a location objection.
   If the listing's own location/eligibility signals are internally contradictory (e.g. a "compatible
   timezone" framing alongside an explicit country-selector or eligibility list that excludes the
   candidate's country), do not silently resolve the contradiction either way - keep the role but add a
   concern naming the specific contradiction so the candidate can verify eligibility before applying.

3. LISTING TYPE: The listing must be a real, direct job vacancy the candidate could be hired into. If
   it is actually a paid training course, "traineeship"/placement programme, bootcamp, or any scheme
   where the candidate enrols in (or pays/finances) training and is only promised a job or interview
   afterwards rather than being hired directly, exclude it entirely - it is not a job."""

_FINAL_EVAL_WORDING = """WORDING: When you reference the candidate's OWN background in "summary", "match_reasons" or "concerns",
never state a leadership or founder title (e.g. president, chair, founder, co-founder, cofounder, CEO,
director, co-lead) on its own. If such a title came from a student club, society, campaign group,
fellowship, or other informal/unpaid activity, name the SPECIFIC organisation or activity it belongs to,
exactly as given in the candidate's profile (e.g. "co-lead of the Oxford AI Safety Society", "president of
the Debating Society", "co-founded the university's sustainability campaign") - do not flatten it into a
vague generic paraphrase that drops which specific thing it was (e.g. "ran a student society", "led an
initiative"), and never state the bare title with no object at all. If the profile text gives no specific
name to attach, leave the title out entirely rather than stating it bare - never phrase any of this so it
could read as company-founding or executive experience."""

_FINAL_EVAL_STRONG_RULES = """4. EVIDENCE STRENGTH: The candidate's background profile may show a qualifier in parentheses next to a
   skill, past role, or experience bullet. For skills this is a depth signal, e.g. "Python (Expert)" or
   "Excel (One-time)" - Expert/Proficient stated experience is strong evidence; Familiar/One-time
   exposure is weak evidence - weigh each accordingly. For past roles, the only qualifier used is
   "(Informal)", which flags a student-club, society, or volunteer position rather than paid employment,
   e.g. "President (Informal)". For experience bullets, the only qualifier used is "(Personal project)",
   which flags personal/academic/hackathon work rather than paid/commercial work.
   Treat an Informal-tagged past role, or a Personal-project-tagged experience bullet, as materially
   weaker evidence of professional/commercial competency than an untagged (real employment) past role or
   experience of similar or even longer standing - a multi-year unpaid club position or a personal coding
   project does not substitute for paid work experience. When the candidate's only support for a
   specific hard requirement (a named tool, a specific process like invoice/expense handling or
   diary/calendar management, a certification) is a generic or unrelated soft-skill/reliability anecdote
   (e.g. safety-critical responsibility, leadership of an unrelated activity, or an Informal-tagged role),
   that is NOT evidence the requirement is met unless the connection to the requirement is direct and
   explicitly stated - do not present it as satisfying the requirement in "match_reasons". Put any such
   gap in "concerns" instead.
   Also weigh CUMULATIVE nice-to-have gaps: several compounding smaller gaps (e.g. no fintech background
   AND no dbt AND no BI tooling) can together make a role a weak fit even when no single gap is
   disqualifying. Report each such gap separately in "concerns"."""

_FINAL_EVAL_SCHEMA = """Output ONLY a valid JSON object (no markdown), with two lists using this item shape:
{"strong": [
  {
    "job_number": 1, "title": "...", "company": "...", "url": "...",
    "summary": "1 concise sentence on why this role fits the candidate.",
    "match_reasons": ["concrete alignment factor 1", "concrete alignment factor 2 (max 2)"],
    "concerns": ["each notable skill gap or prerequisite the candidate lacks, one per item; [] if none"]
  }
],
 "backup": [ {same item shape} ]}"""

# Static system prefix -- identical across every cluster/call, so it's a stable
# (prompt-cache-friendly) prefix instead of being rebuilt into each user prompt. It
# carries all the rules; the per-call user prompt is just the CV + the jobs payload.
_FINAL_EVAL_SYSTEM = f"""You are an elite talent placement advisor matching a candidate to open job vacancies.
You are given a candidate profile and a set of job postings, and must return TWO lists: "strong" and "backup".

DISQUALIFIERS -- apply to EVERY role, for BOTH lists, first:
{_FINAL_EVAL_DISQUALIFIERS}

"strong" -- genuinely strong fits ONLY. Beyond the disqualifiers above, also apply:
{_FINAL_EVAL_STRONG_RULES}
Include a role in "strong" only if it is a genuinely strong fit; never pad it with weak matches.

"backup" -- least-bad survivors, for when nothing (or too little) is strong. Include a role here only if
it passes the DISQUALIFIERS above. In this list, evidence weakness and cumulative nice-to-have gaps are
EXPECTED and ACCEPTABLE -- do NOT use them to exclude a role, only note them honestly in "concerns".
Leave "backup" empty when "strong" already gives good coverage, or when every role is disqualified.

{_FINAL_EVAL_WORDING}

{_FINAL_EVAL_SCHEMA}"""


def _final_eval_job_block(i: int, j: dict) -> str:
    """One job's payload block. A non-trivial screen note (the merged gate's seniority
    verdict, already computed upstream) is surfaced as a hint so the model focuses its
    seniority re-check rather than re-deriving it from scratch."""
    reason = j.get("_gate_reason")
    hint = f"[screen note: {reason}]\n" if reason and reason not in ("ok", "gate_error", "missing_decision") else ""
    return (f"JOB {i+1}: {j['title']} at {j['company']}\n"
            f"Location: {j.get('location') or 'not stated'}\nURL: {j['url']}\n{hint}\n"
            f"{j.get('full_text','')[:2000]}")


def _run_final_eval(jobs: list[dict], cv_text: str | None) -> tuple[list[dict], list[dict]]:
    """One expensive-model call returning (strong, backup) lists of merged job dicts.
    Replaces the old strict-then-relaxed two-call pattern: the single prompt asks for
    both a strict "strong" list and a lenient disqualifier-only "backup" list, so a
    round with no strong fits no longer costs a second full-payload call."""
    if not jobs:
        return [], []
    emit(f"[phase 6] Final matching processing matrix active ({EXP_MODEL})...")
    if cv_text is None:
        cv_text = open(CV_PATH, encoding="utf-8").read()
    cv_text = cv_text[:5000]

    jobs_block = "\n\n---\n\n".join(_final_eval_job_block(i, j) for i, j in enumerate(jobs))
    prompt = f"""Candidate Background Profile:
{cv_text}

Judge the {len(jobs)} complete job postings below. Return up to {FINAL_PICKS} genuinely strong fits in
"strong" (best first), and up to 3 least-bad disqualifier-only survivors in "backup" (best first; empty
if "strong" already covers it or nothing qualifies).

Jobs Payload:
{jobs_block}"""

    try:
        raw = llm(prompt, system=_FINAL_EVAL_SYSTEM, model=EXP_MODEL, require_json=True)
        data = json.loads(clean_json(raw))
    except Exception as e:
        emit(f"[phase 6] Final generation evaluation failed: {e}")
        # None,None (not [],[]) -- a failed call must be distinguishable from a
        # real judgment that rejected everyone. The caller (engine.py) treats
        # the two very differently: a genuine rejection must never resurface,
        # while a failed call falls back to an unverified top-N so a transient
        # API error doesn't get silently recorded as a permanent rejection.
        return None, None

    def _merge(entries, cap):
        out = []
        for entry in (entries or [])[:cap]:
            idx = entry.get("job_number", 1) - 1
            if 0 <= idx < len(jobs):
                merged = jobs[idx].copy()
                merged.update(entry)
                out.append(merged)
        return out

    return _merge(data.get("strong"), FINAL_PICKS), _merge(data.get("backup"), min(3, len(jobs)))


def final_evaluation_split(jobs: list[dict], profile: dict, cv_text: str | None = None
                           ) -> tuple[list[dict] | None, list[dict] | None]:
    """Backend entry point: (strong, backup) from ONE expensive call. The backend uses
    `strong` when non-empty, else `backup` (tagged as a non-strong fallback). Returns
    (None, None) if the call itself failed -- see _run_final_eval's except branch --
    so the caller can tell that apart from a real judgment that rejected everyone."""
    return _run_final_eval(jobs, cv_text)


def final_evaluation(jobs: list[dict], profile: dict, cv_text: str | None = None) -> list[dict]:
    """Judge `jobs`; return the best flat list -- strong fits, or the least-bad backups
    when nothing is strong. Standalone/CLI callers use this; `cv_text`, when given,
    overrides reading CV_PATH (the backend passes a role-cluster-scoped bio)."""
    strong, backup = _run_final_eval(jobs, cv_text)
    return (strong or backup) or []


# ── Phase 7: Save & Output ───────────────────────────────────────────────────────


def save_and_display(results: list[dict]):
    sep = "─" * 60
    emit(f"\n{'═'*60}\n  TOP PIPELINE MATCHES SUMMARY\n{'═'*60}")

    conn = get_db()
    for i, job in enumerate(results, 1):
        emit(f"\n#{i}  {job['title']}\n    {job['company']}\n    {job['url']}\n\n    {job.get('summary', '')}")
        for r in job.get("match_reasons", []):
            emit(f"    ✓  {r}")
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
            for r in job.get("match_reasons", []):
                f.write(f"- ✓ {r}\n")
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