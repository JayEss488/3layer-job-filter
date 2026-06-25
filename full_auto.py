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
import sqlite3
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
TOP_CANDIDATES      = 10      # Passed to Phase 5 full scrape
FINAL_PICKS         = 3       # Returned to final results

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


# ── Flexible API Fetchers ──────────────────────────────────────────────────────

def fetch_reed(query: str, location: str = "United Kingdom") -> List[Dict]:
    """Reed is primarily UK-focused. Skips execution if profile is elsewhere."""
    if normalize_location(location) != "United Kingdom" or not REED_API_KEY:
        return []
    
    url = "https://www.reed.co.uk/api/1.0/search"
    params = {"keywords": query, "locationName": location, "resultsToTake": 50}
    try:
        r = requests.get(url, params=params, auth=HTTPBasicAuth(REED_API_KEY, ""), timeout=12)
        jobs = []
        for job in r.json().get("results", []):
            jobs.append({
                "board": "reed",
                "title": job.get("jobTitle", ""),
                "company": job.get("employerName", ""),
                "url": job.get("jobUrl", ""),
                "snippet": job.get("jobDescription", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] Reed API Error: {e}")
        return []


def fetch_adzuna(query: str, location: str = "United Kingdom", country_code: str = "gb") -> List[Dict]:
    """Routes dynamically to the matching Adzuna global regional server."""
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        return []
    
    # Ensure clean lowercase ISO code string (default to 'gb')
    cc = country_code.strip().lower() if country_code else "gb"
    url = f"https://api.adzuna.com/v1/api/jobs/{cc}/search/1"
    
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "what": query,
        "where": location,
        "results_per_page": 50
    }
    try:
        r = requests.get(url, params=params, timeout=12)
        jobs = []
        for job in r.json().get("results", []):
            jobs.append({
                "board": "adzuna",
                "title": job.get("title", ""),
                "company": job.get("company", {}).get("display_name", ""),
                "url": job.get("redirect_url", ""),
                "snippet": job.get("description", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] Adzuna ({cc}) API Error: {e}")
        return []


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
                "snippet": job.get("description", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] Remotive API Error: {e}")
        return []


def fetch_jobicy(query: str) -> List[Dict]:
    """Queries Jobicy remote listings targeting specific keywords."""
    url = f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={query}"
    try:
        r = requests.get(url, timeout=12)
        jobs = []
        for job in r.json().get("jobs", []):
            jobs.append({
                "board": "jobicy",
                "title": job.get("jobTitle", ""),
                "company": job.get("companyName", ""),
                "url": job.get("url", ""),
                "snippet": job.get("jobDescription", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] Jobicy API Error: {e}")
        return []


def fetch_google_jobs(query: str, location: str = "United Kingdom") -> List[Dict]:
    """Fetches localized search results via Google Jobs API."""
    if not SERPAPI_KEY:
        return []
    
    clean_loc = normalize_location(location)
    params = {"engine": "google_jobs", "q": query, "location": clean_loc, "api_key": SERPAPI_KEY}
    try:
        r = requests.get("https://serpapi.com/search", params=params, timeout=12)
        data = r.json()
        if "error" in data:
            emit(f"   [!] SerpAPI Error: {data['error']}")
            return []
            
        jobs = []
        for job in data.get("jobs_results", []):
            apply_options = job.get("apply_options") or []
            url = apply_options[0].get("link") if apply_options else ""
            jobs.append({
                "board": "google_jobs",
                "title": job.get("title", ""),
                "company": job.get("company_name", ""),
                "url": url,
                "snippet": job.get("description", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] Google Jobs API Error: {e}")
        return []


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
            jobs.append({
                "board": "jsearch",
                "title": job.get("job_title", ""),
                "company": job.get("employer_name", ""),
                "url": job.get("job_apply_link", ""),
                "snippet": job.get("job_description", "")
            })
        return jobs
    except Exception as e:
        emit(f"   [!] JSearch API Error: {e}")
        return []


def gather_jobs(profile: Dict) -> List[Dict]:
    """Orchestrates job harvesting across boards using the profile constraints."""
    location = profile.get("location", "United Kingdom")
    adzuna_cc = profile.get("adzuna_country_code", "gb")
    all_jobs = []

    for term in profile.get("search_terms", []):
        emit(f"[api] Fetching listings for: {term}")
        
        all_jobs.extend(fetch_reed(term, location))
        all_jobs.extend(fetch_adzuna(term, location, adzuna_cc))
        all_jobs.extend(fetch_google_jobs(term, location))
        all_jobs.extend(fetch_jsearch(term, location))
        all_jobs.extend(fetch_remotive(term))
        all_jobs.extend(fetch_jobicy(term))
        
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
    conn.commit()
    conn.close()
    emit("[db] Tables ready and schema initialized.")


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


def llm(prompt: str, system: str = "", model: str = CHEAP_MODEL, require_json: bool = False) -> str:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    
    args = {"model": model, "messages": msgs, "temperature": 0.2}
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


# ── Phase 4: Candidate Ranking ──────────────────────────────────────────────────

def rank_candidates(candidates: list[dict], profile: dict) -> list[dict]:
    if not candidates:
        emit("[phase 4] No candidates found passing basic structural vector checks.")
        return []

    pool = sorted(candidates, key=lambda x: x.get("embed_score", 0), reverse=True)[:60]
    emit(f"[phase 4] Ranking top {len(pool)} raw candidates via semantic matrix...")

    listing_block = "\n".join(
        f"{i+1}. {c['title']} @ {c['company']} | {c['snippet'][:120]}"
        for i, c in enumerate(pool)
    )

    prompt = f"""You are an executive talent recruiter. Assess these open roles for a candidate profile matching:
Core Qualifications: {', '.join(profile['key_skills'])}
Target Industries: {', '.join(profile['sectors'])}
Target Experience Bracket: {profile['seniority']}

Select the {TOP_CANDIDATES} absolute best matches. Output ONLY a structured JSON object containing a 'rankings' array:
{{"rankings": [{{"listing_number": 1, "score": 9, "reason": "Match summary..."}}]}}

Listings Base:
{listing_block}"""

    try:
        raw = llm(prompt, require_json=True)
        ranked_list = json.loads(clean_json(raw)).get("rankings", [])
    except Exception as e:
        emit(f"[phase 4] Ranking structural processing failed ({e}). Reverting to default matrix order.")
        return pool[:TOP_CANDIDATES]

    top = []
    for entry in ranked_list[:TOP_CANDIDATES]:
        idx = entry.get("listing_number", 1) - 1
        if 0 <= idx < len(pool):
            job = pool[idx].copy()
            job["llm_score"]  = entry.get("score", 0)
            job["llm_reason"] = entry.get("reason", "")
            top.append(job)

    emit(f"[phase 4] Top {len(top)} high-yield listings selected for extraction hydration.")
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
        f"JOB {i+1}: {j['title']} at {j['company']}\nURL: {j['url']}\n\n{j.get('full_text','')[:2000]}"
        for i, j in enumerate(jobs)
    )

    prompt = f"""You are a professional career advisor matching a candidate with long-term targeted open vacancies.

Candidate Background Profile:
{cv_text}

Analyze the {len(jobs)} complete extracted documents below. Choose the {FINAL_PICKS} strongest overall alignments.
Output ONLY valid structural JSON object (no markdown formatting code):
{{"selections": [
  {{
    "job_number": 1,
    "title": "...",
    "company": "...",
    "url": "...",
    "summary": "2-3 clear descriptive evaluation sentences mapping role profile directly to candidate vector.",
    "match_reasons": ["concrete alignment factor 1", "concrete alignment factor 2"],
    "concerns": ["notable technical skill caps or structural prerequisites"]
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