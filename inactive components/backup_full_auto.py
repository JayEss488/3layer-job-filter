#backup data
#!/usr/bin/env python3
"""
Job Search Automation
─────────────────────
Phase 1  Profile extraction from exp.txt          (cached in DB)
Phase 2  Board URL pattern detection              (cached 30 days)
Phase 3  Parallel scraping + embedding filter
Phase 4  Cheap-model candidate ranking → top 10
Phase 5  Full page scrape of top 10
Phase 6  Expensive-model final evaluation → top 3
Phase 7  Output to terminal + results.md + DB
"""

from crawl4ai import async_crawler_strategy
import asyncio
import hashlib
import json
import os
import random
import sqlite3
from datetime import datetime, timedelta
from typing import Optional
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, BrowserConfig

import numpy as np
from crawl4ai import AsyncWebCrawler
from openai import OpenAI

# API KEYS

REED_API_KEY = os.getenv("REED_API_KEY", "")

ADZUNA_APP_ID = os.getenv("ADZUNA_APP_ID", "")
ADZUNA_APP_KEY = os.getenv("ADZUNA_APP_KEY", "")

SERPAPI_KEY = os.getenv("SERPAPI_KEY", "")

RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")


# ── Config ─────────────────────────────────────────────────────────────────────

DB_PATH             = "/home/jamesstephens/Documents/search_auto/boards_cache.db"
BOARDS_PATH         = "/home/jamesstephens/Documents/search_auto/boards.json"
CV_PATH             = "/home/jamesstephens/Documents/search_auto/exp.txt"

CHEAP_MODEL         = "gpt-5.4-nano-2026-03-17"
EXP_MODEL           = "gpt-5.4"
EMBED_MODEL         = "text-embedding-3-small"

MAX_CONCURRENT      = 5      # max simultaneous crawl requests
RELEVANCE_THRESHOLD = 0.55   # cosine similarity cutoff for embedding pre-filter
BOARD_CACHE_DAYS    = 30     # how long to reuse detected URL patterns
TOP_CANDIDATES      = 10     # jobs passed to full scrape
FINAL_PICKS         = 3      # jobs returned to user

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

import requests
from requests.auth import HTTPBasicAuth


def fetch_reed(query, location="United Kingdom"):
    url = "https://www.reed.co.uk/api/1.0/search"

    params = {
        "keywords": query,
        "locationName": location,
        "resultsToTake": 50,
    }

    r = requests.get(
        url,
        params=params,
        auth=HTTPBasicAuth(REED_API_KEY, "")
    )

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


def fetch_adzuna(query, location="United Kingdom"):

    url = (
        f"https://api.adzuna.com/v1/api/jobs/gb/search/1"
        f"?app_id={ADZUNA_APP_ID}"
        f"&app_key={ADZUNA_APP_KEY}"
        f"&what={query}"
        f"&where={location}"
        f"&results_per_page=50"
    )

    r = requests.get(url)

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


def fetch_remotive():

    r = requests.get(
        "https://remotive.com/api/remote-jobs"
    )

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


def fetch_jobicy():

    r = requests.get(
        "https://jobicy.com/api/v2/remote-jobs"
    )

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


def fetch_google_jobs(query,
                      location="United Kingdom"):

    params = {
        "engine": "google_jobs",
        "q": query,
        "location": location,
        "api_key": SERPAPI_KEY
    }

    r = requests.get(
        "https://serpapi.com/search",
        params=params
    )

    jobs = []

    for job in r.json().get(
        "jobs_results",
        []
    ):

        jobs.append({
            "board": "google_jobs",
            "title": job.get("title", ""),
            "company": job.get("company_name", ""),
            "url": (
                job.get("apply_options", [{}])[0]
                .get("link", "")
            ),
            "snippet": job.get("description", "")
        })

    return jobs


def fetch_jsearch(query,
                  location="United Kingdom"):

    headers = {
        "X-RapidAPI-Key": RAPIDAPI_KEY
    }

    params = {
        "query": f"{query} in {location}",
        "page": 1,
        "num_pages": 1
    }

    r = requests.get(
        "https://jsearch.p.rapidapi.com/search",
        headers=headers,
        params=params
    )

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


def gather_jobs(profile):

    location = profile.get(
        "location",
        "United Kingdom"
    )

    all_jobs = []

    for term in profile["search_terms"]:

        print(f"[api] {term}")

        try:
            all_jobs.extend(
                fetch_reed(term, location)
            )
        except Exception as e:
            print("reed", e)

        try:
            all_jobs.extend(
                fetch_adzuna(term, location)
            )
        except Exception as e:
            print("adzuna", e)

        try:
            all_jobs.extend(
                fetch_google_jobs(
                    term,
                    location
                )
            )
        except Exception as e:
            print("google jobs", e)

        try:
            all_jobs.extend(
                fetch_jsearch(
                    term,
                    location
                )
            )
        except Exception as e:
            print("jsearch", e)

    try:
        all_jobs.extend(fetch_remotive())
    except Exception:
        pass

    try:
        all_jobs.extend(fetch_jobicy())
    except Exception:
        pass

    return all_jobs





# ── Database ───────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create extra tables alongside the existing jobs table (safe to re-run)."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS profile_cache (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            cached_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS board_patterns (
            domain     TEXT PRIMARY KEY,
            pattern    TEXT NOT NULL,
            cached_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS candidates (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id       TEXT UNIQUE,
            board        TEXT NOT NULL,
            title        TEXT NOT NULL,
            company      TEXT,
            url          TEXT NOT NULL,
            snippet      TEXT,
            full_text    TEXT,
            embed_score  REAL,
            scraped_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS jobs (
            job_id       TEXT PRIMARY KEY,
            title        TEXT NOT NULL,
            company      TEXT,
            url          TEXT NOT NULL,
            ai_summary   TEXT,
            saved_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()
    print("[db] Tables ready.")


# ── Utilities ──────────────────────────────────────────────────────────────────

def make_job_id(board: str, url: str) -> str:
    return hashlib.md5(f"{board}|{url}".encode()).hexdigest()[:16]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    a, b = np.array(a), np.array(b)
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / norm) if norm else 0.0


def clean_json(raw: str) -> str:
    """Strip markdown fences that models sometimes add around JSON."""
    return raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def llm(prompt: str, system: str = "", model: str = CHEAP_MODEL, require_json: bool = False) -> str:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    
    # Configure safety flags
    args = {
        "model": model,
        "messages": msgs,
        "temperature": 0.2
    }
    
    # IF we explicitly require structured JSON data, force the API schema guardrail
    if require_json:
        args["response_format"] = { "type": "json_object" }
        
    resp = client.chat.completions.create(**args)
    return resp.choices[0].message.content.strip()


def get_embeddings_batch(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts in one API call."""
    cleaned = [t[:8000] for t in texts]
    resp = client.embeddings.create(model=EMBED_MODEL, input=cleaned)
    return [e.embedding for e in resp.data]


# ── Phase 1: Profile extraction ────────────────────────────────────────────────

def get_profile() -> dict:
    """
    Read exp.txt, ask a cheap model to extract a structured profile,
    and cache the result. Subsequent runs skip the LLM call.
    """
    conn = get_db()
    row = conn.execute("SELECT value FROM profile_cache WHERE key='profile'").fetchone()
    conn.close()
    if row:
        print("[phase 1] Profile loaded from cache.")
        return json.loads(row["value"])

    print("[phase 1] Extracting profile from CV...")
    cv_text = open(CV_PATH).read()

    prompt = f"""Read this CV/experience document. Output ONLY valid JSON (no markdown fences) with:
{{
  "sectors":      ["list of relevant sectors e.g. software, data, climate, policy"],
  "seniority":    "graduate / junior / mid-level",
  "key_skills":   ["up to 10 skills"],
  "location":     "preferred location or 'UK'",
  "search_terms": ["10-14 specific job-title search queries for job boards"]
}}

CV:
{cv_text}"""

    raw = llm(prompt, require_json=True)
    profile = json.loads(clean_json(raw))

    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO profile_cache(key, value) VALUES(?,?)",
        ("profile", json.dumps(profile))
    )
    conn.commit()
    conn.close()

    print(f"[phase 1] Done. Sectors: {profile['sectors']}")
    return profile


def get_profile_embedding(profile: dict) -> list[float]:
    """Embed a compact representation of the candidate profile (cached in DB)."""
    conn = get_db()
    row = conn.execute("SELECT value FROM profile_cache WHERE key='profile_embedding'").fetchone()
    conn.close()
    if row:
        return json.loads(row["value"])

    text = " ".join([
        *profile["key_skills"],
        *profile["sectors"],
        profile["seniority"],
        " ".join(profile["search_terms"]),
    ])
    embedding = get_embeddings_batch([text])[0]

    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO profile_cache(key, value) VALUES(?,?)",
        ("profile_embedding", json.dumps(embedding))
    )
    conn.commit()
    conn.close()
    return embedding


# ── Phase 2: Board URL pattern detection ──────────────────────────────────────

async def detect_board_pattern(
    domain: str,
    crawler: AsyncWebCrawler,
    semaphore: asyncio.Semaphore,
) -> Optional[str]:
    """Fetch the board homepage using gentle pacing and advanced anti-bot configurations."""
    # 1. Check local cache first
    with get_db() as conn:
        row = conn.execute(
            "SELECT pattern, cached_at FROM board_patterns WHERE domain=?", (domain,)
        ).fetchone()

    if row:
        age = datetime.now() - datetime.fromisoformat(row["cached_at"])
        if age < timedelta(days=BOARD_CACHE_DAYS):
            return row["pattern"]

    page_text = ""
    
    # 2. Network Fetch Phase (Protected by Semaphore)
    async with semaphore:
        print(f"[phase 2] Gentle crawl for {domain}...")
        try:
            await asyncio.sleep(random.uniform(1.0, 3.0))

            config = CrawlerRunConfig(
                cache_mode="BYPASS",
                page_timeout=30000,
                simulate_user=True,        # Human-like mouse movements/scrolls
                override_navigator=True,   # Masks core automated Playwright properties
             )

            result = await crawler.arun(url=f"https://{domain}", config=config)
            page_text = result.markdown[:5000] if result.markdown else ""
            
            if not page_text:
                print(f"  [phase 2] Warning: Got empty page content for {domain}")
                return None
                
        except Exception as e:
            print(f"  [phase 2] Crawl network failed for {domain}: {e}")
            return None

    # 3. LLM Analysis Phase
    # 3. LLM Analysis Phase
    prompt = f"""Analyse this job board web page structure and determine the URL pattern for keyword searches.
Common forms:
  https://domain.com/jobs?keywords={{{{q}}}}&location={{{{l}}}}
  https://domain.com/search?q={{{{q}}}}&location={{{{l}}}}

Output ONLY valid JSON (no markdown):
{{"pattern": "full URL template using {{{{q}}}} and {{{{l}}}}"}}

If you cannot determine it output: {{"pattern": null}}

Content:
{page_text}"""

    pattern = None
    try:
        raw = llm(prompt, require_json=True)
        data = json.loads(clean_json(raw))
        pattern = data.get("pattern")
    except Exception as llm_err:
        # CRITICAL: Stop hiding the error! Let's see what is breaking.
        print(f"  [phase 2] LLM or JSON Parsing failed for {domain}: {llm_err}")
        if 'raw' in locals():
            print(f"  [phase 2] Raw LLM Output was: {raw}")

    # 4. Database Storage Phase
    if pattern:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO board_patterns(domain, pattern, cached_at) VALUES(?,?,CURRENT_TIMESTAMP)", 
                (domain, pattern)
            )
            conn.commit()
        print(f"  [phase 2] {domain} → {pattern}")
    else:
        print(f"  [phase 2] Could not resolve pattern for {domain} (LLM returned null)")
        
    return pattern


async def prepare_boards(
    boards: list[dict],
    crawler: AsyncWebCrawler,
) -> dict[str, str]:
    """Deduplicates input targets and executes board pattern identification safely."""
    unique_boards = {b["domain"]: b for b in boards}.values()
    phase2_semaphore = asyncio.Semaphore(2)
    
    tasks = [detect_board_pattern(b["domain"], crawler, phase2_semaphore) for b in unique_boards]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # ── DETECT GHOST EXCEPTIONS HERE ──
    final_patterns = {}
    for board, pat in zip(unique_boards, results):
        if isinstance(pat, Exception):
            print(f"❌ [CRITICAL LOG] Task for {board['domain']} raised an uncaught exception: {pat}")
        elif isinstance(pat, str):
            final_patterns[board["domain"]] = pat
        else:
            print(f"⚠️ [DEBUG] Task for {board['domain']} returned an unexpected type: {type(pat)} (Value: {pat})")

    return final_patterns


# ── Phase 3: Parallel scraping ─────────────────────────────────────────────────

def build_search_urls(pattern: str, search_terms: list[str], location: str) -> list[str]:
    urls = []
    
    # ── Normalize Brackets Automatically ──
    # If the pattern somehow ended up with {{q}} or {q}, we convert it cleanly
    normalized_pattern = pattern.replace("{{q}}", "{q}").replace("{{l}}", "{l}")
    
    for term in search_terms:
        # Swap clean single placeholders
        url = normalized_pattern.replace("{q}", term.replace(" ", "+"))
        url = url.replace("{l}", location.replace(" ", "+"))
        urls.append(url)
    return urls


async def scrape_results_page(
    url: str,
    board: str,
    profile_embedding: list[float],
    crawler: AsyncWebCrawler,
    semaphore: asyncio.Semaphore,
) -> list[dict]:
    """
    Scrape one search-results page. Steps:
      1. Crawl the page
      2. Ask cheap model to extract all job listings as JSON
      3. Batch-embed all listings, drop below RELEVANCE_THRESHOLD
      4. Skip job_ids already in DB
      5. Store survivors in candidates table
    """
    async with semaphore:
        try:
            result = await crawler.arun(url=url, config=CrawlerRunConfig(cache_mode="BYPASS"))
            await asyncio.sleep(1.0)  # polite rate limiting
        except Exception as e:
            print(f"  [scrape] Error fetching {url}: {e}")
            return []

    if not result.markdown:
        return []

    # ── Extract listings ────────────────────────────────────────────────────────
    prompt = f"""Extract every job listing from this page.
Output ONLY a JSON object containing a 'jobs' array (no markdown):
{{"jobs": [{{"title":"...","company":"...","url":"...","snippet":"..."}}]}}
If no jobs found output: {{"jobs": []}} ..."""

    try:
        raw = await asyncio.to_thread(llm, prompt, require_json=True)
        listings = json.loads(clean_json(raw)).get("jobs", [])
    except Exception:
        return []

    if not listings:
        return []

    # ── Resolve relative URLs ───────────────────────────────────────────────────
    for job in listings:
        u = job.get("url", "")
        if u.startswith("/"):
            job["url"] = f"https://{board}{u}"

    # ── Fetch already-seen IDs ──────────────────────────────────────────────────
    conn = get_db()
    seen = {r[0] for r in conn.execute("SELECT job_id FROM candidates")}
    final = {r[0] for r in conn.execute("SELECT job_id FROM jobs")}
    conn.close()
    known = seen | final

    new_listings = [
        j for j in listings
        if j.get("title") and j.get("url")
        and make_job_id(board, j["url"]) not in known
    ]

    if not new_listings:
        return []

    # ── Batch embed + filter ────────────────────────────────────────────────────
    texts = [
        f"{j.get('title','')} {j.get('company','')} {j.get('snippet','')}"
        for j in new_listings
    ]
    try:
        embeddings = await asyncio.to_thread(get_embeddings_batch, texts)
    except Exception:
        embeddings = [None] * len(new_listings)

    candidates = []
    for job, emb in zip(new_listings, embeddings):
        score = cosine_similarity(profile_embedding, emb) if emb else 0.0
        if score < RELEVANCE_THRESHOLD:
            continue
        candidates.append({
            "job_id":      make_job_id(board, job["url"]),
            "board":       board,
            "title":       job["title"],
            "company":     job.get("company", ""),
            "url":         job["url"],
            "snippet":     job.get("snippet", ""),
            "embed_score": round(score, 4),
        })

    if candidates:
        conn = get_db()
        conn.executemany(
            """INSERT OR IGNORE INTO candidates
               (job_id, board, title, company, url, snippet, embed_score)
               VALUES(:job_id,:board,:title,:company,:url,:snippet,:embed_score)""",
            candidates,
        )
        conn.commit()
        conn.close()
        print(f"  [phase 3] {board}: +{len(candidates)} candidates from {url}")

    return candidates


async def parallel_scrape(
    patterns: dict[str, str],
    profile: dict,
    profile_embedding: list[float],
    crawler: AsyncWebCrawler,
) -> list[dict]:
    """Launch all board × search_term scrapes concurrently, bounded by semaphore."""
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)
    tasks = []
    location = profile.get("location", "UK")

    for domain, pattern in patterns.items():
        urls = build_search_urls(pattern, profile["search_terms"], location)
        for url in urls:
            tasks.append(
                scrape_results_page(url, domain, profile_embedding, crawler, semaphore)
            )

    print(f"[phase 3] Launching {len(tasks)} scrape tasks across {len(patterns)} boards...")
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_candidates = []
    for r in results:
        if isinstance(r, list):
            all_candidates.extend(r)

    print(f"[phase 3] Total candidates after embedding filter: {len(all_candidates)}")
    return all_candidates


# ── Phase 4: Candidate ranking ──────────────────────────────────────────────────

def rank_candidates(candidates: list[dict], profile: dict) -> list[dict]:
    """
    Cheap model scores all candidates in one call and returns top TOP_CANDIDATES.
    Embedding scores are used to pre-sort so the LLM sees the strongest candidates.
    """
    if not candidates:
        print("[phase 4] No candidates. Exiting.")
        return []

    # Pre-sort by embedding score, cap input size
    pool = sorted(candidates, key=lambda x: x.get("embed_score", 0), reverse=True)[:60]
    print(f"[phase 4] Ranking top {len(pool)} candidates...")

    listing_block = "\n".join(
        f"{i+1}. {c['title']} @ {c['company']} | {c['snippet'][:120]}"
        for i, c in enumerate(pool)
    )

    prompt = f"""You are a recruiter. Rate these job listings for a candidate with:
Skills: {', '.join(profile['key_skills'])}
Sectors: {', '.join(profile['sectors'])}
Level: {profile['seniority']}

Select the {TOP_CANDIDATES} best matches. Output ONLY a JSON object containing a 'rankings' array:
{{"rankings": [{{"listing_number": 1, "score": 8, "reason": "..."}}]}}

Listings:
{listing_block}"""

    try:
        raw = llm(prompt, require_json=True)
        ranked_list = json.loads(clean_json(raw)).get("rankings", [])
    except Exception as e:
        print(f"[phase 4] Ranking parse failed ({e}), falling back to embedding order.")
        return pool[:TOP_CANDIDATES]

    top = []
    for entry in ranked_list[:TOP_CANDIDATES]:
        idx = entry.get("listing_number", 1) - 1
        if 0 <= idx < len(pool):
            job = pool[idx].copy()
            job["llm_score"]  = entry.get("score", 0)
            job["llm_reason"] = entry.get("reason", "")
            top.append(job)

    print(f"[phase 4] Top {len(top)} selected for full scrape.")
    return top


# ── Phase 5: Full detail scrape ─────────────────────────────────────────────────

async def scrape_full_details(
    jobs: list[dict],
    crawler: AsyncWebCrawler,
) -> list[dict]:
    """Fetch the full job page for each of the top candidates in parallel."""
    print(f"[phase 5] Fetching full pages for {len(jobs)} jobs...")
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def fetch_one(job: dict) -> dict:
        async with semaphore:
            try:
                # ── Polite Delay ──
                # Introduces a random pause between 1.5 to 3.5 seconds.
                # This breaks the "burst" pattern so servers don't see 5 requests in the same millisecond.
                await asyncio.sleep(random.uniform(1.5, 3.5))
                
                # ── Crawl4AI Config Fix ──
                # Included the missing modern config setup to prevent API execution crashes.
                result = await crawler.arun(
                    url=job["url"], 
                    config=CrawlerRunConfig(cache_mode="BYPASS")
                )
                
                job["full_text"] = result.markdown[:8000] if result.markdown else job.get("snippet", "")
            except Exception as e:
                print(f"  [phase 5] Failed {job['url']}: {e}")
                job["full_text"] = job.get("snippet", "")
        return job

    return list(await asyncio.gather(*[fetch_one(j) for j in jobs]))

# ── Phase 6: Final evaluation ────────────────────────────────────────────────────

def final_evaluation(jobs: list[dict], profile: dict) -> list[dict]:
    """
    Expensive model reads full job descriptions alongside the CV,
    picks the best FINAL_PICKS, and writes a candidate-facing summary for each.
    """
    print(f"[phase 6] Final evaluation ({EXP_MODEL})...")
    cv_text = open(CV_PATH).read()[:3000]

    jobs_block = "\n\n---\n\n".join(
        f"JOB {i+1}: {j['title']} at {j['company']}\nURL: {j['url']}\n\n{j.get('full_text','')[:2000]}"
        for i, j in enumerate(jobs)
    )

    prompt = f"""You are a career advisor helping a candidate find their best job matches.

Candidate CV (summary):
{cv_text}

Review all {len(jobs)} job listings below. Select the {FINAL_PICKS} strongest matches.
For each, output ONLY valid JSON (no markdown):
{{"selections": [
  {{
    "job_number":    2,
    "title":         "...",
    "company":       "...",
    "url":           "...",
    "summary":       "2-3 sentences explaining why this role suits the candidate",
    "match_reasons": ["specific reason 1", "specific reason 2"],
    "concerns":      ["any skill gaps or caveats"]
  }}
]}}

Jobs:
{jobs_block}"""

    try:
        raw = llm(prompt, system="You are a helpful, direct career advisor.", model=EXP_MODEL, require_json=True)
        # Pull from the 'selections' key instead of reading a root list
        results = json.loads(clean_json(raw)).get("selections", [])
    except Exception as e:
        print(f"[phase 6] Evaluation failed: {e}")
        return []

    final = []
    for entry in results[:FINAL_PICKS]:
        idx = entry.get("job_number", 1) - 1
        if 0 <= idx < len(jobs):
            merged = jobs[idx].copy()
            merged.update(entry)
            final.append(merged)

    return final


# ── Phase 7: Output ───────────────────────────────────────────────────────────────

def save_and_display(results: list[dict]):
    """Print results to terminal, write results.md, and save to the jobs table."""
    sep = "─" * 60

    print(f"\n{'═'*60}")
    print("  TOP JOB MATCHES")
    print(f"{'═'*60}")

    conn = get_db()

    for i, job in enumerate(results, 1):
        print(f"\n#{i}  {job['title']}")
        print(f"    {job['company']}")
        print(f"    {job['url']}")
        print(f"\n    {job.get('summary', '')}")
        for r in job.get("match_reasons", []):
            print(f"    ✓  {r}")
        for c in job.get("concerns", []):
            print(f"    ⚠  {c}")
        print(f"\n{sep}")

        try:
            conn.execute(
                """INSERT OR IGNORE INTO jobs(job_id, title, company, url, ai_summary)
                   VALUES(?,?,?,?,?)""",
                (
                    job.get("job_id") or make_job_id(job.get("company", ""), job["url"]),
                    job["title"],
                    job["company"],
                    job["url"],
                    job.get("summary", ""),
                )
            )
        except Exception as e:
            print(f"  [save] DB error: {e}")

    conn.commit()
    conn.close()

    # Markdown output
    with open("results.md", "w") as f:
        f.write("# Job Search Results\n\n")
        f.write(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n")
        for i, job in enumerate(results, 1):
            f.write(f"## {i}. {job['title']} — {job['company']}\n\n")
            f.write(f"**Link:** {job['url']}\n\n")
            f.write(f"{job.get('summary', '')}\n\n")
            for r in job.get("match_reasons", []):
                f.write(f"- ✓ {r}\n")
            for c in job.get("concerns", []):
                f.write(f"- ⚠ {c}\n")
            f.write("\n---\n\n")

    print("\n[phase 7] Saved to DB and results.md")


# ── Main ───────────────────────────────────────────────────────────────────────

async def main():
    init_db()

    # Phase 1 — profile
    profile = get_profile()
    profile_embedding = get_profile_embedding(profile)

    boards = json.load(open(BOARDS_PATH))
    browser_config = BrowserConfig(
        headless=True,
        verbose=False,
        viewport_width=1280,
        viewport_height=800,
        # Force Crawl4AI to use its stealth kit 
        user_agent_mode="random", 
        # Note: Depending on your exact sub-version, magic_mode is a direct keyword here,
        # or enabled automatically when setting advanced anti-detection properties.
    )

    async with AsyncWebCrawler(config=browser_config) as crawler:

        # Phase 2 — board URL patterns
        patterns = await prepare_boards(boards, crawler)
        if not patterns:
            print("[main] No board patterns found. Add manual patterns to boards.json.")
            return

        # Phase 3 — parallel scrape + embedding filter
        candidates = await parallel_scrape(patterns, profile, profile_embedding, crawler)
        if not candidates:
            print("[main] No candidates found. Try lowering RELEVANCE_THRESHOLD or adding boards.")
            return

        # Phase 4 — LLM ranking → top 10
        top10 = rank_candidates(candidates, profile)
        if not top10:
            return

        # Phase 5 — full page fetch
        top10 = await scrape_full_details(top10, crawler)

    # Phase 6 — expensive model final pick (no crawling needed, outside async context)
    final = final_evaluation(top10, profile)
    if not final:
        print("[main] Final evaluation returned no results.")
        return

    # Phase 7 — output
    save_and_display(final)


if __name__ == "__main__":
    asyncio.run(main())