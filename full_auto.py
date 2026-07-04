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
import json
import os
import random
import re
import sqlite3
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
# ── Add these near the top, after imports ──────────────────────────────────────
import queue
from crawl4ai import CacheMode

_log_queue: queue.Queue | None = None

def set_log_queue(q: queue.Queue):
    global _log_queue
    _log_queue = q

def emit(msg: str):
    """Prints to terminal and pushes to web queue if one is set."""
    print(msg)
    if _log_queue is not None:
        _log_queue.put(msg)

# ── API KEYS ───────────────────────────────────────────────────────────────────
REED_API_KEY = os.getenv("REED_API_KEY", "")
ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")
SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")

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
FINAL_PICKS         = 10      # Max results returned, quality-gated

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
           "leeds", "glasgow", "edinburgh", "bristol", "liverpool"},
    "us": {"united states", "usa", "u.s.", "u.s.a.", "america", "new york",
           "san francisco", "los angeles", "chicago", "seattle", "austin",
           "boston", "texas", "california"},
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


# ── Flexible API Fetchers ──────────────────────────────────────────────────────

def fetch_reed(query: str, location: str = "United Kingdom", country_code: str = "gb", pages: int = 3) -> List[Dict]:
    """Reed is UK-only. Skips execution if the profile's resolved country isn't GB
    (location is just the locationName Reed's API scopes the search to -- it's
    never going to equal "United Kingdom" for a real city/postcode profile, so
    gating on country_code instead of a location-string match)."""
    if (country_code or "gb").strip().lower() != "gb" or not REED_API_KEY:
        return []

    url = "https://www.reed.co.uk/api/1.0/search"
    page_size = 100  # Reed's max resultsToTake
    jobs: List[Dict] = []
    for page in range(pages):
        params = {"keywords": query, "locationName": location,
                  "resultsToTake": page_size, "resultsToSkip": page * page_size}
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


def fetch_google_jobs(query: str, location: str = "United Kingdom", pages: int = 3) -> List[Dict]:
    """Fetches localized search results via Google Jobs API, following
    next_page_token for up to `pages` pages. Broad-tier: only called on
    non-first runs for the rotated term, to keep SerpAPI credit use bounded."""
    if not SERPAPI_KEY:
        return []

    clean_loc = normalize_location(location)
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


def fetch_jsearch(query: str, location: str = "United Kingdom") -> List[Dict]:
    """Fetches high-density results using the JSearch endpoint structure."""
    if not RAPIDAPI_KEY:
        return []
        
    clean_loc = normalize_location(location)
    headers = {"X-RapidAPI-Key": RAPIDAPI_KEY, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    params = {"query": f"{query} in {clean_loc}", "page": 1, "num_pages": 1}
    try:
        r = requests.get("https://jsearch.p.rapidapi.com/search", headers=headers, params=params, timeout=12)
        jobs = []
        for job in r.json().get("data", []):
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


class ReedSource:
    name, tier = "reed", "fast"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        country_code = profile.get("adzuna_country_code", "gb")
        for term in _terms(profile):
            out.extend(fetch_reed(term, profile.get("location", "United Kingdom"), country_code))
        return out


class AdzunaSource:
    name, tier = "adzuna", "fast"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(fetch_adzuna(term, profile.get("location", "United Kingdom"),
                                    profile.get("adzuna_country_code", "gb")))
        return out


class GoogleJobsSource:
    name, tier = "google_jobs", "broad"   # paginated + SerpAPI credits
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(fetch_google_jobs(term, profile.get("location", "United Kingdom")))
        return out


class JSearchSource:
    name, tier = "jsearch", "broad"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(fetch_jsearch(term, profile.get("location", "United Kingdom")))
        return out


class RemotiveSource:
    name, tier = "remotive", "broad"
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(fetch_remotive(term))
        return out


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
                    "snippet": (desc or "")[:3000],
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

    rows = data.get("jobs") if vendor == "greenhouse" else \
           data.get("postings") if vendor == "lever" else \
           data.get("offers") if vendor == "recruitee" else \
           data.get("jobs") if vendor == "workable" else \
           data.get("jobs", data)  # ashby
    out = []
    for j in rows or []:
        if vendor == "greenhouse":
            out.append({"board": f"gh:{token}", "title": j.get("title", ""),
                        "company": token, "url": j.get("absolute_url", ""),
                        "location": (j.get("location") or {}).get("name", ""),
                        "snippet": (j.get("content", "") or "")[:3000],
                        "updated_at": j.get("updated_at")})
        elif vendor == "lever":
            out.append({"board": f"lever:{token}", "title": j.get("text", ""),
                        "company": token, "url": j.get("hostedUrl", ""),
                        "location": (j.get("categories") or {}).get("location", ""),
                        "snippet": (j.get("descriptionPlain", "") or "")[:3000],
                        "updated_at": j.get("createdAt")})
        elif vendor == "workable":
            loc = j.get("location") or {}
            out.append({"board": f"workable:{token}", "title": j.get("title", ""),
                        "company": token,
                        "url": j.get("url") or j.get("application_url", ""),
                        "location": loc.get("location_str") or ", ".join(
                            x for x in [loc.get("city"), loc.get("country")] if x),
                        "snippet": (j.get("description", "") or "")[:3000],
                        "updated_at": j.get("published_on") or j.get("created_at")})
        elif vendor == "recruitee":
            out.append({"board": f"recruitee:{token}", "title": j.get("title", ""),
                        "company": token,
                        "url": j.get("careers_url") or j.get("careers_apply_url", ""),
                        "location": j.get("location") or ", ".join(
                            x for x in [j.get("city"), j.get("country")] if x),
                        "snippet": (j.get("description", "") or "")[:3000],
                        "updated_at": j.get("published_at")})
        else:  # ashby
            out.append({"board": f"ashby:{token}", "title": j.get("title", ""),
                        "company": token, "url": j.get("jobUrl", ""),
                        "location": j.get("location", ""),
                        "snippet": (j.get("descriptionPlain", "") or "")[:3000],
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


def select_sources_for_run(profile: Dict) -> List[JobSource]:
    terms = profile.get("search_terms") or []
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
    """Maintenance job (not run on every search): uses SerpAPI to find company
    ATS board tokens via site: search, then upserts them into company_ats.
    Run this occasionally, e.g. once when onboarding a new sector."""
    if not SERPAPI_KEY:
        emit("   [!] SERPAPI_KEY not set; cannot harvest ATS tokens.")
        return []

    site_patterns = {
        "greenhouse": "site:boards.greenhouse.io",
        "lever":      "site:jobs.lever.co",
        "ashby":      "site:jobs.ashbyhq.com",
        "workable":   "site:apply.workable.com",
        "recruitee":  "site:recruitee.com",
        "personio":   "site:jobs.personio.com",
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
    for vendor, site in site_patterns.items():
        token_re = token_res[vendor]
        for keyword in sector_keywords:
            params = {"engine": "google", "q": f"{site} {keyword}", "api_key": SERPAPI_KEY}
            try:
                r = requests.get("https://serpapi.com/search", params=params, timeout=12)
                data = r.json()
            except Exception as e:
                emit(f"   [!] ATS harvest error ({vendor}/{keyword}): {e}")
                continue
            for result in data.get("organic_results", []):
                link = result.get("link", "")
                m = token_re.search(link)
                if m:
                    token = m.group(1)
                    # Tag the row with the phrase that found it so the batch
                    # selector can favour it for profiles in this sector.
                    found.append((token, vendor, token, keyword))

    deduped = list({(c, v, t, kw) for c, v, t, kw in found})
    save_company_ats(deduped)
    emit(f"[ats] Harvested {len(deduped)} ATS tokens across {len(site_patterns)} vendors.")
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
            f"{(c.get('location') or 'location unknown')} | {(c.get('snippet') or '')[:150]}"
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

async def scrape_full_details(jobs: list[dict], crawler: AsyncWebCrawler) -> list[dict]:
    """Robust scraper isolating heavy tracking/redirect links with single concurrency queue and retries."""
    emit(f"\n🌐 [phase 5] Fetching full pages for {len(jobs)} jobs...")
    
    general_sem = asyncio.Semaphore(MAX_CONCURRENT)
    adzuna_sem = asyncio.Semaphore(1)  # Force single lane execution on anti-bot patterns

    async def fetch_one(job: dict) -> dict:
        url = job.get("url", "")
        is_adzuna = "adzuna." in url.lower()
        sem = adzuna_sem if is_adzuna else general_sem
        
        async with sem:
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    if is_adzuna:
                        base_delay = random.uniform(4.0, 7.0)
                        await asyncio.sleep(base_delay * attempt)
                    else:
                        await asyncio.sleep(random.uniform(1.5, 3.5))
                    
                    # Force browser instance to pause until asynchronous JS redirect tracking resolves
                    run_config = CrawlerRunConfig(
    cache_mode=CacheMode.BYPASS,  # <-- Change string to Enum here
    wait_until="networkidle",
    page_timeout=45000
)
                    
                    result = await crawler.arun(url=url, config=run_config)
                    
                    if result.success and result.markdown and len(result.markdown.strip()) > 150:
                        job["full_text"] = result.markdown[:8000]
                        if attempt > 1:
                            emit(f"   ✓ [RETRY SUCCESS] Bypassed script wall for {job['company']} on attempt #{attempt}")
                        break
                    else:
                        raise ValueError("Scraper returned an empty page shell or incomplete markup structure.")
                        
                except Exception as e:
                    if attempt == max_retries:
                        emit(f"   [!] [PHASE 5 FAILURE] Blocked at {job['company']}. Preserving snippet summary.")
                        job["full_text"] = job.get("snippet", "")
                    else:
                        emit(f"   ⚠️ [BLOCKED/SHELL] Cool-down applied for {job['company']} (Attempt #{attempt}). Retrying...")
                        
        return job

    return list(await asyncio.gather(*[fetch_one(j) for j in jobs]))


# ── Phase 6: Final Evaluation ────────────────────────────────────────────────────

def final_evaluation(jobs: list[dict], profile: dict) -> list[dict]:
    emit(f"[phase 6] Final matching processing matrix active ({EXP_MODEL})...")
    cv_text = open(CV_PATH, encoding="utf-8").read()[:3000]

    jobs_block = "\n\n---\n\n".join(
        f"JOB {i+1}: {j['title']} at {j['company']}\n"
        f"Location: {j.get('location') or 'not stated'}\nURL: {j['url']}\n\n{j.get('full_text','')[:2000]}"
        for i, j in enumerate(jobs)
    )

    prompt = f"""You are a professional career advisor matching a candidate with long-term targeted open vacancies.

Candidate Background Profile:
{cv_text}

Analyze the {len(jobs)} complete extracted documents below. Apply these disqualification rules FIRST,
before judging general fit:

1. SENIORITY/EXPERIENCE: Check whether the job states an explicit experience/seniority requirement
   (years of experience, "senior"/"lead"/"principal" in the title, or prior experience in a specific
   sector/domain). If the job clearly requires meaningfully more experience or specific sector
   experience the candidate does not have, treat that as disqualifying, not a minor caveat - exclude
   the role unless the candidate's transferable experience genuinely closes the gap. If the requirement
   is soft, negotiable, or not stated, judge fit on skills/interests as normal - don't invent a
   seniority objection that isn't in the text.

2. LOCATION/VISA/RELOCATION: If the role's location (or an explicit on-site/relocation/visa/work-
   authorization requirement in the text) clearly puts it outside where the candidate can realistically
   work, exclude it. If location is remote, unstated, or plainly compatible with the candidate's
   location above, do not raise a location objection.

3. EVIDENCE STRENGTH: The candidate's background profile may show a proficiency/duration qualifier in
   parentheses next to a skill or past role, e.g. "Python (expert, 5+ years)" or "Camp Counsellor
   (one-off, one week)". Multi-year or expert-level stated experience is strong evidence; brief,
   one-time, or duration-unstated exposure is weak evidence - weigh each accordingly. When the
   candidate's only support for a specific hard requirement (a named tool, a specific process like
   invoice/expense handling or diary/calendar management, a certification) is a generic or unrelated
   soft-skill/reliability anecdote (e.g. safety-critical responsibility, leadership of an unrelated
   activity), that is NOT evidence the requirement is met unless the connection to the requirement is
   direct and explicitly stated - do not present it as satisfying the requirement in "match_reasons".
   Put any such gap in "concerns" instead.

Then, for the roles that survive, weigh the CUMULATIVE nice-to-have gaps: several compounding smaller
gaps (e.g. no fintech background AND no dbt AND no BI tooling) can together make a role a weak fit even
when no single gap is disqualifying. Report each such gap separately in "concerns".

Return every role that is a genuinely strong fit for this candidate, up to {FINAL_PICKS}, ordered best
first. Return fewer than {FINAL_PICKS} if fewer genuinely qualify - do not pad the list with weak matches.
Output ONLY valid structural JSON object (no markdown formatting code):
{{"selections": [
  {{
    "job_number": 1,
    "title": "...",
    "company": "...",
    "url": "...",
    "summary": "1 concise sentence on why this role fits the candidate.",
    "match_reasons": ["concrete alignment factor 1", "concrete alignment factor 2 (max 2)"],
    "concerns": ["each notable skill gap or prerequisite the candidate lacks, one per item; [] if none"]
  }}
]}}

Jobs Payload:
{jobs_block}"""

    try:
        raw = llm(prompt, system="You are an elite talent placement advisor.", model=EXP_MODEL, require_json=True)
        results = json.loads(clean_json(raw)).get("selections", [])
    except Exception as e:
        emit(f"[phase 6] Final generation evaluation failed: {e}")
        return []

    final = []
    for entry in results[:FINAL_PICKS]:
        idx = entry.get("job_number", 1) - 1
        if 0 <= idx < len(jobs):
            merged = jobs[idx].copy()
            merged.update(entry)
            final.append(merged)

    return final


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