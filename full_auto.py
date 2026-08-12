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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, wait
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

# Generated, worldwide country reference data (see scripts/gen_countries.py).
# This module is plain data with no heavy deps, and lives next to this file so
# the standalone path resolves it too. Insert our own dir defensively in case
# this is imported with a cwd that isn't the repo root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import countries_data as _cd
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
# USAJOBS: the US federal government's own hiring site. Free, self-service API --
# register an email at https://developer.usajobs.gov/apirequest and a key comes
# back by return. Needs BOTH a key and a registered User-Agent (the email itself,
# not a browser string) on every request; see fetch_usajobs.
USAJOBS_API_KEY = os.getenv("USAJOBS_API_KEY", "")
USAJOBS_USER_AGENT = os.getenv("USAJOBS_USER_AGENT", "")
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
# Careerjet public search API: free, worldwide (90+ countries via locale_code).
# The API keys access on (affid, Referer): the shared example affid from
# Careerjet's own API samples works for a localhost referer, which is truthful
# for this single-user localhost prototype and lets it run out of the box.
# Careerjet is migrating to a registered v4 API -- get a free affid at
# https://www.careerjet.com/partners/api and set CAREERJET_AFFID (+ your site as
# CAREERJET_REFERER) for a durable production setup.
CAREERJET_AFFID = os.getenv("CAREERJET_AFFID", "213e213hd127076e2214578c256a6f66")
CAREERJET_REFERER = os.getenv("CAREERJET_REFERER", "http://localhost/")
# ATS-token harvesting (harvest_ats_tokens) needs a domain-restricted search.
# serper.dev's free tier rejects `site:`-operator queries outright ("Query
# pattern not allowed for free accounts"), so by default it builds a plain
# keyword+domain query instead (lower precision, but it actually runs on a
# free plan). Flip this on once/if the serper.dev plan is upgraded to one that
# allows site: search, to get the precise query back with no other changes.
SERPER_SITE_OPERATOR_OK = os.getenv("SERPER_SITE_OPERATOR_OK", "false").strip().lower() == "true"
# Hard cap on (vendor, keyword) query combos per harvest_ats_tokens() call, so
# one profile edit can't burn through the whole serper/CSE credit balance. Sized
# to the actual combo count: harvest.derive_keywords returns up to 16 keyword
# phrases x the 6 ATS vendors here = 96 combos. At the old default of 30, a live
# test (3 profiles, real CV text) showed the keyword-major loop exhausting the
# budget after only ~5 of 16 keywords every time ("skipped 66 of 96 combos"),
# silently discarding the back 11 keywords every run regardless of quality. 96
# gives full coverage; only lower this back down alongside a lower
# derive_keywords cap, or the same silent truncation comes back.
ATS_HARVEST_MAX_QUERIES = int(os.getenv("ATS_HARVEST_MAX_QUERIES", "96"))
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

# Adzuna full-description enrichment (see fetch_adzuna_details). Adzuna's search API
# truncates `description` to an exact-500-char teaser and offers no per-job detail
# route, so its rows were the single largest block of permanently text-starved
# candidates in the store: a measured 261-row live sample had 225 with no full_text
# at all. Phase 5 can't fix it either -- the API hands out a /jobs/land/ad/ tracking
# URL that resolves to a JS interstitial, which _looks_like_redirect_stub correctly
# detects and gives up on, so those jobs reach the FINAL judge on 500 chars of
# company blurb. (Measured: 23 of 38 `strong` verdicts in one store were issued with
# no full_text at all, and the Avara Foods listing that prompted this was graded
# strong on a teaser whose text ends before the word "requirements" appears.)
#
# The fix is not to follow that redirect -- the land URL 403s a plain HTTP client and
# is bot-walled behind the browser too. Adzuna's own detail page for the same ad id
# (/details/{id} on the same host) returns 200 to an ordinary GET and carries the
# whole description in a JSON-LD JobPosting block: measured 5,675 chars for the Avara
# listing against its 500-char teaser, including the "Proven experience working as a
# Data Analyst" clause the judge needed and never saw.
#
# Tuned far more conservatively than the Reed equivalent above, for two measured
# reasons: the response is a ~100KB HTML page rather than a small JSON body, and the
# host starts returning 429 after only a handful of rapid requests -- so this uses a
# narrow pool and abandons the whole batch on repeated 429s rather than hammering a
# host we depend on for discovery.
ADZUNA_DETAIL_ENRICH_ENABLED = os.getenv("ADZUNA_DETAIL_ENRICH_ENABLED", "true").lower() == "true"
ADZUNA_DETAIL_MAX_PER_RUN = int(os.getenv("ADZUNA_DETAIL_MAX_PER_RUN", "80"))
ADZUNA_DETAIL_MAX_WORKERS = int(os.getenv("ADZUNA_DETAIL_MAX_WORKERS", "3"))

# Pages fetched per (source, term) by gather_jobs' per-run discovery. One page
# returns up to 100 (Reed) / 50 (Adzuna) results per term. The fetchers emit a
# "page cap hit" note whenever the last page came back full, so the coverage
# trade-off stays visible per term.
#
# These were cut to 1 on the measurement that a live run discovered 7,800 raw
# listings of which only ~100 were ever examined past the embedding stage,
# making pages 2-3 pure fetch latency in front of first paint. They are back at
# the fetchers' own pages=3 default because two things changed: RANK_EXAMINE_
# BUDGET is now 320 rather than 40-80, so the deeper pages have somewhere to go;
# and across runs Reed and Adzuna are where most strongly-ranked picks actually
# come from, so depth on these two specifically is worth more than depth
# anywhere else.
#
# The cost is real and lands in the worst place -- discovery sits before the
# first "early matches" paint. What keeps it bounded is that gather_jobs fans
# out one pool task per (source, term), so 3 pages is 3 sequential HTTP calls
# inside ONE slot of the 12-wide pool, not 3x the wall clock. If time-to-first-
# card regresses, these two env vars are the knob, not the examine budget.
REED_PAGES_PER_TERM = int(os.getenv("REED_PAGES_PER_TERM", "3"))
ADZUNA_PAGES_PER_TERM = int(os.getenv("ADZUNA_PAGES_PER_TERM", "3"))
# Deeper still when the licensed-sponsor filter is on. That filter keeps ~10% of
# rows (see services/sponsors.py), so the pool behind it has to be correspondingly
# deeper or a profile that needs sponsorship gets a near-empty results page.
REED_PAGES_PER_TERM_SPONSOR = int(os.getenv("REED_PAGES_PER_TERM_SPONSOR", "5"))
ADZUNA_PAGES_PER_TERM_SPONSOR = int(os.getenv("ADZUNA_PAGES_PER_TERM_SPONSOR", "5"))
# Left at 1: USAJobs self-gates to nothing outside the US, so depth here buys
# nothing for the profiles this change is about.
USAJOBS_PAGES_PER_TERM = int(os.getenv("USAJOBS_PAGES_PER_TERM", "1"))

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
#
# ENGINE_-prefixed on purpose. backend/app/services/llm.py already reads the BARE
# names CHEAP_MODEL/MID_MODEL/STRONG_MODEL for the profile-formation calls (CV
# extraction, families, summary). Sharing them would mean an A/B on this search
# pipeline silently retuned CV parsing too -- two unrelated stages moving on one
# knob, with the formation change invisible from any search diagnostic.
CHEAP_MODEL         = os.getenv("ENGINE_CHEAP_MODEL", "gpt-5.4-nano-2026-03-17")
# Middle tier, used only where the cheap tier's coarseness is the limiting factor
# (currently just rank_gate's numeric fit scoring) -- screen_gate stays on
# CHEAP_MODEL since its binary sector/seniority check doesn't need the extra
# reasoning power, and this keeps the highest-volume gate call cheapest.
# Upgraded to GPT-5.6 Luna: rank_gate now also enforces RANK_REJECT_SCORE_FLOOR
# (engine.py), an absolute floor on its 0-100 fit score rather than just a
# relative ranking -- that's a materially heavier ask of this tier than before,
# so it's worth the step up from gpt-5.4-mini.
MID_MODEL           = os.getenv("ENGINE_MID_MODEL", "gpt-5.6-luna")
# Phase 6 final judge only (1-3 calls per run, the only exp-tier call in a live
# search), so a newer/stronger model here costs cents per run, not dollars --
# the highest-leverage place to spend more. Upgraded to GPT-5.6 Terra.
EXP_MODEL           = os.getenv("ENGINE_EXP_MODEL", "gpt-5.6-terra")
EMBED_MODEL         = os.getenv("ENGINE_EMBED_MODEL", "text-embedding-3-small")
# The whole gpt-5.6 family (Luna, Terra) rejects any non-default temperature
# outright (400 Unsupported value) -- only the default (1) is accepted. Every
# call site that passes an explicit temperature must check this rather than
# using llm()'s default: llm() forwards `temperature` straight to the API with
# no guard of its own.
#
# A PREFIX test, not the exact-membership tuple this used to be. Two reasons the
# old form was a trap. (1) The model constants are now env-overridable, so a
# perfectly reasonable value like "gpt-5.6-luna-2026-05-01" would miss the tuple
# and get temperature=0 -> 400 -> the caller's fail-open path. (2) screen_gate
# hardcodes temperature=0 and its except branch keeps the whole batch
# ("fail-open"), so pointing ENGINE_CHEAP_MODEL at a 5.6 model would make every
# listing pass every axis while the console showed only a parse-failure line --
# the gate silently deleted, which is precisely how the rank_gate flat-50 and
# the day-long judge-fallback bugs both happened.
_FIXED_TEMPERATURE_PREFIXES = ("gpt-5.5", "gpt-5.6")


def _fixed_temperature(model: str) -> bool:
    """True when `model` accepts only its default temperature."""
    return any((model or "").startswith(p) for p in _FIXED_TEMPERATURE_PREFIXES)


def _safe_temperature(model: str, wanted: float) -> float:
    """The temperature to actually send: `wanted`, or the model's forced default."""
    return 1 if _fixed_temperature(model) else wanted

PROFILE_CACHE_DAYS  = 7
MAX_CONCURRENT      = 5       # Max general simultaneous crawl requests
# Phase 5 scrape timing knobs (env-tunable). The wall-clock budget caps each
# per-cluster scrape phase; whatever hasn't finished falls back to its snippet.
# The per-page timeout is the dominant per-hang cost -- an anti-bot page that
# never renders burns the whole thing -- so it's kept below the budget.
SCRAPE_BUDGET_SECONDS = float(os.getenv("SCRAPE_BUDGET_SECONDS", "80"))
SCRAPE_PAGE_TIMEOUT_MS = int(os.getenv("SCRAPE_PAGE_TIMEOUT_MS", "20000"))
# Discovery pool: same "budget it, keep partial results" shape as the scrape budget
# above, for gather_jobs' outer per-(source,term)/per-ATS-company pool -- a backstop
# so a single stuck task (network hang, a misbehaving board) can't block the whole
# discovery phase, however long it runs. See also SMARTRECRUITERS_BUDGET_SECONDS,
# a tighter budget on the one vendor that has actually caused this.
DISCOVERY_MAX_WORKERS = int(os.getenv("DISCOVERY_MAX_WORKERS", "12"))
DISCOVERY_BUDGET_SECONDS = float(os.getenv("DISCOVERY_BUDGET_SECONDS", "100"))
RELEVANCE_THRESHOLD = 0.35    # Balanced threshold preventing snippet penalty
TOP_CANDIDATES      = 25      # Pool size handed to the final evaluator
FINAL_PICKS         = 12      # Max results returned, quality-gated
# Cap per single Phase 6 prompt; a larger cluster splits into CONCURRENT batches
# rather than one call risking the client's 90s read timeout (see client below).
#
# 15 -> 20 -> 10. It went UP for cost (fewer, larger calls at JUDGE_POOL=40) and
# has come back down because that trade was being paid in output quality, which
# nothing was measuring. The judge's per-pick output budget collapsed 1305 -> 491
# tokens across runs 20-24 as the pool grew to its cap and v26 widened "backup"
# from 3 to FINAL_PICKS -- and the field the model economised on was the step-D
# requirements checklist, which the fit_level rubric reads to grade every pick
# (see FINAL_EVAL_PROMPT_VERSION 28's note, and scripts/audit_judge_checklists.py's
# tok/pick column). Checklist size tracks that budget monotonically, and
# tests/judge_harness.py reproduces it causally on one unchanged prompt: 6.33
# items at 16 jobs/9 picks per call, 5.15 at 20 jobs/13 picks.
#
# Halving this is the cheapest of the three available levers and the only one
# that costs the candidate nothing: it does not reduce how many roles are judged
# or shown, it just gives each call fewer picks to write up. It is also FASTER,
# not slower, despite being more calls -- the chunks run concurrently in a
# ThreadPoolExecutor, so 4 x 10 wall-clock-beats 2 x 20 while also easing the 90s
# read timeout this constant exists to protect.
#
# What it costs: each chunk is judged INDEPENDENTLY (no cross-chunk comparison,
# see final_evaluation_split), so a smaller chunk gives the model fewer listings
# to weigh each pick against; and each extra call re-pays the ~12k-token system
# prefix, which is why that prefix is a byte-identical constant with 24h prompt
# cache retention (see _FINAL_EVAL_CACHE_KEY) -- the marginal call is mostly
# cached tokens, not fresh ones. Watch tokens_judge_cached_tokens: if the hit
# rate falls, this change starts costing real money instead of cache reads.
# Also note _judge_groups merges thin clusters up to THIS number, so lowering it
# narrows merging too -- deliberately, since both exist to manage the same
# per-call budget.
FINAL_EVAL_MAX_JOBS_PER_CALL = int(os.getenv("FINAL_EVAL_MAX_JOBS_PER_CALL", "10"))
# Explicit output ceiling for the judge call only (0 = leave the model's default
# alone). Set generously: this is NOT a way to make the model write more -- a
# model that stops on "stop" was never near the ceiling, and every measured judge
# call has (7.4k completion tokens per call at the worst observed load). It is a
# guard so that a model-default change, or a genuinely large batch, cannot start
# silently truncating the JSON into a parse failure that disappears down
# _run_final_eval's fail-open path. Whether it ever BINDS is measured, not
# assumed: see _record_llm_usage's length_capped / funnel tokens_judge_length_capped.
FINAL_EVAL_MAX_OUTPUT_TOKENS = int(os.getenv("FINAL_EVAL_MAX_OUTPUT_TOKENS", "32000"))
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
# characters before board "Similar Jobs" boilerplate started.
#
# Raised 3000 -> 5000 (2026-07-27) on measured evidence, not headroom. A
# tier_analysis --text-mode full run located, by character offset, the exact
# clause the final judge quoted when it rejected a job this stage had scored into
# the judge pool: of the 5 locatable ones, 4 sat at offsets 3456 / 3758 / 3817 /
# 4546 -- past the old 3000 cap and ALL under 5000. That is the single
# highest-yield text change available here, because it needs no extra fetching:
# the characters are already sitting in JobSeen.full_text, just unread. The store
# they came from ran median 3227 / p75 4592 / p90 6079 chars per scraped page
# (max 8000, FINAL_EVAL_JOB_TEXT_CHARS' own cap), with 109 of 192 pages longer
# than 3000 -- so the old cap was truncating the majority of pages, and doing it
# right where a JD's requirements section tends to start. Note this only spends
# input tokens on candidates that HAVE a scraped page; the ~91% of the store
# carrying only a ~455-char source teaser is unaffected either way (see the
# text-supply note in CLAUDE.md). Raising it further has no evidence behind it
# yet -- nothing was found between 5000 and 8000.
#
# Worth doing only because screen_v12 fixed listing_ok's board-chrome
# false-positive: before that, supplying the cheap tiers MORE text made them
# strictly worse, so this change would have backfired. Re-check that ordering
# still holds (tests/gate_harness.py --ground-truth, snippet vs full) before
# raising any of these budgets again.
RANK_LISTING_TEXT_CHARS = 5000

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
# Curated high-signal tokens layered ON TOP of the generated worldwide data
# (countries_data), so long-standing behaviour never regresses even if a name
# drops out of the dataset (e.g. UK county/region names Adzuna/Reed emit as a
# job location, which aren't cities). These are merged into the generated
# per-country token sets below.
# Country-level names/aliases checked BEFORE city tokens (pass 1), so an explicit
# country wins over a city that also exists elsewhere. These supplement the
# generated name tokens with UK county/region names and abbreviations the
# dataset doesn't carry as cities.
_CURATED_NAME_EXTRA = {
    "gb": {"u.k.", "great britain", "britain", "england", "scotland", "wales",
           "northern ireland", "essex", "kent", "surrey", "sussex", "hampshire",
           "yorkshire", "lancashire", "cheshire", "devon", "cornwall",
           "milton keynes", "southend-on-sea"},
    "us": {"u.s.", "u.s.a.", "america", "texas", "california", "florida",
           "washington"},
    "de": {"deutschland", "munchen"},
    "it": {"italia"},
    "nl": {"holland"},
}

# The original hand-curated city tokens, restored as pass-2 extras so the core
# markets never lose a city the dataset spells differently (geonames stores "New
# York City", not "new york") or ranks below the population threshold.
_CURATED_CITY_EXTRA = {
    "gb": {"london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh",
           "bristol", "liverpool", "sheffield", "newcastle", "nottingham",
           "leicester", "coventry", "cardiff", "belfast", "cambridge", "oxford",
           "reading", "brighton", "aberdeen", "dundee", "southampton",
           "portsmouth", "southend",
           # High-volume towns/cities the geonames threshold or an earlier-sorting
           # country would otherwise miss -- so a non-scoped source's listing here
           # positively tags gb instead of leaning on the keep-unknowns fallback.
           # Multi-syllable/unambiguous only -- generic single words that are also
           # well-known foreign cities (york -> New York, newport -> Newport Beach,
           # plymouth/gloucester -> MA, bath -> ME, hull, stoke) are deliberately
           # NOT added: they'd false-match a foreign location, and the keep-unknowns
           # fallback already keeps a bare UK-town listing without a positive tag.
           "slough", "watford", "luton", "basingstoke",
           "swindon", "milton keynes", "stevenage", "warrington",
           "guildford", "chelmsford", "peterborough", "northampton",
           "colchester", "farnborough", "bracknell", "high wycombe",
           "aylesbury", "crawley", "maidenhead", "wokingham", "newbury",
           "harlow", "blackburn", "rochdale", "middlesbrough", "wrexham",
           "swansea", "telford", "wolverhampton", "solihull", "croydon"},
    "us": {"new york", "san francisco", "los angeles", "chicago", "seattle",
           "austin", "boston", "denver", "atlanta", "dallas", "houston",
           "san diego", "philadelphia"},
    "ca": {"toronto", "vancouver", "montreal", "ottawa", "calgary"},
    "au": {"sydney", "melbourne", "brisbane", "perth"},
    "de": {"berlin", "munich", "hamburg", "frankfurt"},
    "fr": {"paris", "lyon", "marseille"},
    "in": {"bangalore", "bengaluru", "mumbai", "delhi", "hyderabad", "pune"},
    "it": {"rome", "milan", "turin", "venice"},
    "nl": {"amsterdam", "rotterdam", "the hague"},
    "at": {"vienna"},
    "pl": {"warsaw", "krakow", "wroclaw"},
    "za": {"johannesburg", "cape town", "pretoria"},
}


def _build_country_tokens():
    """Merge the generated worldwide data with the curated extras into two
    lookups: single-word tokens (matched on a word boundary) and multi-word /
    punctuated phrases (matched as substrings, which are specific enough)."""
    words: dict[str, set[str]] = {}
    phrases: dict[str, set[str]] = {}
    merged: dict[str, set[str]] = {}
    for code in _cd.CC_DISPLAY:
        toks = set(_cd.COUNTRY_TOKENS.get(code, []))
        toks |= _CURATED_NAME_EXTRA.get(code, set())
        toks |= _CURATED_CITY_EXTRA.get(code, set())
        merged[code] = toks
        for t in toks:
            (phrases if (" " in t or "." in t or "-" in t) else words).setdefault(
                code, set()).add(t)
    return words, phrases, merged


_COUNTRY_WORD_TOKENS, _COUNTRY_PHRASE_TOKENS, _COUNTRY_TOKENS = _build_country_tokens()


def country_of(job_location: str) -> str | None:
    """Best-effort country code for a free-text job location, or None if it
    can't be confidently determined (ambiguous/blank/unrecognised). Single-word
    tokens (city/country names) match on a whole-word boundary so a short token
    can't false-positive as a substring ("nice" inside "venice"); multi-word or
    punctuated tokens ("new york", "u.k.") match as substrings, which are
    specific enough. Country-name tokens are checked before city tokens so an
    explicit country in the string wins over a city that also exists elsewhere."""
    loc = (job_location or "").strip().lower()
    if not loc:
        return None
    loc_words = set(re.findall(r"[a-z]+", loc))

    def _hit(code: str) -> bool:
        if _COUNTRY_WORD_TOKENS.get(code, set()) & loc_words:
            return True
        return any(p in loc for p in _COUNTRY_PHRASE_TOKENS.get(code, set()))

    # Pass 1: country name/alias (highest confidence).
    for code, names in _cd.COUNTRY_NAME_TOKENS.items():
        nset = set(names) | _CURATED_NAME_EXTRA.get(code, set())
        w = {n for n in nset if " " not in n and "." not in n and "-" not in n}
        p = nset - w
        if (w & loc_words) or any(ph in loc for ph in p):
            return code
    # Pass 2: city tokens.
    for code in _cd.CITY_TOKENS:
        if _hit(code):
            return code
    return None


def country_matches(job_location: str, allowed) -> bool:
    """True if the location positively names one of the `allowed` country codes,
    by country-name/alias token OR city token. Unlike country_of (which returns a
    single first-in-sort-order match and so can misroute an ambiguous city -- e.g.
    "Newcastle" resolves to `au` because Australia sorts before `gb` in the
    worldwide token set, even though gb also carries "newcastle"), this checks the
    allowed codes DIRECTLY, so a city that IS a valid allowed-country city is
    recognised regardless of collisions elsewhere. Used by the country filter to
    keep a job whose town is a legitimate allowed-country place before falling back
    to country_of's single guess to decide 'confirmed foreign'."""
    loc = (job_location or "").strip().lower()
    if not loc:
        return False
    allowed = set(allowed or ())
    if not allowed:
        return False
    loc_words = set(re.findall(r"[a-z]+", loc))
    for code in allowed:
        # Country name / alias (same split as country_of's pass 1).
        nset = set(_cd.COUNTRY_NAME_TOKENS.get(code, [])) | _CURATED_NAME_EXTRA.get(code, set())
        w = {n for n in nset if " " not in n and "." not in n and "-" not in n}
        p = nset - w
        if (w & loc_words) or any(ph in loc for ph in p):
            return True
        # City tokens (the merged word/phrase sets country_of's pass 2 reads).
        if _COUNTRY_WORD_TOKENS.get(code, set()) & loc_words:
            return True
        if any(ph in loc for ph in _COUNTRY_PHRASE_TOKENS.get(code, set())):
            return True
    return False


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
                # Reed is UK-only, so the currency is never in doubt. It carries
                # no period field at all and returns an hourly rate for hourly
                # roles in the same numbers as an annual one for salaried roles,
                # so the period is deliberately left unset for
                # services/salary.py to infer from magnitude.
                "salary_currency": "GBP",
                "snippet": job.get("jobDescription", ""),
                "posted_at": _uk_date_to_iso(job.get("date")),
                "expires_at": _uk_date_to_iso(job.get("expirationDate")),
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


def fetch_reed_details(job_ids: List[str], dead_out: set | None = None) -> Dict[str, str]:
    """jobId -> full plain-text job description, for the ids that resolved.

    Reed's search response truncates jobDescription to a ~455-char teaser; this
    per-job endpoint returns the whole thing (see REED_DETAIL_ENRICH_ENABLED for
    why that matters). One cheap HTTP call each, fanned out over the same
    12-wide pool gather_jobs uses -- a measured 24-id batch resolved in 1.7s.

    Fails SOFT and per-id: a 404/410 (listing pulled), a timeout, or an empty
    description simply doesn't appear in the returned dict, and the caller keeps
    whatever text it already had. Enrichment that can't be done is never a reason
    to lose a candidate.

    `dead_out`, when given, additionally collects the ids that came back 404/410.
    A 404 from Reed's OWN per-job endpoint, for an id Reed's OWN search API just
    returned, is as strong a "this listing is gone" signal as _dead_listing_signal
    gets from a scrape -- and this call is already being made ~100 times a run, so
    the signal was being discarded for free. Same exclusions as
    _dead_listing_signal: 403/429/5xx and timeouts mean blocked/erroring, never
    gone, and must not land here."""
    ids = [j for j in dict.fromkeys(job_ids) if j][:REED_DETAIL_MAX_PER_RUN]
    if not ids or not REED_API_KEY or not REED_DETAIL_ENRICH_ENABLED:
        return {}

    def _one(job_id: str) -> tuple[str, str, bool]:
        try:
            r = requests.get(f"https://www.reed.co.uk/api/1.0/jobs/{job_id}",
                             auth=HTTPBasicAuth(REED_API_KEY, ""), timeout=12)
            if r.status_code != 200:
                return job_id, "", r.status_code in (404, 410)
            return job_id, _strip_html(r.json().get("jobDescription") or ""), False
        except Exception:
            return job_id, "", False

    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(_one, ids))
    out = {job_id: text for job_id, text, _dead in results if text}
    if dead_out is not None:
        dead_out.update(job_id for job_id, _t, dead in results if dead)
    emit(f"   [reed] full descriptions fetched for {len(out)}/{len(ids)} listing(s)")
    return out


# Adzuna's API hands out a click-tracking URL ending in the numeric ad id
# (https://www.adzuna.co.uk/jobs/land/ad/5786752438?se=...). The id plus that URL's
# OWN host is everything fetch_adzuna_details needs, which is why it takes URLs
# rather than (id, country) pairs -- Adzuna's per-country websites live on a dozen
# different TLDs (.co.uk/.com/.de/.com.au/...) that don't follow from the API's
# two-letter country code, and reusing the host the listing arrived on sidesteps
# having to maintain that mapping at all.
_ADZUNA_AD_ID_RE = re.compile(r"adzuna\.[a-z.]+/(?:jobs/)?(?:land/ad|details)/(\d+)", re.I)


def adzuna_ad_id(url: str) -> str | None:
    m = _ADZUNA_AD_ID_RE.search(url or "")
    return m.group(1) if m else None


def _adzuna_detail_url(url: str) -> str | None:
    ad_id = adzuna_ad_id(url)
    if not ad_id:
        return None
    host = _scrape_host(url)
    return f"https://{host}/details/{ad_id}" if host else None


def _jobposting_from_html(page: str) -> dict:
    """Pull the schema.org JobPosting description (+ datePosted/validThrough,
    when present) out of any HTML page that publishes one.

    Written for Adzuna's detail pages, but there is nothing Adzuna-specific in
    it -- JobPosting is a public schema every board that wants to be indexed by
    Google for Jobs publishes, so the same reader serves the listing-liveness
    verification pass too (see engine._verify_listings_alive), where
    validThrough is the one FORWARD-looking expiry signal available without
    asking the employer.

    Read from the page's JSON-LD rather than by scraping its rendered markup: the
    schema.org block is a stable contract the site maintains for search engines,
    while the surrounding HTML is ordinary site chrome that redesigns freely. It
    also arrives already scoped to THIS posting, so none of the board's "similar
    jobs" list can leak in -- the exact contamination the final judge's SCOPE OF
    EACH POSTING'S TEXT rule exists to warn about.

    datePosted/validThrough ride along in the SAME block already being parsed for
    description -- free to read, no extra fetch. Adzuna's own search API supplies
    `created` (-> posted_at) but never an expiry, so validThrough is the only
    source of expires_at this pipeline has for Adzuna at all."""
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
                         page or "", re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for item in (data if isinstance(data, list) else [data]):
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                text = _strip_html(item.get("description") or "")
                if text:
                    return {
                        "description": text,
                        "posted_at": _loose_date_to_iso(item.get("datePosted")),
                        "expires_at": _loose_date_to_iso(item.get("validThrough")),
                    }
    return {}


# Kept so fetch_adzuna_details and anything else reading the old name is
# untouched by the rename -- the function was never Adzuna-specific.
_adzuna_description_from_html = _jobposting_from_html


def fetch_adzuna_details(urls: List[str], dead_out: set | None = None) -> Dict[str, dict]:
    """Adzuna listing URL -> {"text", "posted_at", "expires_at"}, for the ones that
    resolved (a key is only present at all when a description was actually found;
    posted_at/expires_at inside it may still individually be None).

    The Adzuna counterpart to fetch_reed_details -- see ADZUNA_DETAIL_ENRICH_ENABLED
    for why this exists and why it's throttled harder. Keyed by the ORIGINAL url the
    caller passed (not the derived /details/ one) so the caller can map results back
    onto its own job dicts without re-deriving anything.

    Fails SOFT and per-url exactly like the Reed version: a non-200, a timeout, or a
    page with no JSON-LD JobPosting simply doesn't appear in the returned dict and the
    caller keeps whatever text it had. On repeated 429s the whole remaining batch is
    abandoned rather than retried -- enrichment is an optimisation, and getting rate-
    limited out of DISCOVERY (which shares this host) would cost far more than the
    text is worth.

    `dead_out`, when given, collects the URLs whose detail page proves the listing is
    gone -- three signals, all free on a fetch already being made: a 404/410 from the
    detail endpoint itself (see fetch_reed_details for the reasoning; 429 is pointedly
    excluded, this host rate-limits aggressively and a throttle says nothing about
    whether the posting exists); a JSON-LD validThrough date that has already passed
    (the board's own stated closing date -- structured, no text-pattern guessing); or
    the page's own rendered text matching the (now board-chrome-aware) expired-listing
    phrasing. That last one exists because a closed Adzuna listing's JSON-LD often
    keeps serving its original description for SEO -- the description text alone gives
    no hint the ad has closed, which is exactly how one survives past its real
    expiry with nothing in the pipeline able to tell."""
    seen = list(dict.fromkeys(u for u in urls if u))[:ADZUNA_DETAIL_MAX_PER_RUN]
    targets = [(u, _adzuna_detail_url(u)) for u in seen]
    targets = [(u, d) for u, d in targets if d]
    if not targets or not ADZUNA_DETAIL_ENRICH_ENABLED:
        return {}

    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9",
    }
    throttled = [0]        # consecutive-ish 429 count, shared across workers
    _THROTTLE_GIVE_UP = 3
    now_iso = datetime.utcnow().isoformat()

    def _one(target: tuple[str, str]) -> tuple[str, dict, bool]:
        original, detail_url = target
        if throttled[0] >= _THROTTLE_GIVE_UP:
            return original, {}, False
        try:
            r = requests.get(detail_url, headers=headers, timeout=15)
            if r.status_code == 429:
                throttled[0] += 1
                return original, {}, False
            if r.status_code != 200:
                return original, {}, r.status_code in (404, 410)
            parsed = _adzuna_description_from_html(r.text)
            if not parsed:
                return original, {}, False
            expired = bool(parsed.get("expires_at")) and parsed["expires_at"] < now_iso
            if not expired:
                # Belt-and-braces: the JSON-LD description often survives a real
                # closure untouched (no validThrough update either), so also check
                # the rendered page for the same closure phrasing Phase 5 looks
                # for -- unbounded here (no position/length gate) since this is
                # always exactly one posting's own page, not a multi-listing
                # scrape that could pick up an unrelated "similar jobs" mention.
                # _visible_text, not _strip_html: an unbounded search over a whole
                # document must not be able to match inside a <script> i18n string
                # table, which is the one way this could produce a false positive.
                expired = bool(_EXPIRED_LISTING_RE.search(_visible_text(r.text)))
            return original, parsed, expired
        except Exception:
            return original, {}, False

    with ThreadPoolExecutor(max_workers=max(1, ADZUNA_DETAIL_MAX_WORKERS)) as ex:
        results = list(ex.map(_one, targets))
    out = {u: {"text": p["description"], "posted_at": p.get("posted_at"),
               "expires_at": p.get("expires_at")}
           for u, p, dead in results if p and not dead}
    if dead_out is not None:
        dead_out.update(u for u, _p, dead in results if dead)
    note = " (rate-limited, batch cut short)" if throttled[0] >= _THROTTLE_GIVE_UP else ""
    emit(f"   [adzuna] full descriptions fetched for {len(out)}/{len(targets)} listing(s){note}")
    return out


# Country-level location strings that Adzuna's `where` geocoder rejects (the cc
# endpoint already scopes the country, so these must be omitted, not passed).
# Generated from the Adzuna-supported nodes' names/aliases, plus a few bare
# codes/abbreviations the generator doesn't carry as tokens.
_ADZUNA_COUNTRY_LEVEL = set(_cd.ADZUNA_COUNTRY_LEVEL) | {
    "uk", "gb", "us", "usa", "u.s.", "u.k.",
}

# Adzuna only operates in these country nodes; any other cc returns
# UNSUPPORTED_COUNTRY, so we skip the call entirely (see fetch_adzuna).
_ADZUNA_SUPPORTED = _cd.ADZUNA_SUPPORTED


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

    # Adzuna doesn't operate in this country -- calling it just returns
    # UNSUPPORTED_COUNTRY (log noise + wasted HTTP). Skip; Google Jobs / JSearch
    # / Careerjet / the ATS pool cover these places instead.
    if cc not in _ADZUNA_SUPPORTED:
        return []

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
        # Adzuna intermittently returns an empty/invalid body (parsed as
        # "Expecting value: line 1 column 1") -- usually transient rate-limiting,
        # not a real outage. A single silent break here used to drop the whole
        # term's Adzuna results with no retry, and with no per-source balancing
        # downstream that collapsed a run onto the careerjet aggregator. Retry once
        # with a short backoff and a longer timeout before giving up on the term
        # (mirrors the JSearch read-timeout retry).
        results = None
        for attempt in range(2):
            try:
                timeout = 12 if attempt == 0 else 20
                r = requests.get(url, params=base_params, timeout=timeout)
                body = r.json()
                results = body.get("results", [])
                break
            except Exception as e:
                if attempt == 0:
                    emit(f"   [!] Adzuna ({cc}) API Error: {e} -- retrying once")
                    time.sleep(1.5)
                    continue
                emit(f"   [!] Adzuna ({cc}) API Error: {e} -- giving up on this term")
        if results is None:
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
                # Adzuna documents salary_min/max as ANNUALISED regardless of
                # how the employer quoted it, so the period is known even though
                # the field doesn't exist. Currency is left unset: it varies by
                # country endpoint and guessing it wrong is worse than a figure
                # rendered without a symbol.
                "salary_period": "year",
                # Adzuna MODELS a salary for postings that state none (its own docs
                # call salary_min/max("salary_is_predicted": 1) an estimate, not the
                # employer's figure). Left uncaptured, that guess renders identically
                # to a real stated salary -- a live case (Caristo Diagnostics "Data
                # Operations Analyst") showed as a confident "£54,208 a year" (a single
                # non-round figure with min==max, the tell of a model output) when the
                # posting's own text said "Competitive salary" with no number at all.
                # See engine._role_salary_fields/_filter_by_salary, which must never
                # treat this as a CONFIRMED figure for a hard drop, and
                # _listing_salary_suffix, which must label it for the cheap-tier gates.
                "salary_is_predicted": str(job.get("salary_is_predicted", "0")) == "1",
                "snippet": job.get("description", ""),
                "posted_at": _loose_date_to_iso(job.get("created")),
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


def _usajobs_to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_usajobs(query: str, location: str = "", country_code: str = "us", pages: int = 1) -> List[Dict]:
    """USAJOBS is the US federal government's own hiring site -- covers federal
    (plus some excepted-service/legislative) postings no other source here
    reaches at all. Skips execution if the profile's resolved country isn't US,
    or no key/User-Agent is registered (see USAJOBS_API_KEY/USAJOBS_USER_AGENT).
    `location` is a city/state string; empty means nationwide (LocationName
    omitted), the same convention fetch_reed/fetch_adzuna use for national/
    international scope."""
    if (country_code or "us").strip().lower() != "us" or not USAJOBS_API_KEY or not USAJOBS_USER_AGENT:
        return []

    url = "https://data.usajobs.gov/api/search"
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": USAJOBS_USER_AGENT,
        "Authorization-Key": USAJOBS_API_KEY,
    }
    page_size = 250  # USAJOBS' max ResultsPerPage
    jobs: List[Dict] = []
    last_page_full = False
    for page in range(1, pages + 1):
        params = {"Keyword": query, "ResultsPerPage": page_size, "Page": page}
        if location and location.strip():
            params["LocationName"] = location
        try:
            r = requests.get(url, headers=headers, params=params, timeout=12)
            results = r.json().get("SearchResult", {}).get("SearchResultItems", [])
        except Exception as e:
            emit(f"   [!] USAJOBS API Error: {e}")
            last_page_full = False
            break
        for item in results:
            job = item.get("MatchedObjectDescriptor", {}) or {}
            remun = (job.get("PositionRemuneration") or [{}])[0]
            summary = ((job.get("UserArea") or {}).get("Details") or {}).get("JobSummary", "")
            jobs.append({
                "board": "usajobs",
                "title": job.get("PositionTitle", ""),
                "company": job.get("OrganizationName", ""),
                "url": job.get("PositionURI", ""),
                "location": job.get("PositionLocationDisplay", ""),
                "salary_min": _usajobs_to_float(remun.get("MinimumRange")),
                "salary_max": _usajobs_to_float(remun.get("MaximumRange")),
                # "Per Year" / "Per Hour" -- federal pay scales include both.
                "salary_period": (remun.get("RateIntervalCode")
                                  or remun.get("Description") or None),
                "salary_currency": "USD",
                "snippet": summary,
                "posted_at": _loose_date_to_iso(job.get("PublicationStartDate")),
                "expires_at": _loose_date_to_iso(job.get("ApplicationCloseDate")),
            })
        last_page_full = len(results) >= page_size
        if len(results) < page_size:
            break
    if last_page_full:
        emit(f"   [usajobs] '{query}': page cap ({pages}) hit with a full page -- more results likely available")
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
                "snippet": job.get("description", ""),
                "posted_at": _loose_date_to_iso(job.get("publication_date")),
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
                # JSearch is the one source that states its period explicitly
                # (HOUR/DAY/WEEK/MONTH/YEAR), and it genuinely varies -- reading
                # its numbers as annual is how a $25/hour role gets dropped for
                # a candidate with a $30,000 floor.
                "salary_period": job.get("job_salary_period"),
                "salary_currency": job.get("job_salary_currency"),
                "snippet": job.get("job_description", ""),
                "posted_at": _loose_date_to_iso(job.get("job_posted_at_datetime_utc")),
                "expires_at": _loose_date_to_iso(job.get("job_offer_expiration_datetime_utc")),
            })
        return jobs
    except Exception as e:
        emit(f"   [!] JSearch API Error: {e}")
        return []


# Careerjet interface locales for markets where the local language (not English)
# is the natural default, so results/currency come back sensibly. Any cc not
# listed defaults to en_<CC>, which Careerjet accepts for its many English
# locales (en_AE, en_SA, en_NG, en_IE, ...). The `location` param does the actual
# geo-scoping regardless, so an imperfect locale still returns in-country jobs.
_CAREERJET_LOCALES = {
    "fr": "fr_FR", "de": "de_DE", "es": "es_ES", "it": "it_IT", "nl": "nl_NL",
    "pt": "pt_PT", "br": "pt_BR", "pl": "pl_PL", "at": "de_AT", "ch": "de_CH",
    "be": "fr_BE", "ru": "ru_RU", "ua": "uk_UA", "tr": "tr_TR", "jp": "ja_JP",
    "cn": "zh_CN", "tw": "zh_TW", "kr": "ko_KR", "vn": "vi_VN", "th": "th_TH",
    "id": "id_ID", "mx": "es_MX", "ar": "es_AR", "cl": "es_CL", "co": "es_CO",
    "se": "sv_SE", "no": "no_NO", "dk": "da_DK", "fi": "fi_FI", "gr": "el_GR",
    "cz": "cs_CZ", "hu": "hu_HU", "ro": "ro_RO", "sa": "en_SA", "ae": "en_AE",
    "qa": "en_QA", "kw": "en_KW", "eg": "en_EG", "ng": "en_NG", "ke": "en_KE",
    "za": "en_ZA", "in": "en_IN", "sg": "en_SG", "my": "en_MY", "ph": "en_PH",
    "hk": "en_HK", "au": "en_AU", "nz": "en_NZ", "ca": "en_CA", "ie": "en_IE",
    "us": "en_US", "gb": "en_GB",
}


def _careerjet_locale(country_code: str) -> str:
    cc = (country_code or "gb").strip().lower()
    return _CAREERJET_LOCALES.get(cc, f"en_{cc.upper()}")


def fetch_careerjet(query: str, location: str = "United Kingdom",
                    country_code: str = "gb", pages: int = 1) -> List[Dict]:
    """Careerjet public search API -- free and worldwide, the coverage backstop
    for countries Adzuna/Reed don't serve. `location` geo-scopes the search;
    `locale_code` sets the interface region. Fails soft (returns [] on any
    error) like every other source."""
    if not CAREERJET_AFFID:
        return []
    clean_loc = normalize_location(location)
    params = {
        "keywords": query,
        "location": clean_loc,
        "affid": CAREERJET_AFFID,
        "locale_code": _careerjet_locale(country_code),
        "pagesize": 50,
        "page": 1,
        "sort": "relevance",
        # The API requires these (it geolocates the caller); fixed placeholders
        # are fine since `location`/`locale_code` drive the actual scoping.
        "user_ip": "11.22.33.44",
        "user_agent": "Mozilla/5.0 (compatible; four-in-a-thousand/1.0)",
    }
    # Careerjet rejects the call (403 "Undeclared referrer") without a Referer
    # header, and the shared example affid only accepts certain referers -- see
    # CAREERJET_REFERER. Set your own affid+referer for production.
    headers = {"Referer": CAREERJET_REFERER}
    jobs: List[Dict] = []
    try:
        r = requests.get("http://public.api.careerjet.net/search",
                         params=params, headers=headers, timeout=12)
        data = r.json()
    except Exception as e:
        emit(f"   [!] Careerjet API Error: {e}")
        return []
    if data.get("type") != "JOBS":
        # A bad locale / no match returns type "ERROR" (with an `error` message)
        # or a "LOCATIONS"/"KEYWORDS" suggestion payload, not jobs. Surface a
        # real error once so a misconfigured affid/locale is visible.
        if data.get("type") == "ERROR":
            emit(f"   [!] Careerjet ({params['locale_code']}): {data.get('error')}")
        return []
    for job in data.get("jobs", []):
        # Descriptions come back with <b>…</b> highlight markup; strip to plain
        # text so the gate/rank stages read it like every other source's snippet.
        jobs.append({
            "board": "careerjet",
            "title": _strip_html(job.get("title", "") or ""),
            "company": job.get("company", "") or "",
            "url": job.get("url", "") or "",
            "location": job.get("locations", "") or clean_loc,
            # Careerjet exposes salary only as a free-text string, not min/max.
            "salary_min": None,
            "salary_max": None,
            "snippet": _strip_html(job.get("description", "") or ""),
            "posted_at": _loose_date_to_iso(job.get("date")),
        })
    return jobs


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
# location string (Google/JSearch/Careerjet). Adzuna/Reed instead take an empty
# string. Sourced from the generated worldwide data so any country a profile can
# resolve to gets its correct display name (not a UK fallback).
_CC_DISPLAY = dict(_cd.CC_DISPLAY)


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
def _pages_for(profile: Dict, normal: int, sponsor: int) -> int:
    """Page depth for this run: deeper when the licensed-sponsor filter is on,
    since that filter keeps roughly a tenth of what it sees."""
    return sponsor if profile.get("visa_sponsor_only") else normal


class ReedSource:
    name, tier = "reed", "fast"
    def fetch_term(self, profile, term):
        return fetch_reed(term, _geo_scoped_location(profile),
                          profile.get("adzuna_country_code", "gb"),
                          pages=_pages_for(profile, REED_PAGES_PER_TERM,
                                           REED_PAGES_PER_TERM_SPONSOR))
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
                            pages=_pages_for(profile, ADZUNA_PAGES_PER_TERM,
                                             ADZUNA_PAGES_PER_TERM_SPONSOR))
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(self.fetch_term(profile, term))
        return out


class USAJobsSource:
    # "fast" tier like Reed/Adzuna: a plain, free, structured-API call that
    # self-gates to nothing for a non-US profile (see fetch_usajobs), so it
    # costs nothing to include unconditionally.
    name, tier = "usajobs", "fast"
    def fetch_term(self, profile, term):
        return fetch_usajobs(term, _geo_scoped_location(profile),
                             profile.get("adzuna_country_code", "gb"),
                             pages=USAJOBS_PAGES_PER_TERM)
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


class CareerjetSource:
    # "fast" tier (plain HTTP, text-complete): runs every run incl. the first, so
    # countries Adzuna/Reed can't serve still get geo-targeted results up front.
    name, tier = "careerjet", "fast"
    def fetch_term(self, profile, term):
        return fetch_careerjet(term, _google_location(profile),
                               profile.get("adzuna_country_code", "gb"))
    def fetch(self, profile, since=None):
        out: List[Dict] = []
        for term in _terms(profile):
            out.extend(self.fetch_term(profile, term))
        return out


# ── Posting-date normalisation ───────────────────────────────────────────────
# Every source states when a job was posted and every source states it differently.
# Until this existed only the ATS feeds carried a date at all, so the four sources
# that supply most real listings (Reed, Adzuna, JSearch, Careerjet) produced rows
# with no age at any stage -- which is why a months-old listing could rank top with
# nothing anywhere in the pipeline able to notice. Each helper fails soft to None:
# an unparseable date must mean "age unknown" (and so no penalty), never "old".

def _epoch_ms_to_iso(v) -> str | None:
    """Lever's createdAt/updatedAt are epoch milliseconds."""
    try:
        return datetime.utcfromtimestamp(int(v) / 1000).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _uk_date_to_iso(v) -> str | None:
    """Reed states dates as DD/MM/YYYY."""
    try:
        return datetime.strptime(str(v).strip(), "%d/%m/%Y").isoformat()
    except (TypeError, ValueError):
        return None


def _loose_date_to_iso(v) -> str | None:
    """ISO-ish timestamps from Adzuna (`created`), JSearch
    (`job_posted_at_datetime_utc`), Remotive (`publication_date`) and Careerjet
    (`date`). Tolerates a trailing Z and a bare 'YYYY-MM-DD HH:MM:SS'."""
    s = (str(v).strip() if v else "")
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None).isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s[:19], fmt).isoformat()
        except ValueError:
            continue
    return None


# Fallback threshold when a candidate's own profile carries no
# max_listing_age_days at all -- the standalone CV_PATH profile dict `python
# full_auto.py` builds has no ProfileAttribute table to read one from. The
# backend always sets engine_profile["max_listing_age_days"] (default 30, see
# backend/app/config.DEFAULT_MAX_LISTING_AGE_DAYS -- kept in sync manually,
# the two modules share no config import) and ["max_listing_age_hard"], which
# is what actually decides ELIMINATE-vs-downgrade below. Below the threshold a
# listing isn't mentioned at all -- ordinary board latency is not a signal and
# naming it would invite the model to penalise it.
DEFAULT_MAX_LISTING_AGE_DAYS = 30

# Our OWN observation clock (JobSeen.first_seen / seen_days), used alongside the
# board's claimed date. It is a LOWER bound on the ad's true age and nothing else:
# it can only ever RAISE the age, never certify a listing as fresh.
#
# Why it earns its place: only ~15% of the live store carries a posted_at at all,
# aggregators reset that date on every re-syndication, and Greenhouse has no
# creation date to give (see posted_at_approx). None of that touches how long WE
# have been seeing the ad, which no source can launder.
OBSERVED_MENTION_DAYS = 21      # below this, ordinary board latency -- say nothing
# Distinct days of re-observation before an ad reads as a standing pipeline ad
# rather than a vacancy. ~4 weeks is a normal UK time-to-hire, so an ad still
# being re-advertised on this many SEPARATE days has outlived a normal req.
EVERGREEN_SEEN_DAYS = 30
# ...but only if it was present on most days since we first saw it. 30 days
# scattered over six months is a genuine repost of a new req, not an evergreen ad.
EVERGREEN_SEEN_DENSITY = 0.6
# The observation clock is meaningless until the STORE itself is old enough to
# have observed anything. A reset store (or a new deployment) makes every row look
# newly-discovered, and a per-row threshold alone cannot tell that apart from a
# genuinely new listing. Below this, the observation clause is suppressed
# entirely -- which correctly makes all of this a no-op on first ship.
OBSERVATION_MIN_STORE_DAYS = 30


def _days_since(iso: str | None, now: datetime) -> int | None:
    """Whole days since an ISO timestamp, or None if absent/unparseable."""
    parsed = _loose_date_to_iso(iso)
    if not parsed:
        return None
    try:
        return (now - datetime.fromisoformat(parsed)).days
    except (TypeError, ValueError):
        return None


def _humanise_days(days: int) -> str:
    months = days // 30
    return f"{months} month{'s' if months != 1 else ''}" if months >= 2 else f"{days} days"


def _listing_definite_age_days(job: dict, now: datetime) -> int | None:
    """Days since a DEFINITE (non-approximate) stated posting date, or None when
    there is no posted date at all, or only an aliased updated_at (Greenhouse
    etc -- see JobSeen.posted_at_approx / fetch_ats). Only a definite date is
    solid enough to ELIMINATE a listing outright; an unknown or merely-
    approximate one is downgrade evidence at most (see _listing_age_tag)."""
    if job.get("_posted_at_approx"):
        return None
    return _days_since(job.get("_posted_at"), now)


def listing_over_max_age(job: dict, max_age_days: int | None, now: datetime | None = None) -> bool:
    """True only when the listing's OWN definite posted date clearly exceeds the
    candidate's stated maximum listing age (the Dashboard "Maximum listing age"
    preference; 0/None means the candidate hasn't capped it, so this always
    returns False). Used by engine.py to hard-drop a listing before ever
    spending a gate LLM call on it, when that preference is enforced Hard.

    An unknown or merely-approximate date is NEVER eliminated on this basis --
    only a downgrade (see _listing_age_tag) -- which is why this checks
    _listing_definite_age_days rather than the more lenient effective age the
    tag itself computes."""
    if not max_age_days:
        return False
    days = _listing_definite_age_days(job, now or datetime.utcnow())
    return days is not None and days > max_age_days


def _listing_age_tag(job: dict, now: datetime | None = None,
                     store_age_days: float | None = None,
                     max_age_days: int | None = None, hard: bool = True) -> str:
    """Bracketed age/closing note for a listing block, or "" when nothing is known.

    Silence must stay silent: most of the store has no posting date at all, and an
    absent date is not evidence of an old posting.

    TWO CLOCKS, deliberately never merged into one number:
      * what the BOARD claims ("posted N days ago") -- testimony about when it was
        posted, and the only one that can say a listing is NEW;
      * what WE have observed ("we have been seeing this for N days") -- evidence
        that the ad is still standing, and a lower bound on its age.
    They are different evidence with different strength, so they get different
    words. Averaging them would destroy exactly the distinction the judge needs:
    a fresh claimed date sitting on top of a long observation window is the
    signature of a re-listed old posting, and it is only visible as two numbers.

    `store_age_days` gates the observation clause -- see OBSERVATION_MIN_STORE_DAYS.
    `max_age_days`/`hard` are the candidate's own "Maximum listing age" preference
    (DEFAULT_MAX_LISTING_AGE_DAYS when unset). A DEFINITE date past that limit is
    marked ELIMINATE when `hard` -- defense in depth for rank_gate/the judge, since
    engine.py's gate-stage Python filter (listing_over_max_age) already drops these
    before any LLM ever sees them under normal operation; a merely-approximate or
    observed-only signal, or a Soft preference, only ever gets the STALE downgrade
    wording, never ELIMINATE."""
    now = now or datetime.utcnow()
    # None (unset -- a legacy/standalone caller that never learned about this
    # preference) falls back to the default; an explicit 0 is the candidate's
    # own "No limit" choice and must disable the staleness/ELIMINATE wording
    # entirely, not silently become the default threshold instead.
    if max_age_days is None:
        max_age_days = DEFAULT_MAX_LISTING_AGE_DAYS
    age_limit_disabled = max_age_days <= 0
    bits: List[str] = []

    posted_days = _days_since(job.get("_posted_at"), now)
    approx = bool(job.get("_posted_at_approx"))
    observed_days = _days_since(job.get("_first_seen"), now)
    seen_days = job.get("_seen_days") or 1
    # Only trust our own clock once the store has been running long enough for it
    # to mean anything.
    observable = (store_age_days is not None
                  and store_age_days >= OBSERVATION_MIN_STORE_DAYS
                  and observed_days is not None)

    effective_age = max([d for d in (posted_days, observed_days if observable else None)
                         if d is not None] or [0])
    stale = (not age_limit_disabled) and effective_age >= max_age_days
    # A DEFINITE (non-approximate) date past the candidate's own limit -- the one
    # case solid enough to eliminate, never merely downgrade. See
    # listing_over_max_age, which engine.py uses to drop these before the gate.
    # Same ">=" as `stale` above, so this subsumes every non-approx case that
    # would otherwise read as "STALE" -- the plain-staleness wording below is
    # left for the case `stale` is true ONLY via the observed clock (posted_days
    # itself still under the limit), which must NOT be reported against
    # posted_days (see the "refreshed" clause further down instead).
    definite_over = ((not age_limit_disabled) and posted_days is not None
                      and not approx and posted_days >= max_age_days)

    if posted_days is not None and posted_days >= 0:
        if definite_over:
            # Three severities, not two. The Soft case used to render as
            # "... -- well past the candidate's stated limit -- strong negative",
            # which said the same thing twice AND matched neither of the two
            # severities the judge's system prompt describes (STALE / ELIMINATE),
            # so a listing confirmed past the candidate's own stated maximum was
            # read as ordinary staleness -- worth "one step down IF the grade is
            # borderline". These two keywords are what that prompt's third
            # paragraph keys on, so keep them in sync with it. Deliberately worded
            # with no day count of their own beyond the stated maximum: the
            # severity is what the judge reads, the numbers are context.
            if hard:
                verdict = "ELIMINATE"
            elif posted_days >= max_age_days * 2:
                verdict = ("MORE THAN DOUBLE THE CANDIDATE'S STATED MAXIMUM "
                           "(a preference, not a hard limit)")
            else:
                verdict = ("OVER THE CANDIDATE'S STATED MAXIMUM "
                           "(a preference, not a hard limit)")
            bits.append(f"posted ~{_humanise_days(posted_days)} ago -- past the candidate's stated "
                        f"{max_age_days}-day maximum listing age -- {verdict}")
        elif approx:
            # Greenhouse gives no creation date, only updated_at. Saying "posted"
            # would put a claim in the board's mouth it never made.
            bits.append(f"the board last updated this listing ~{_humanise_days(posted_days)} ago"
                        + (" -- STALE, treat as a negative" if stale else ""))
        else:
            bits.append(f"posted {posted_days} day{'s' if posted_days != 1 else ''} ago")

    if observable and observed_days >= OBSERVED_MENTION_DAYS:
        clause = (f"we have been finding this same listing in our own searches for "
                  f"{observed_days} days (this is when we FIRST found it, not when it "
                  f"was posted -- it may be older, never newer)")
        # The laundering catch: a fresh claimed date on top of a long observation
        # window means the posting date was refreshed, not that the role is new.
        if posted_days is not None and posted_days + 14 < observed_days:
            clause += " -- so the stated posting date appears to have been refreshed"
        if stale and posted_days is None:
            clause += " -- STALE, treat as a negative"
        bits.append(clause)
        density = seen_days / max(1, observed_days)
        if seen_days >= EVERGREEN_SEEN_DAYS and density >= EVERGREEN_SEEN_DENSITY:
            bits.append(f"it has been advertised on {seen_days} separate days, near-continuously "
                        f"-- consistent with a standing/pipeline ad rather than one vacancy")

    expires = _loose_date_to_iso(job.get("_expires_at"))
    if expires:
        try:
            left = (datetime.fromisoformat(expires) - now).days
        except (TypeError, ValueError):
            left = None
        if left is not None and left < 0:
            bits.append("the stated closing date has PASSED")
        elif left is not None and left <= 7:
            bits.append(f"closes in {left} day{'s' if left != 1 else ''}")
    return f"[listing age: {'; '.join(bits)}] " if bits else ""


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


# Elements whose CONTENT is not page text. _strip_html removes the tags but keeps
# what's between them, which is right for an ATS description field and badly
# wrong for a whole document from a modern JS framework.
_NOISE_ELEMENT_RE = re.compile(
    r"(?is)<(script|style|noscript|template|svg)\b[^>]*>.*?</\1\s*>"
)


def _visible_text(raw_html: str) -> str:
    """What a reader would actually see on a WHOLE fetched page, as opposed to
    _strip_html's remit (an HTML fragment from an ATS description field).

    This exists because _strip_html on a full document keeps every byte of
    inlined CSS and JS, and both of the dead-listing gates are calibrated on
    document LENGTH and on the OFFSET of the match. A measured live case (a
    Next.js board page, flexa.careers) whose only real content was "we're really
    sorry but this job is no longer available" came out of _strip_html as
    234,757 chars of @font-face rules and RSC payload, with the closure notice at
    offset 107,967 -- so _looks_like_expired_listing failed the length gate
    outright, and would have failed the head-position gate too. Through here the
    same page is 7,736 chars with the notice at offset 288, which is what those
    gates were designed to read. The listing was graded a pick and shown.

    Only for the verification path, which is the only caller holding raw
    server-returned HTML. Phase 5 goes through crawl4ai, which already extracts
    to markdown and never had this problem."""
    if not raw_html:
        return ""
    return _strip_html(_NOISE_ELEMENT_RE.sub(" ", raw_html))


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
    # SmartRecruiters is the other odd one out, for the opposite reason: its
    # listing endpoint carries no description AT ALL, only posting metadata, so
    # the text costs one extra call per posting (see _fetch_smartrecruiters).
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{token}/postings",
}


# SmartRecruiters' Posting API is public and unauthenticated like the six above,
# but it splits into two endpoints and only the second one carries any text:
#   /v1/companies/{token}/postings            -> id, name, location, releasedDate
#   /v1/companies/{token}/postings/{id}       -> jobAd.sections.{...}.text
# So unlike every other vendor here, one company costs 1 + N HTTP calls rather
# than 1. All three caps below exist for that reason: a large board would otherwise
# fire hundreds of requests from inside a slot of gather_jobs' 12-wide pool,
# i.e. up to 12 companies' worth of fan-out hitting one host at once.
SMARTRECRUITERS_MAX_POSTINGS = int(os.getenv("SMARTRECRUITERS_MAX_POSTINGS", "120"))
SMARTRECRUITERS_DETAIL_WORKERS = int(os.getenv("SMARTRECRUITERS_DETAIL_WORKERS", "4"))
SMARTRECRUITERS_PAGE_SIZE = 100  # the API's own documented maximum
# The postings/workers caps above bound how much WORK one company can generate, but
# not how long that work can take -- every detail call has its own 12s timeout, but
# nothing bounded the total, and a real run measured one board at 219.5s (of a 254.5s
# discovery phase) with nothing degraded except this vendor being slow that day. This
# is a third, wall-clock cap: hit it and _fetch_smartrecruiters returns whatever it has
# rather than continuing to wait -- same "budget it, log it, keep partial results"
# shape as SCRAPE_BUDGET_SECONDS. Deliberately tighter than DISCOVERY_BUDGET_SECONDS
# (the outer gather_jobs backstop) so this fires first, with board-specific logging,
# for the vendor that's actually caused the problem.
SMARTRECRUITERS_BUDGET_SECONDS = float(os.getenv("SMARTRECRUITERS_BUDGET_SECONDS", "45"))

# jobAd.sections in SmartRecruiters' own display order. Emitted with their titles
# for the same reason _lever_text keeps Lever's list headings: the heading is
# real signal, and "Qualifications" in particular is the marker that the
# REQUIREMENTS section starts here -- the half a fit judgement turns on, and the
# half every vendor that splits its description was dropping (see _ats_text).
_SR_SECTION_ORDER = ("companyDescription", "jobDescription",
                     "qualifications", "additionalInformation")


def _smartrecruiters_text(detail: dict) -> str:
    """Detail payload -> one plain-text description, sections in display order."""
    sections = ((detail.get("jobAd") or {}).get("sections") or {})
    if not isinstance(sections, dict):
        return ""
    ordered = list(_SR_SECTION_ORDER)
    # Any section the vendor adds later still lands in the text rather than
    # being silently dropped, which is the failure mode this whole area exists
    # to prevent -- it just lands after the ones we know the order of.
    ordered += [k for k in sections if k not in _SR_SECTION_ORDER]
    parts: List[str] = []
    for key in ordered:
        sec = sections.get(key)
        if not isinstance(sec, dict):
            continue
        text = sec.get("text") or ""
        if not text.strip():
            continue
        title = (sec.get("title") or "").strip()
        parts.append(f"{title}\n{text}" if title else text)
    return _ats_text(*parts)


def _fetch_smartrecruiters(base_url: str, token: str, company: str = "") -> List[Dict]:
    """Page the postings list, then fetch each posting's detail for its text.

    A posting whose detail call fails is DROPPED rather than emitted text-less.
    That looks harsh but it is the only safe option: `smartrecruiters` is an ATS
    key, and engine._has_judgeable_text treats any ATS-keyed row as already
    carrying enough text to judge -- so a text-less row here would skip phase 5,
    collect RICH_TEXT_SELECTION_BONUS, and reach the expensive judge on its job
    title alone. That is exactly the inversion documented for un-enriched Adzuna
    rows, and there is no reason to reintroduce it on a new source.

    Bounded overall by SMARTRECRUITERS_BUDGET_SECONDS on top of the existing
    postings/workers caps: those bound how much WORK one company can generate,
    this bounds how long it's allowed to take. A posting whose detail fetch
    doesn't finish within budget is dropped exactly like one that answered with
    no text -- see the two separate log lines below, kept distinct because they
    mean different things (a vendor data-quality gap vs. a latency cutoff).
    """
    deadline = time.monotonic() + SMARTRECRUITERS_BUDGET_SECONDS
    postings: List[Dict] = []
    offset = 0
    while len(postings) < SMARTRECRUITERS_MAX_POSTINGS:
        if time.monotonic() >= deadline:
            emit(f"   [!] ATS smartrecruiters/{token}: budget "
                 f"({SMARTRECRUITERS_BUDGET_SECONDS:.0f}s) hit during listing "
                 f"pagination -- proceeding with {len(postings)} posting(s) found so far")
            break
        try:
            r = requests.get(base_url, timeout=12,
                             params={"limit": SMARTRECRUITERS_PAGE_SIZE, "offset": offset})
            data = r.json()
        except Exception as e:
            emit(f"   [!] ATS smartrecruiters/{token} list error: {e}")
            break
        content = data.get("content") or []
        if not content:
            break
        postings.extend(content)
        offset += len(content)
        if offset >= int(data.get("totalFound") or 0):
            break
    postings = postings[:SMARTRECRUITERS_MAX_POSTINGS]
    if not postings:
        return []

    def _detail(p: dict) -> Dict | None:
        pid = p.get("id")
        if not pid:
            return None
        try:
            d = requests.get(f"{base_url}/{pid}", timeout=12).json()
        except Exception:
            return None
        text = _smartrecruiters_text(d)
        if not text:
            return None  # see the docstring -- never emit a text-less ATS row
        loc = p.get("location") or d.get("location") or {}
        place = loc.get("fullLocation") or ", ".join(
            x for x in [loc.get("city"), loc.get("country")] if x)
        released = p.get("releasedDate") or d.get("releasedDate")
        return {"board": f"smartrecruiters:{token}",
                "title": p.get("name") or d.get("name") or "",
                # The posting's own company name wins: SmartRecruiters is the one
                # vendor whose payload carries it, and it is the most specific of
                # the three (a multi-brand group posts under the hiring brand).
                "company": ((p.get("company") or {}).get("name")
                            or (company or "").strip() or token),
                "url": d.get("postingUrl") or d.get("applyUrl")
                       or f"https://jobs.smartrecruiters.com/{token}/{pid}",
                "location": place,
                "snippet": text,
                "updated_at": released,
                "posted_at": released}

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        emit(f"   [!] ATS smartrecruiters/{token}: 0/{len(postings)} posting(s) fetched "
             f"(budget exhausted before detail fetch could start)")
        return []

    # submit()+wait(timeout=) rather than ex.map(): map's own `timeout` only bounds
    # result RETRIEVAL, not execution, and list()-ing it forces full evaluation
    # regardless -- it cannot actually be cut off at a deadline the way this needs.
    ex = ThreadPoolExecutor(max_workers=max(1, SMARTRECRUITERS_DETAIL_WORKERS))
    try:
        futs = [ex.submit(_detail, p) for p in postings]
        done, not_done = wait(futs, timeout=remaining)
        out = [r for r in (f.result() for f in done) if r]
    finally:
        # Anything still queued is dropped immediately; anything already mid-flight
        # keeps running (bounded by its own 12s timeout) with nothing left waiting
        # on it -- not a leak, since a fresh executor is created per call.
        ex.shutdown(wait=False, cancel_futures=True)

    text_dropped = len(done) - len(out)
    if text_dropped:
        emit(f"   [!] ATS smartrecruiters/{token}: {text_dropped}/{len(done)} posting(s) "
             f"dropped (no description returned by the detail endpoint)")
    if not_done:
        emit(f"   [!] ATS smartrecruiters/{token}: budget "
             f"({SMARTRECRUITERS_BUDGET_SECONDS:.0f}s) hit with {len(not_done)}/"
             f"{len(postings)} detail fetch(es) still in flight -- returning "
             f"{len(out)} posting(s) fetched in time")
    return out


# How a vendor's board token appears in a URL found in the wild. Path-segment
# vendors carry it after the domain; subdomain vendors (recruitee, personio)
# carry it before. SHARED source of truth: harvest_ats_tokens' `site:` search
# and services/direct_employer.py's careers-page crawl both read this, so the
# two can't drift into disagreeing about what a valid token looks like.
#
# A list per vendor because several publish their board under more than one
# host: Greenhouse alone has the legacy boards.greenhouse.io/{token}, the newer
# job-boards.greenhouse.io/{token}, and an EMBED form that carries the token in
# a query parameter instead of the path. That last one is why a single loose
# `greenhouse\.io/([A-Za-z0-9_-]+)` is not good enough -- against an embed URL
# it captures the literal path segment "embed" as the company's token.
ATS_TOKEN_PATTERNS: Dict[str, List] = {
    "greenhouse": [
        re.compile(r"greenhouse\.io/embed/job_board(?:/js)?\?(?:[^\"'\s]*&)?for=([A-Za-z0-9_-]+)"),
        re.compile(r"(?:job-)?boards\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ],
    "lever":      [re.compile(r"jobs\.lever\.co/([A-Za-z0-9_-]+)")],
    "ashby":      [re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)")],
    "workable":   [re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
                   re.compile(r"https?://([A-Za-z0-9_-]+)\.workable\.com")],
    "recruitee":  [re.compile(r"https?://([A-Za-z0-9_-]+)\.recruitee\.com")],
    "personio":   [re.compile(r"https?://([A-Za-z0-9_-]+)\.jobs\.personio\.")],
    "smartrecruiters": [
        re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
        re.compile(r"api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)"),
    ],
}

# Path segments that sit where a token does but name a vendor route, not a
# company. Without this an embed/widget/api URL yields a "company" called
# "embed" that then fails live validation and wastes a probe.
_ATS_TOKEN_STOPWORDS = {
    "embed", "api", "v1", "widget", "accounts", "job_board", "jobs", "job",
    "www", "boards", "careers", "career", "search", "postings", "company",
    "companies", "static", "assets", "js", "css", "images", "img",
    # "apply" is the host in Workable's own apply.workable.com/{token} form, so
    # the subdomain pattern below it matches the ROUTE rather than a company.
    "apply", "help", "support", "blog", "app", "account", "login",
}


def ats_tokens_in(text: str) -> List[tuple]:
    """Every (vendor, token) pair an ATS board URL in `text` points at.

    Deliberately extraction-only: it says which boards a page LINKS to, never
    whether they are live. Callers pass each pair through validate_ats_token
    before storing it, the same guard harvested tokens already go through."""
    out: List[tuple] = []
    seen: set = set()
    for vendor, patterns in ATS_TOKEN_PATTERNS.items():
        for pattern in patterns:
            for token in pattern.findall(text or ""):
                token = token.strip()
                if not token or token.lower() in _ATS_TOKEN_STOPWORDS:
                    continue
                key = (vendor, token)
                if key not in seen:
                    seen.add(key)
                    out.append(key)
    return out


def _fetch_personio(url: str, token: str, company: str = "") -> List[Dict]:
    """Personio exposes an XML positions feed rather than JSON, and gives no
    per-job URL, so the apply URL is constructed from the position id."""
    name = (company or "").strip() or token
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
        created = pos.findtext("createdAt") or pos.findtext("createDate")
        out.append({"board": f"personio:{token}", "title": pos.findtext("name") or "",
                    "company": name,
                    "url": f"{base}/job/{job_id}" if job_id else base,
                    "location": pos.findtext("office") or "",
                    "snippet": _strip_html(desc)[:3000],
                    "updated_at": created,
                    # Personio's createdAt is a genuine creation date (like Lever's,
                    # unlike Greenhouse's aliased updated_at) -- it was being read into
                    # updated_at only, so posted_at/the age tag stayed null for every
                    # Personio-sourced job even though the feed always carries it.
                    "posted_at": created})
    return out


# An ATS posting is the one discovery-time source that arrives text-COMPLETE, so
# nothing downstream ever re-fetches it: _needs_full_scrape skips ATS rows and
# SNIPPET_SUFFICIENT_CHARS waves through anything this long. That makes losing part
# of the description here unrecoverable at every later stage, which is exactly what
# was happening -- several vendors split a posting across SEVERAL fields and we were
# storing only the first one. Lever returns the opening blurb in `descriptionPlain`
# and puts "What You'll Do" / "What You'll Bring" in a separate `lists` array;
# Recruitee splits `requirements` out of `description`; Workable documents
# `requirements`/`benefits` alongside it. In every case the omitted part is the
# REQUIREMENTS section, i.e. the half a fit judgement actually turns on, while the
# part we kept is the company marketing blurb. Measured on the live Lever posting
# that prompted this (computercare Data Analyst): 2,064 chars stored, 4,019 dropped,
# including the "2-4+ years of experience" bar the final judge then graded a
# "strong" entry-level fit without ever seeing.
ATS_SNIPPET_CHARS = 8000   # matches FINAL_EVAL_JOB_TEXT_CHARS -- no point storing
                           # more than the judge can ever read


def _ats_text(*parts: str) -> str:
    """Join a posting's sections into one plain-text description, dropping empties.
    Sections are emitted in the vendor's own order so the requirements land after
    the summary, the way the JD reads."""
    return "\n\n".join(p for p in (_strip_html(p or "") for p in parts) if p)[:ATS_SNIPPET_CHARS]


def _lever_text(j: dict) -> str:
    """Lever posting -> full description. `lists` is an array of
    {text: <heading>, content: <html <li> blocks>}; the heading carries real signal
    ("What You'll Bring") so it's kept as a line of its own."""
    sections: List[str] = [j.get("descriptionPlain") or j.get("description") or ""]
    for lst in j.get("lists") or []:
        if not isinstance(lst, dict):
            continue
        sections.append(f"{(lst.get('text') or '').strip()}\n{lst.get('content') or ''}")
    sections.append(j.get("additionalPlain") or j.get("additional") or "")
    return _ats_text(*sections)


def fetch_ats(vendor: str, token: str, company: str = "") -> List[Dict]:
    """One ATS board -> its open postings.

    `company` is the employer's real name from the CompanyATS registry. Without
    it these rows carry the vendor TOKEN in their company field ("tiger-analytics"
    rather than "Tiger Analytics"), which is both what the card renders and what
    every company-keyed consumer sees -- services.sponsors could match only 2-4%
    of ATS rows against the licensed-sponsor register purely because of it, while
    the same employers matched at 9.6% via CompanyATS.company. Optional and
    defaulted so the standalone/validation callers (validate_ats_token,
    seed_ats.py, direct_employer's probe) need not supply one; the token remains
    the fallback."""
    tmpl = ATS_FEEDS.get(vendor)
    if not tmpl:
        return []
    url = tmpl.format(token=token)
    name = (company or "").strip() or token

    if vendor == "personio":
        return _fetch_personio(url, token, name)
    if vendor == "smartrecruiters":
        return _fetch_smartrecruiters(url, token, name)

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
                        "company": name, "url": j.get("absolute_url", ""),
                        "location": (j.get("location") or {}).get("name", ""),
                        "snippet": _ats_text(j.get("content", "")),
                        "updated_at": j.get("updated_at"),
                        # Greenhouse's board API exposes no creation date, only
                        # updated_at -- a MODIFICATION timestamp. Every other
                        # vendor here supplies a genuine publication date
                        # (lever createdAt, workable published_on, recruitee
                        # published_at, ashby publishedAt), so this is the only
                        # aliased one. Kept because an untouched-for-months req
                        # is itself a ghost signal, but flagged so the age tag
                        # never renders it as a "posted" date the source never
                        # claimed. See JobSeen.posted_at_approx.
                        "posted_at": j.get("updated_at"),
                        "posted_at_approx": True})
        elif vendor == "lever":
            out.append({"board": f"lever:{token}", "title": j.get("text", ""),
                        "company": name, "url": j.get("hostedUrl", ""),
                        "location": (j.get("categories") or {}).get("location", ""),
                        "snippet": _lever_text(j),
                        # createdAt is epoch MILLISECONDS, not ISO -- _parse_iso
                        # silently returned None for every Lever row until this
                        # was normalised (see _epoch_ms_to_iso).
                        "updated_at": _epoch_ms_to_iso(j.get("createdAt")),
                        "posted_at": _epoch_ms_to_iso(j.get("createdAt"))})
        elif vendor == "workable":
            loc = j.get("location") or {}
            out.append({"board": f"workable:{token}", "title": j.get("title", ""),
                        "company": name,
                        "url": j.get("url") or j.get("application_url", ""),
                        "location": loc.get("location_str") or ", ".join(
                            x for x in [loc.get("city"), loc.get("country")] if x),
                        "snippet": _ats_text(j.get("description", ""),
                                             j.get("requirements", ""),
                                             j.get("benefits", "")),
                        "updated_at": j.get("published_on") or j.get("created_at"),
                        "posted_at": j.get("published_on") or j.get("created_at")})
        elif vendor == "recruitee":
            out.append({"board": f"recruitee:{token}", "title": j.get("title", ""),
                        "company": name,
                        "url": j.get("careers_url") or j.get("careers_apply_url", ""),
                        "location": j.get("location") or ", ".join(
                            x for x in [j.get("city"), j.get("country")] if x),
                        "snippet": _ats_text(j.get("description", ""),
                                             j.get("requirements", "")),
                        "updated_at": j.get("published_at"),
                        "posted_at": j.get("published_at")})
        else:  # ashby
            out.append({"board": f"ashby:{token}", "title": j.get("title", ""),
                        "company": name, "url": j.get("jobUrl", ""),
                        "location": j.get("location", ""),
                        # Ashby's descriptionPlain IS the whole posting (measured
                        # 5.5k chars on a live board) -- no split fields to merge.
                        "snippet": _ats_text(j.get("descriptionPlain", "")),
                        "updated_at": j.get("publishedAt"),
                        "posted_at": j.get("publishedAt")})
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
    # Careerjet is free and worldwide, but it's an AGGREGATOR (blank-company
    # reposts, no structured salary) -- valuable only as a coverage backstop for
    # the ~230 countries Adzuna (19) and Reed (UK only) can't serve. When the
    # profile's country IS served by Adzuna, those clean structured feeds already
    # cover it and careerjet just crowds the (unbalanced) pool with noise -- a
    # measured UK run collapsed onto careerjet when Adzuna flaked. So join it to
    # the always-on tier ONLY for countries Adzuna can't serve; elsewhere it stays
    # available via the per-source toggle. Reed adds nothing to this test: it's
    # gb-only and gb is Adzuna-supported. USAJobs is the US federal government's
    # own hiring site -- free, and (like Reed) self-gates to nothing for a
    # non-US profile, so it costs nothing to include unconditionally; it's the
    # only source here that reaches federal postings at all.
    always = [AdzunaSource(), ReedSource(), GoogleJobsSource(), USAJobsSource()]
    if (profile.get("adzuna_country_code") or "gb").strip().lower() not in _ADZUNA_SUPPORTED:
        always.append(CareerjetSource())
    # Remaining credit-heavy / overlapping sources: one per run on rotation keeps
    # RapidAPI spend bounded while still adding breadth.
    rotation = [JSearchSource(), RemotiveSource()]

    cur = _load_cursor(profile)
    # Slide a window over the term list so successive runs explore different
    # terms, and add one rotating broad source -- including on the first run.
    # cur=0 naturally selects terms[:TERMS_PER_RUN] and rotation[0] (JSearch),
    # so no special-casing is needed there; this used to hard-return `always`
    # only on first_run, which meant a niche/non-tech profile (whose ATS batch
    # is mostly irrelevant, see the always-on comment above) got zero benefit
    # from JSearch/Remotive on the one run that most needs the extra breadth.
    # Wrap the window with modulo indexing rather than a plain slice -- a slice
    # near the tail of `terms` silently returns fewer than TERMS_PER_RUN terms
    # (the `or terms[:TERMS_PER_RUN]` fallback below only fires when the slice
    # is fully empty, not merely short), so a run could under-query without
    # any signal.
    n = len(terms)
    if n <= TERMS_PER_RUN:
        profile["search_terms_batch"] = list(terms)
    else:
        start = (cur * TERMS_PER_RUN) % n
        profile["search_terms_batch"] = [terms[(start + i) % n] for i in range(TERMS_PER_RUN)]
    profile["search_terms_batch"] += _sponsor_scoped_terms(
        profile, profile["search_terms_batch"], cur)
    picked = always + [rotation[cur % len(rotation)]]
    _save_cursor(profile, cur + 1)
    return picked


SPONSOR_TERMS_PER_RUN = int(os.getenv("SPONSOR_TERMS_PER_RUN", "2"))


def _sponsor_scoped_terms(profile: Dict, batch: List[str], cur: int) -> List[str]:
    """"<role> <employer>" terms aimed at named licensed sponsors.

    Only when the sponsor filter is on. The generic term query returns whoever
    the board ranks highest for "data analyst", which is overwhelmingly agencies
    and non-sponsors -- and a ~90% filter then discards them. Naming an employer
    the candidate could actually be sponsored by asks the board a question whose
    answers survive the filter.

    Which employers: ones already proven to surface for THIS profile -- a company
    whose listings have reached the gate before is one whose roles match this
    candidate, so this deepens a seam rather than guessing at a new one. Read-side
    join over columns that already exist (same posture as direct_employer's
    _charity_board_yield), so it costs the search path nothing but one indexed
    query. Fails soft to no extra terms: this is an enhancement, and a profile
    with no history yet simply gets the normal batch.

    Rotates on the same cursor as the term window, so successive runs work
    through different employers rather than re-asking about the same two."""
    if not profile.get("visa_sponsor_only") or not batch or SPONSOR_TERMS_PER_RUN <= 0:
        return []
    try:
        from app.services import sponsors as _sp
    except Exception:
        return []

    profile_id = profile.get("profile_id")
    if profile_id is None:
        return []
    try:
        conn = _ats_db()
        try:
            rows = conn.execute(
                "SELECT DISTINCT company FROM jobs_seen "
                "WHERE profile_id = ? AND company IS NOT NULL AND company != '' "
                "AND state IN ('enriched', 'shown') AND dead_reason IS NULL",
                (profile_id,)).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    # Deduped by NORMALISED key, not raw string: a store routinely holds the same
    # employer under several spellings ("Davies" and "Davies Group"), and since
    # the picks below are consecutive in sorted order those variants land
    # adjacent -- spending both of a run's two slots re-asking about one company.
    companies = sorted({(r[0] or "").strip() for r in rows if (r[0] or "").strip()})
    seen_keys: set = set()
    sponsors_seen = []
    for c in companies:
        if not _sp.is_sponsor(c):
            continue
        k = _sp.key(_sp.normalise(c))
        if k in seen_keys:
            continue
        seen_keys.add(k)
        sponsors_seen.append(c)
    if not sponsors_seen:
        return []

    n = len(sponsors_seen)
    start = (cur * SPONSOR_TERMS_PER_RUN) % n
    picked = [sponsors_seen[(start + i) % n]
              for i in range(min(SPONSOR_TERMS_PER_RUN, n))]
    # Pair each with a different role term so two extra queries don't both probe
    # the same role shape.
    out = [f"{batch[i % len(batch)]} {company}" for i, company in enumerate(picked)]
    emit(f"   [sponsor] added {len(out)} employer-scoped term(s): {out}")
    return out


# Word-level match makes the batch profile-aware: harvested rows carry the phrase
# that found them, so a profile whose terms/sectors share a word gets those
# companies first. Drop role-shape words that don't carry sector signal.
_ATS_MATCH_STOP = {"the", "and", "for", "with", "junior", "senior", "lead", "mid",
                   "level", "manager", "assistant", "coordinator", "officer",
                   "associate", "intern", "graduate", "specialist", "role", "jobs"}


def _words_of(parts) -> set:
    words = set()
    for p in parts or []:
        for w in re.findall(r"[a-z0-9]+", str(p).lower()):
            if len(w) > 2 and w not in _ATS_MATCH_STOP:
                words.add(w)
    return words


def _profile_match_words(profile: Dict) -> set:
    return _words_of(list(profile.get("search_terms") or [])
                     + list(profile.get("sectors") or []))


def _ats_keyword_matches(keyword, words: set) -> bool:
    if not keyword or keyword == "curated" or not words:
        return False
    kw = {w for w in re.findall(r"[a-z0-9]+", str(keyword).lower()) if len(w) > 2}
    return bool(kw & words)


def select_ats_batch_for_run(profile: Dict) -> List[tuple]:
    """Return (company, vendor, token) rows, profile-aware. company_ats is a
    single store shared by all profiles, so favour companies whose harvest
    keyword matches this profile's sector, then fill the batch from the rest by
    rotation (so the shared, multi-sector store doesn't dilute any one profile).

    THREE tiers, not two, and the split that was added matters. A keyword match
    used to be one boolean over search terms and sectors merged together, which
    meant a board tagged "data analyst" and a board tagged "charity nonprofit"
    were equally "preferred" for a charity-sector data analyst -- and since the
    tier is then rotated in insertion order, the sector-specific boards (the
    newest rows, appended last) sat at the BACK. Measured on the live store: a
    freshly-crawled charity board landed at index 283 of a 285-row preferred
    list, i.e. ~7 runs of rotation before the batch would ever reach it, ranked
    behind 283 boards that matched only on the generic word "data".

    A SECTOR word is the far stronger signal of "this company is in the
    candidate's field" -- a role-shape word like "data" or "analyst" matches
    employers in every industry -- so sector matches now get their own tier
    ahead of term-only matches. Each tier still rotates independently, so
    nothing is starved; only the order in which the 40 slots are claimed
    changes.

    A FOURTH tier goes in front of all three, but only for a profile that has
    the licensed-sponsor filter on: boards whose company is on the register.
    Every other board's postings will be dropped outright by
    engine._filter_by_sponsor, so spending the batch's 40 slots on them is
    spending the whole ATS tier on nothing. Note the ceiling this runs into --
    seed_ats.py stores the vendor token AS the company for its curated rows, so
    a board only lands in this tier when its token happens to normalise to the
    registered name; it grows with the direct-employer crawl, which does record
    real names. Empty tier is harmless: the other three fill the batch exactly
    as before."""
    rows = load_company_ats()  # (company, vendor, token, keyword)
    if not rows:
        return []
    size = 40
    sector_words = _words_of(profile.get("sectors"))
    term_words = _words_of(profile.get("search_terms"))

    is_sponsor = None
    if profile.get("visa_sponsor_only"):
        try:
            from app.services.sponsors import is_sponsor as _is_sponsor
            is_sponsor = _is_sponsor
        except Exception:
            is_sponsor = None   # standalone path without the backend importable

    by_sponsor, by_sector, by_term, others = [], [], [], []
    for r in rows:
        if is_sponsor is not None and is_sponsor(r[0] or ""):
            by_sponsor.append(r)
        elif _ats_keyword_matches(r[3], sector_words):
            by_sector.append(r)
        elif _ats_keyword_matches(r[3], term_words):
            by_term.append(r)
        else:
            others.append(r)

    cur = _load_cursor(profile)

    def _rotate(lst: List[tuple]) -> List[tuple]:
        # Wrap-around slice so large sets still cycle across runs (first run: cur=0).
        if not lst:
            return []
        start = (cur * size) % len(lst)
        return (lst[start:] + lst[:start])[:size]

    # Licensed sponsors first (when the filter is on), then sector matches, then
    # term matches, then fill with rotated others.
    ordered = (_rotate(by_sponsor) + _rotate(by_sector)
               + _rotate(by_term) + _rotate(others))
    return [(c, v, t) for (c, v, t, _kw) in ordered[:size]]


# ── Parallel discovery ───────────────────────────────────────────────────────

def gather_jobs(profile: Dict) -> List[Dict]:
    """Orchestrates job harvesting: rotated source tier(s) + a batch of ATS
    feeds, all fetched concurrently -- the rotation tier runs from the first
    run onward (see select_sources_for_run)."""
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
    # The registry's `company` rides along rather than being discarded: it is the
    # employer's real name, and without it every row this vendor emits carries
    # the opaque token instead (see fetch_ats).
    tasks += [("ats", (vendor, token, company))
              for (company, vendor, token) in select_ats_batch_for_run(profile)
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
        vendor, token, company = payload
        return kind, vendor, (fetch_ats(vendor, token, company) or []), \
            time.monotonic() - started

    def _task_label(t: tuple) -> str:
        """Human-readable name for a discovery task, for the straggler log line."""
        kind, payload = t
        if kind == "src":
            return payload.name
        if kind == "term":
            src, term = payload
            return f"{src.name} ('{term}')"
        vendor, token, _company = payload
        return f"ats:{vendor}/{token}"

    all_jobs: List[Dict] = []
    term_counts: Counter = Counter()
    ats_counts: Counter = Counter()
    slowest: Dict[str, float] = {}
    # Managed manually rather than `with ThreadPoolExecutor(...) as ex:` -- that
    # context manager's __exit__ calls shutdown(wait=True), which would block on
    # any straggler anyway and undo the point of the deadline below.
    ex = ThreadPoolExecutor(max_workers=DISCOVERY_MAX_WORKERS)
    try:
        fut_to_task = {ex.submit(run_task, t): t for t in tasks}
        done, not_done = wait(list(fut_to_task), timeout=DISCOVERY_BUDGET_SECONDS)

        for fut in done:
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

        if not_done:
            # A task this slow has already blown well past what any individual
            # source's own retry/backoff logic allows for -- this is the backstop
            # for a task that's hung outright (network stall, a misbehaving board),
            # not the expected path. See SMARTRECRUITERS_BUDGET_SECONDS for the one
            # vendor that's actually done this; it has its own tighter budget and
            # fires first, so a straggler reaching this backstop is rarer still.
            stragglers = [_task_label(fut_to_task[f]) for f in not_done]
            shown = stragglers[:10]
            more = f" (+{len(stragglers) - 10} more)" if len(stragglers) > 10 else ""
            emit(f"   [!] discovery budget ({DISCOVERY_BUDGET_SECONDS:.0f}s) hit with "
                 f"{len(not_done)}/{len(tasks)} task(s) still running -- results "
                 f"discarded for: {shown}{more}")
    finally:
        # Queued-but-not-started tasks are cancelled outright; anything already
        # running keeps going (bounded by its own timeout) with nothing waiting on
        # its result -- not a leak, a fresh executor is created on every call.
        ex.shutdown(wait=False, cancel_futures=True)

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
    # WAL keeps the several concurrent gate-cache writers from ever seeing a
    # reader-vs-writer "database is locked" (which busy_timeout can't wait out);
    # with WAL only writer-vs-writer serialises, and the 30s timeout covers that.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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
    # timeout=30 (busy_timeout) + WAL match the backend engine's SQLite settings
    # (backend/app/database.py): this raw connection *writes* to jobmatch.db
    # (save_company_ats, during the end-of-run ATS harvest) concurrently with the
    # backend search thread's own commits. WAL is a persistent DB-level property
    # so it's normally already on, but setting it here keeps the standalone
    # seed_ats.py path consistent; the default 5s busy-timeout was thin for the
    # concurrent-writer case and is bumped to 30s to match.
    conn = sqlite3.connect(_ats_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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


def harvest_ats_tokens(sector_keywords: List[str], location: str = "") -> List[tuple]:
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
    instead of repeating the same rejection for every remaining combo.

    `location` (a plain place name, e.g. "Dubai" or "United Arab Emirates" --
    see harvest.py's caller for how it's derived from the candidate's own
    location attribute) is an optional SOFT bias appended to the query text,
    not a hard filter: a live test harvesting for a Dubai-based candidate with
    no location term found 78 companies, every one a US/European tech company,
    because the search has nothing else to prefer a company with an actual
    regional presence. Deliberately not a hard geographic filter at validation
    time -- that would need to inspect every found company's live job locations
    and would risk dropping a genuinely relevant company that simply has no
    open req in that country/region right now (e.g. a remote-friendly
    employer); the real per-listing location enforcement already happens
    downstream in the main discovery pipeline (_filter_by_country, the cheap
    gate's work-arrangement axis, the final judge's LOCATION disqualifier) once
    an actual job from this company is discovered, so this only needs to bias
    which companies get found in the first place, not gate them here."""
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
        "smartrecruiters": "jobs.smartrecruiters.com",
    }
    # Token extraction is shared with the direct-employer crawl -- see
    # ATS_TOKEN_PATTERNS / ats_tokens_in, which own the per-vendor regexes.
    token_res = {v: ATS_TOKEN_PATTERNS[v] for v in site_domains}

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
        token_pats = token_res[vendor]
        if SERPER_DEV_API_KEY:
            base = f"site:{domain} {keyword}" if SERPER_SITE_OPERATOR_OK else f"{keyword} {domain}"
            query = f"{base} {location}" if location else base
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
            query = f"site:{domain} {keyword} {location}" if location else f"site:{domain} {keyword}"
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
            for pattern in token_pats:
                m = pattern.search(link or "")
                if m and m.group(1).lower() not in _ATS_TOKEN_STOPWORDS:
                    # Tag the row with every phrase that found it so the batch
                    # selector can favour it for profiles in this sector.
                    found.setdefault((vendor, m.group(1)), set()).add(keyword)
                    break

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


# The board's own id for a listing, parsed back out of the URL. No fetcher
# stores one -- every Source class discards the id its API hands over -- and
# rather than widen seven return shapes for a value the URL already encodes,
# this reads it back the way reed_job_id already does for Reed.
#
# WHAT IT IS FOR: observation continuity, not deduplication. engine.identity_hash
# is sha1(_canonical_url(url)), so an aggregator appending a tracking parameter,
# or Reed changing a slug, mints a NEW identity and silently restarts that
# listing's first_seen at zero. That quietly corrupts the one measurement the
# whole ghost-listing feature depends on, and nothing else detects it.
#
# READ THE VENDOR BEFORE TRUSTING IT AS A VACANCY KEY. The two halves mean
# genuinely different things:
#   * reed / adzuna ids identify a LISTING. A repost gets a fresh number, and
#     re-syndication mints new ones, so equality proves continuity but
#     inequality proves nothing.
#   * greenhouse gh_jid identifies the EMPLOYER'S OWN REQUISITION, and lever /
#     ashby / workable / personio ids identify a posting that persists for its
#     life. A gh_jid holding steady across a long observation window is the
#     closest thing in this codebase to ground truth that one vacancy is still
#     the same vacancy -- and one disappearing while a new one appears for the
#     same title is a genuine close-and-reopen.
# recruitee (slug only), careerjet (opaque jobviewtrack.com/v2/<blob>) and
# google_jobs (arbitrary destination, often a category page) expose no usable
# id at all and always return None. Verified against live URLs in the store.
_BOARD_REF_PATTERNS: List[tuple] = [
    ("reed",       re.compile(r"reed\.co\.uk/jobs/[^/]+/(\d+)", re.I)),
    # Both Adzuna shapes carry the same ad id: the API's /jobs/land/ad/ tracking
    # interstitial and the site's own /jobs/details/ page (what
    # fetch_adzuna_details reads). Matching both is what lets a row enriched via
    # the detail page keep the observation window its land-URL twin started.
    ("adzuna",     re.compile(r"adzuna\.[a-z.]+/jobs/(?:land/ad|details)/(\d+)", re.I)),
    ("greenhouse", re.compile(r"[?&]gh_jid=(\d+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards\.greenhouse\.io/[A-Za-z0-9_-]+/jobs/(\d+)", re.I)),
    ("lever",      re.compile(r"jobs\.lever\.co/[A-Za-z0-9_-]+/([0-9a-f-]{16,})", re.I)),
    ("ashby",      re.compile(r"jobs\.ashbyhq\.com/[A-Za-z0-9_-]+/([0-9a-f-]{16,})", re.I)),
    ("workable",   re.compile(r"apply\.workable\.com/(?:[A-Za-z0-9_-]+/)?j/([A-Z0-9]{6,})", re.I)),
    ("personio",   re.compile(r"\.jobs\.personio\.[a-z.]+/job/(\d+)", re.I)),
    ("remotive",   re.compile(r"remotive\.(?:io|com)/remote-jobs/[^/]+/[^/]*?-(\d+)/?$", re.I)),
]


def _board_ref(url: str) -> str | None:
    """"<vendor>:<id>" for a listing URL, or None when the board exposes no id.

    Keyed off the URL alone rather than the source name: an aggregator row
    (jsearch, google_jobs) often points AT a board we can read, and that
    destination id is exactly the stable key its own feed would have given us."""
    if not url:
        return None
    for vendor, pattern in _BOARD_REF_PATTERNS:
        m = pattern.search(url)
        if m:
            return f"{vendor}:{m.group(1)}"
    return None


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


# ── Prompt-cache accounting ───────────────────────────────────────────────────
# Every prompt in this pipeline is a long FIXED prefix followed by a short
# variable payload (screen/rank: the whole profile block, then the listings;
# judge: the entire system prompt, then the CV + jobs), which is exactly the
# shape OpenAI's automatic prompt caching discounts. Nothing here used to read
# `usage` back, so whether that discount was actually landing was unknowable --
# and the fixed prefixes are big enough (screen ~4.5k, rank ~2.5k, judge ~12k
# tokens) that the answer is worth roughly 110k tokens a run. These counters make
# it observable per stage; engine.py resets them per run and reports the rollup
# into funnel_counts.
_LLM_USAGE_LOCK = threading.Lock()
_LLM_USAGE: dict[str, dict[str, int]] = {}


def reset_llm_usage() -> None:
    with _LLM_USAGE_LOCK:
        _LLM_USAGE.clear()


def llm_usage_snapshot() -> dict[str, dict[str, int]]:
    with _LLM_USAGE_LOCK:
        return {k: dict(v) for k, v in _LLM_USAGE.items()}


def _record_llm_usage(stage: str, model: str, usage, finish_reason: str = "") -> None:
    """Accumulate one call's token usage under `stage`. Tolerates a missing or
    partial `usage` object (some error paths and non-OpenAI-compatible proxies
    omit it) rather than letting accounting break a real pipeline stage.

    `length_capped` counts responses the model did not finish writing -- it ran
    into an output ceiling instead of stopping on its own. That number is the
    only thing that distinguishes "the model chose to write less" from "we cut
    it off", and the judge's shrinking requirements checklist is exactly the
    symptom that could be either (see FINAL_EVAL_MAX_OUTPUT_TOKENS). It is
    recorded for EVERY stage, not just the judge, since a truncated JSON reply
    parses as a failure and disappears down a fail-open path anywhere it
    happens."""
    if not stage or usage is None:
        return
    try:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        details = getattr(usage, "prompt_tokens_details", None)
        cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
    except Exception:
        return
    with _LLM_USAGE_LOCK:
        row = _LLM_USAGE.setdefault(
            stage, {"calls": 0, "prompt_tokens": 0, "cached_tokens": 0,
                    "completion_tokens": 0, "length_capped": 0}
        )
        row["calls"] += 1
        row["prompt_tokens"] += prompt_tokens
        row["cached_tokens"] += cached
        row["completion_tokens"] += completion
        row["length_capped"] += 1 if finish_reason == "length" else 0
        row["model"] = model


def emit_llm_usage_summary() -> dict[str, dict[str, int]]:
    """One console line per stage: prompt tokens, how many of them were served
    from the prompt cache, and the resulting hit rate. A hit rate near zero on a
    stage with a multi-thousand-token fixed prefix means the cache is not being
    reused and the prefix is being paid for in full on every call -- which is the
    specific failure `prompt_cache_key` below exists to prevent."""
    snap = llm_usage_snapshot()
    for stage, row in sorted(snap.items()):
        prompt_tokens = row.get("prompt_tokens", 0)
        cached = row.get("cached_tokens", 0)
        pct = (100.0 * cached / prompt_tokens) if prompt_tokens else 0.0
        emit(f"[tokens] {stage:<8} {row.get('model','?')}: {row.get('calls',0)} calls, "
             f"{prompt_tokens} prompt ({cached} cached, {pct:.0f}%), "
             f"{row.get('completion_tokens',0)} completion")
    return snap


def llm(prompt: str, system: str = "", model: str = CHEAP_MODEL,
        require_json: bool = False, temperature: float = 0.2,
        stage: str = "", cache_key: str = "", cache_retention: str = "",
        max_output_tokens: int = 0) -> str:
    """`cache_key` is OpenAI's `prompt_cache_key` -- a ROUTING hint only. Requests
    sharing one are steered to the same cache, which is what this pipeline needs:
    it fires its calls concurrently (4-wide gate batches, all clusters at once,
    concurrent judge chunks), and concurrent requests otherwise spread across
    machines and each miss a prefix the others just wrote. It can never serve the
    wrong cache -- the API still requires an exact prefix match, so a stale or
    coarse key costs at most a miss.

    `cache_retention="24h"` extends a prefix's lifetime past the default few
    minutes of inactivity. Worth it only for a prefix that is identical across
    RUNS, not merely within one -- see the judge call site.

    `max_output_tokens` (0 = leave the model's own default alone) sets
    `max_completion_tokens`. Only the judge passes one -- see
    FINAL_EVAL_MAX_OUTPUT_TOKENS. Whether it is BINDING is recorded rather than
    assumed: `_record_llm_usage` counts responses that stopped on "length"
    instead of "stop", surfaced per stage as `tokens_{stage}_length_capped`. A
    truncated JSON response would fail to parse and fall through the caller's
    fail-open path, so a rising count there is the difference between "the model
    chose to write less" and "we cut it off" -- two diagnoses needing opposite
    fixes, and previously indistinguishable."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})

    args = {"model": model, "messages": msgs, "temperature": temperature}
    if require_json:
        args["response_format"] = {"type": "json_object"}
    if cache_key:
        args["prompt_cache_key"] = cache_key
    if cache_retention:
        args["prompt_cache_retention"] = cache_retention
    if max_output_tokens:
        args["max_completion_tokens"] = max_output_tokens

    resp = client.chat.completions.create(**args)
    choice = resp.choices[0]
    _record_llm_usage(stage, model, getattr(resp, "usage", None),
                      getattr(choice, "finish_reason", "") or "")
    return choice.message.content.strip()


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
# Worker cap for screen_gate's and rank_gate's own per-batch fan-out. Raised 3 -> 4
# when engine.GATE_ROUND_SIZE went 40 -> 80: an 80-candidate round is exactly 4
# _GATE_BATCH sub-calls, so at 3 workers it ran as two waves (3 then 1) and the
# second wave was three-quarters idle. At 4 it is one wave, which is most of what
# keeps the tripled RANK_EXAMINE_BUDGET from costing proportional wall time. Still
# bounded rather than unlimited, for the original reason -- firing every batch at
# once risks a short-window rate cap, and rank_gate's own retry path already
# suspects one. THIS is the knob to turn back down if 429s/401s start appearing.
_GATE_MAX_WORKERS = 4


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


def _profile_signature_v2(profile: dict) -> str:
    """_profile_signature plus the facets added after it, folded in ONLY when they
    are non-default so an unchanged profile keeps its existing cache entries.

    "Allow overqualified" has to participate: it rewrites the seniority rule in
    both _screen_prompt and _rank_prompt, so without it a candidate who turns the
    preference on would keep being served verdicts reached under the old rule --
    exactly the stale-cache failure the signature exists to prevent. Adding the
    key unconditionally would instead re-screen every store row for every profile
    to get an identical answer for the (default) off case, so it is added only
    when true."""
    base = _profile_signature(profile)
    if not profile.get("allow_overqualified"):
        return base
    return hashlib.sha1(f"{base}|overqual".encode()).hexdigest()[:12]


def _rank_age_cache_tag(profile: dict) -> str:
    """Short, human-readable cache-key component for rank_gate's own "Maximum
    listing age" preference. Not folded into _profile_signature (shared with
    screen_gate, which never sees this preference at all -- see
    listing_over_max_age's gate-stage Python filter) for the same reason the
    intent hash rides in the gate name instead: editing the preference must
    re-score rank_gate without also re-running every cheap screen call for a
    guaranteed identical answer. Small integers, not worth hashing."""
    days = int(profile.get("max_listing_age_days") or 0)
    hard = int(bool(profile.get("max_listing_age_hard", True)))
    return f"age{days}h{hard}"


def _gate_job_id(job: dict) -> str:
    """Stable per-job key. Prefer the backend's cross-source identity when present.

    Carries a TEXT-RICHNESS marker, because _gate_cache_key below keys on
    (gate, profile signature, this) and NOT on the text that was actually judged.
    The same job can reach a gate with wildly different amounts of text: a ~455-char
    Reed/Adzuna search teaser when it's brand new, or its full description once
    fetch_reed_details or a Phase 5 scrape has supplied one (see engine.py's
    _has_full_text). Without the marker, a verdict reached on the teaser would be
    served forever for a job we can now actually read -- silently cancelling the
    enrichment for exactly the jobs that most needed re-judging.

    The marker used to be two-state (`:full` or nothing), which keyed only on
    WHERE the text came from and so missed a text that grew in place. An ATS row
    never has full_text -- its description arrives complete at discovery and
    nothing re-fetches it -- so when fetch_ats started merging in the requirements
    sections several vendors return as separate fields, every one of those rows
    kept serving the verdict reached on its company blurb alone, permanently.
    Bucketing the length in 1k steps fixes that class of change generically: text
    that grows materially re-screens, text that is merely re-fetched identically
    does not, and no global cache-version bump (which would re-screen the whole
    store to get identical answers for the untouched majority) is needed."""
    ident = job.get("_identity") or make_job_id(job.get("board", ""), job.get("url", ""))
    text = job.get("full_text") or job.get("snippet") or ""
    marker = "full" if job.get("_has_full_text") else "t"
    return f"{ident}:{marker}{len(text) // 1000}"


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
            raw = llm(prompt, system=system, require_json=True,
                      temperature=_safe_temperature(CHEAP_MODEL, 0))
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

# Deterministic backstop for the ENTRY-LEVEL FLOOR text in _screen_prompt below.
# Measured (tests/tier_analysis.py, 2026-07-29): the cheap model does not reliably
# apply that rule across a full _GATE_BATCH-sized call -- an unambiguous
# "Graduate X"/"Junior X" listing flipped between seniority_ok true/false across
# otherwise-identical repeated calls at temperature=0 (same batch, same listings,
# same prompt). Rather than trust the model to get this right every time, a title
# match forces the correction in code. Deliberately narrow in two ways: (1) only
# "graduate"/"junior"/"entry-level" tokens, NOT "intern"/"trainee"/"apprentice"/
# "placement" -- those interact with the APPRENTICESHIP carve-out above, which is
# SUPPOSED to still fail some of them, and this must not swallow that; (2) only
# overrides "seniority_low" (candidate outranks the role), never "seniority_high"
# or "too_many_gaps" -- a title containing "graduate" out of context (e.g.
# "Graduate Program Director") could still genuinely be _high, and that direction
# was never the failure mode observed, so it's left to the model untouched.
_UNAMBIGUOUS_ENTRY_TITLE_RE = re.compile(r"\b(graduate|junior|entry[- ]level)\b", re.I)
_CANDIDATE_JUNIOR_WORDS = ("intern", "graduate", "entry", "junior", "student", "trainee",
                           "apprentice", "placement")


def _apply_entry_level_floor(candidates: list[dict], profile: dict) -> int:
    """Corrects any "seniority_low" verdict screen_gate gave an unambiguous
    graduate/junior/entry-level-titled listing, for a candidate whose own stated
    seniority is itself Graduate/Junior/Entry (etc.) -- see the constants above
    for why. Runs on EVERY candidate (cache-hit or freshly judged) so a stale
    cached mis-verdict self-corrects too, not just a fresh one. Deliberately
    applied AFTER gate_cache is written (see the call site) so the cache keeps
    storing the model's own raw verdict -- still auditable via
    tests/gate_harness.py -- while every consumer sees the corrected one."""
    seniority = (profile.get("seniority") or "").lower()
    if not any(w in seniority for w in _CANDIDATE_JUNIOR_WORDS):
        return 0
    fixed = 0
    for c in candidates:
        if c.get("_seniority_ok") is False and _UNAMBIGUOUS_ENTRY_TITLE_RE.search(c.get("title") or ""):
            parts = (c.get("_gate_reason") or "").split("|")
            if parts and parts[0] == "seniority_low":
                c["_seniority_ok"] = True
                c["_seniority_signal"] = None
                parts[0] = "ok"
                c["_gate_reason"] = "|".join(parts)
                fixed += 1
    return fixed


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


def _overqualified_note(profile: dict) -> str:
    """The candidate's "Allow overqualified" preference, rendered for whichever
    seniority rule is about to be applied. "" when the preference is off, which is
    the default -- so an unchanged profile's prompt is byte-identical to before and
    keeps its prompt-cache prefix.

    Deliberately carves out the two cases the preference was never about (see
    engine._INELIGIBLE_REGARDLESS_OF_LEVEL_RE): an apprenticeship is a course with
    an eligibility bar against the already-qualified, and a placement year requires
    the applicant to still be mid-degree. Both get worse, not better, the more
    qualified the candidate is, so "I'll consider a more junior role" is not
    consent to either. All three tiers repeat this carve-out for the same reason
    the apprenticeship rules themselves are repeated at all three: a rule dropped
    at one tier reinstates the hole at that tier."""
    if not profile.get("allow_overqualified"):
        return ""
    return """
- OPEN TO MORE JUNIOR ROLES: this candidate has explicitly said they will consider roles pitched BELOW
  their own stated seniority. So a listing being more junior than them is NOT by itself a mismatch --
  do not set seniority_ok=false with reason="seniority_low" merely because the role sits a level or two
  under their stated level. Judge it as an ordinary listing. This changes NOTHING about the opposite
  direction ("seniority_high" is unaffected), and NOTHING about the APPRENTICESHIPS AND TRAINING
  SCHEMES rule above -- an apprenticeship or a student placement year is excluded because the candidate
  is INELIGIBLE for it, not because it is junior, and being open to junior roles is not consent to a
  course they cannot enrol on. Keep failing those exactly as that rule says."""


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
  That word-for-word presumption does NOT apply to a title that names two different professions and is
  only told apart by the duties -- e.g. "Automation Engineer" (software/test/RPA automation) vs
  (industrial control systems, PLCs, robotics); "Analyst" (data) vs (financial, intelligence,
  business-process); "Engineer" (software) vs (mechanical/electrical/civil); "Designer" (product/UX) vs
  (mechanical/graphic). For those, ignore the title agreement entirely and judge the described duties:
  a clearly different profession is sector_confidence="mismatch", and too little description to tell
  which profession it is is sector_confidence="ambiguous" -- never "match" on the title alone.
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
- IGNORE PAGE FURNITURE. Some listings arrive as text scraped from a job board's web page, so the
  posting is wrapped in the board's own chrome: a heading like "Business Intelligence Analyst jobs in
  Peterborough", "back to last search", "Create email alert", "Leave us your email address and we'll
  send you similar new jobs", cookie and privacy notices, "Loading...", breadcrumbs, an "Apply for this
  job" link, or a trailing list of similar vacancies with their own titles and salaries. NONE of that
  is evidence about what the page is -- it is the same furniture the board wraps around every posting,
  including real ones, and it frequently appears BEFORE the posting itself. Read past it and judge only
  the posting body. If ONE specific role is described anywhere in the text, listing_ok=true no matter
  how much surrounding chrome came with it. Set listing_ok=false only when, having ignored the
  furniture, there is no single role being described at all.

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
  ENTRY-LEVEL FLOOR -- read this before ever using "seniority_low". "Below the candidate" is measured
  against the CANDIDATE'S OWN stated seniority at the top of this block, not against some general idea
  of a serious job. When that stated seniority is Graduate, Junior, Entry-level or equivalent, there is
  almost nothing left below them: a Graduate, Junior, Entry-level, Trainee or "0-2 years" listing is a
  DIRECT MATCH for them and is seniority_ok=true. Only genuinely sub-entry work -- an unpaid internship,
  school work-experience, or an apprenticeship/training scheme the candidate is already past the level
  of (see below) -- can be "seniority_low" for such a candidate, and even then only when the listing
  says so plainly. A listing titled "Graduate X" or "Junior X" is
  never by itself evidence of a level mismatch for a graduate or junior candidate; if anything it is
  evidence of a match, and doubly so when it echoes one of their target roles above.
  APPRENTICESHIPS AND TRAINING SCHEMES -- the one case where the floor does not protect a listing. A
  listing whose title or text makes it a formal apprenticeship, traineeship or structured training
  scheme ("Apprentice", "Apprenticeship", "Level 2/3/4/5 ...", "you will study towards a qualification",
  "we will train you in ...") is not an ordinary entry-level job: it is a place on a course that comes
  with a job, and its purpose is to teach someone who does NOT yet hold the qualification or the skills.
  Set seniority_ok=false, reason="seniority_low", when BOTH hold:
    (i) the candidate already holds a qualification at or above the level the scheme awards -- for a
        candidate with a completed degree that means any BELOW-degree scheme (Level 2-5, "advanced"/
        "higher" apprenticeship, or one awarding a qualification they already have). A DEGREE
        apprenticeship, or a Level 7/master's-level scheme, is not below such a candidate and stays
        seniority_ok=true; and
    (ii) the candidate's own skills/background above already cover the core things the scheme says it
        will train them in -- an apprenticeship in a field they genuinely lack is a real opportunity
        and stays seniority_ok=true.
  A training rate of pay ("National Minimum Wage", "apprentice rate", a wage plainly under the going
  graduate rate for the field) corroborates (i) but is not required for it. Quote the scheme wording you
  relied on in "seniority_signal". This is NOT licence to fail graduate schemes, graduate programmes or
  junior roles -- those hire you at your level and are direct matches.
  (These two directions are opposite failures -- "_high" always means the ROLE outranks the
  CANDIDATE, "_low" always means the CANDIDATE outranks the role's real level or lacks the
  professional depth it expects. Do not mix them up. Sanity-check yourself before answering: if the
  reason you are about to give is that the role demands MORE than the candidate has -- more years, more
  ownership, more scope, a higher pay band than their level -- that is "seniority_high", never
  "seniority_low", no matter how the sentence is phrased.)
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
- otherwise seniority_ok=true. When unsure or genuinely ambiguous, seniority_ok=true.{_overqualified_note(profile)}{_strict("_seniority_ok")}

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
- Then apply this conflict table, and nothing else. There are exactly TWO conflicts:
    listing is fully REMOTE  + candidate stated preferences NOT including Remote  -> work_arrangement_ok=false
    listing is ON-SITE       + candidate stated ONLY Remote                       -> work_arrangement_ok=false
  Every other combination is work_arrangement_ok=true. In particular:
  - A HYBRID listing NEVER conflicts with anything. Hybrid includes on-site days, so it satisfies an
    On-site preference, and it includes remote days, so it partly satisfies a Remote one. Do not fail
    this axis on a hybrid listing for any candidate, whatever they stated. This carve-out is about the
    LISTING being hybrid; it says nothing about a candidate who stated Hybrid.
  - A candidate who stated Hybrid wants office days. A fully REMOTE listing offers none, so it does NOT
    satisfy a Hybrid preference -- "Hybrid" on the candidate side is not a wildcard, and stating
    On-site and/or Hybrid without Remote is an explicit statement that fully-remote is not wanted.
  - If the candidate stated more than one preference, the listing only has to match ONE of them --
    matching ONE is required, though: a listing matching none of several stated preferences conflicts.
  - If the candidate stated no preference, work_arrangement_ok=true always.
  - If you could not confidently classify the LISTING's arrangement in the step above, you cannot fail
    this axis -- work_arrangement_ok=true. The on-site default there is for reading the listing, not a
    licence to fail a listing you couldn't read.
  Note this axis is about the working PATTERN only, never about the city/country -- a listing in the
  wrong place is a location question, judged elsewhere, not an arrangement conflict.{_strict("_work_arrangement_ok")}

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


# Fraction of a gate round that can sail through every soft axis clean before the
# round is treated as non-discriminating and the hard-drop threshold tightens from
# 2+ failures to 1+. Loosened from 0.5 to 0.35 (i.e. the gate now tightens SOONER)
# alongside engine.RANK_EXAMINE_BUDGET going 40-80 -> 240: the gate's job is to
# stop the mid tier paying to rank hopeless candidates, and tripling the intake
# both makes that saving worth more and removes the reason to be lenient -- a
# wrongly-dropped borderline job used to cost a scarce slot out of ~40 examined,
# where now there are 200 more candidates behind it. The per-axis bar is unchanged
# (still "false only on a CLEAR mismatch, default true when unsure"), so even the
# tightened threshold needs one real, confident mismatch signal, not a coin flip,
# and the cluster-level MIN_RESULTS floor backfill still catches over-pruning.
_GATE_CLEAN_ROUND_FRACTION = 0.35


def dynamic_hard_drop_threshold(soft_fail_counts: list[int]) -> int:
    """Given each in-sector candidate's soft-axis failure count for one gate
    round, decide how many failures should hard-drop a listing. Fixed at 2+
    normally (two independent LLM signals agreeing on a mismatch), but when
    more than _GATE_CLEAN_ROUND_FRACTION of the round is sailing through every
    axis clean, that's a sign the round is thin on real mismatches rather than
    that everyone genuinely fits -- tighten to 1+ so a single confirmed mismatch
    is enough, instead of waiting for a second signal that a lax round is
    unlikely to produce. Shared by screen_gate's own diagnostic log and
    engine.py's actual hard-drop decision so the two can't disagree on what
    "hard-dropped" means."""
    if not soft_fail_counts:
        return 2
    clean = sum(1 for n in soft_fail_counts if n == 0)
    return 1 if clean / len(soft_fail_counts) > _GATE_CLEAN_ROUND_FRACTION else 2


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


def _key_requirements_text(c: dict) -> str:
    """screen_gate's extracted JD asks, rendered as one inline phrase. Shared by
    rank_gate's listing block and the final judge's job block so the two can't
    describe the same hand-off differently.

    Worth passing DOWN a tier as well as up: these items were pulled by a pass that
    had never seen the candidate, so they cannot have been bent to fit them -- and
    screen_gate reads a listing's first GATE_LISTING_TEXT_CHARS while rank_gate reads
    the first RANK_LISTING_TEXT_CHARS of the same string, so this costs nothing and
    adds nothing rank_gate couldn't in principle read itself. Its value is that the
    asks arrive already separated from the surrounding prose and already tagged
    required vs nice-to-have, which is exactly the distinction HARD DOWNGRADE (a)
    turns on and the one a scoring pass reading a wall of text most often blurs."""
    key_reqs = c.get("_key_requirements") or []
    if not key_reqs:
        return ""
    return ", ".join(
        f"{r['item']} ({r['necessity']}"
        + (", professional-level expected)" if r.get("professional_level_expected") else ")")
        for r in key_reqs
    )


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
    gate="screen_v14". Bumped from "screen_v13" to close the WORK ARRANGEMENT
    table's candidate-side hole: both of v12's conflict rows were phrased "candidate
    stated ONLY X", so a candidate stating On-site AND Hybrid matched neither row and
    a fully-REMOTE listing fell into "every other combination -> ok". The v12 hybrid
    carve-out is about the LISTING being hybrid; it was being read as making a Hybrid
    CANDIDATE compatible with everything. The remote row now reads "stated preferences
    NOT including Remote". "screen_v13" was bumped from "screen_v12" when the
    ENTRY-LEVEL FLOOR gained
    its apprenticeship/training-scheme exception -- see the floor's own text; v12
    protected every apprenticeship but a "pre-degree" one, which is why a below-degree
    BI apprenticeship at National Minimum Wage passed this axis clean for a graduate
    who already had the Power BI/SQL/Excel it offered to teach. "screen_v12" was
    bumped from "screen_v11" after a ground-truth audit
    (`tests/gate_harness.py --ground-truth --text-mode snippet`) scored this stage
    against 113 listings the expensive judge had already ruled on, and found it
    keeping only 15 of 26 judge-approved roles. Reading all 11 drops individually,
    7 were real failures and they sat in exactly two axes, both fixed here:
      * WORK ARRANGEMENT was rejecting hybrid listings for a candidate whose stated
        preference is On-site (4 of the 7). A hybrid role has on-site days, so it
        cannot conflict with wanting on-site -- and it has remote days, so it partly
        satisfies wanting remote. The axis is now an explicit two-row conflict table
        in which hybrid never conflicts with anything, plus an explicit "if you
        couldn't classify the listing you cannot fail this axis" (one dropped
        listing mentioned no arrangement at all, which the classification step says
        to read as on-site, i.e. a match for that candidate).
      * SENIORITY was firing "seniority_low" -- the candidate outranks the role --
        on Graduate and Junior listings for a candidate whose stated seniority is
        Junior, citing signals like "Graduate Analyst (graduate/early-career bar)"
        as evidence (3 of the 7). The axis now carries an explicit entry-level
        floor: for a graduate/junior/entry candidate, a graduate/junior/entry
        listing is a direct match, and only genuinely sub-entry work can be "low".
        A third case returned a plainly seniority_HIGH argument under the low code,
        so the direction rule is restated with a self-check.
    REAL LISTING CHECK was fixed in the same pass on separate evidence: re-running
    the same sample with scraped full text instead of the teaser sent this axis from
    3 failures to 50, because crawl4ai keeps the board's own chrome and postings
    arrive opening with "## <Title> jobs in <City> / Create email alert / back to
    last search" -- verbatim the category-page pattern this axis is told to reject,
    and it is an unconditional hard drop. The axis is now told to read past page
    furniture and judge only the posting body, so more text stops making the gate
    worse. (Bumped from "screen_v10": screen_v10's own word-for-word
    title presumption below turned out to have a hole -- a job title can name two
    genuinely different professions ("Automation Engineer": software/RPA vs
    industrial PLC/robotics work), so an exact match against a target role is no
    evidence of a function match for those. A live run passed an industrial
    controls role straight through this axis on the title alone and it reached the
    candidate as a top pick. The prompt now carves those titles out of the
    presumption and requires the described DUTIES to decide, so a v10 verdict was
    reached under a materially more permissive sector rule and must not be reused.
    "screen_v10" was bumped from "screen_v9": a live audit found the sector axis
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
    # _v2, not the base signature: the "Allow overqualified" preference rewrites
    # the seniority rule in _screen_prompt, so a verdict reached under the other
    # setting must not be served. Identical to the base hash while the preference
    # is off (the default), so turning it on invalidates only that profile.
    sig = _profile_signature_v2(profile)
    # screen_v14 (from screen_v13): the WORK ARRANGEMENT conflict table's remote row
    # went from "candidate stated ONLY On-site" to "stated preferences NOT including
    # Remote". Under v13 a candidate stating On-site AND Hybrid matched neither
    # conflict row, so every fully-remote listing passed this axis clean -- a v13
    # verdict was reached under a rule that could not fail those and must not be reused.
    # screen_v13 (from screen_v12): the ENTRY-LEVEL FLOOR gained its apprenticeship/
    # training-scheme exception. v12's floor explicitly protected every apprenticeship
    # except a "pre-degree" one, so a below-degree scheme for a graduate whose skills
    # already cover what it teaches passed the seniority axis clean -- a v12 verdict
    # was reached under a rule that could not fail those and must not be reused.
    # screen_v12 (from screen_v11): three axes were materially loosened/corrected
    # after a ground-truth audit (tests/gate_harness.py --ground-truth) scored this
    # stage against 113 listings the final judge had already ruled on. A v11 verdict
    # was reached under all three of the old rules and must not be reused.
    # screen_v11 (from screen_v10): the verbatim-title presumption no longer
    # applies to titles that name two different professions (see the docstring's
    # "Automation Engineer" case) -- a v10 row could have been passed on the title
    # alone and must not be reused as if it still means the same thing.
    keys = [_gate_cache_key("screen_v14", sig, _gate_job_id(c)) for c in candidates]
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

        def _ask(items: list[tuple[dict, str]]):
            """One LLM call over `items`, numbered 1..len(items). Returns
            (decisions, key_requirements) keyed by that 1-based position, or None
            if the call or its JSON failed outright. Only positions the model
            actually ruled on are present -- the caller decides what to do about
            any it skipped, rather than a silent default standing in for a
            verdict."""
            listing_block = "\n".join(
                f"{i+1}. {c['title']} @ {c.get('company','')} | "
                f"{(c.get('location') or 'location unknown')}"
                f"{_listing_salary_suffix(c)} | "
                f"{(c.get('full_text') or c.get('snippet') or '')[:GATE_LISTING_TEXT_CHARS]}"
                for i, (c, _k) in enumerate(items)
            )
            got: dict[int, tuple[str, bool, bool, bool, str | None, bool, bool, bool, bool, str]] = {}
            reqs: dict[int, list[dict]] = {}
            try:
                # _safe_temperature, not a bare 0: this except branch keeps the
                # WHOLE batch on any error, so a CHEAP_MODEL that rejects
                # temperature=0 would not fail loudly here -- it would pass every
                # listing on every axis and log one parse-failure line. Since
                # CHEAP_MODEL is env-overridable, that is a config away.
                raw = llm(_screen_prompt(profile, listing_block), require_json=True,
                          temperature=_safe_temperature(CHEAP_MODEL, 0),
                          stage="screen", cache_key=f"screen_v14:{sig}",
                          # 24h retention for the same reason rank_gate's cache_key carries it:
                          # this ~4.5k-token prefix is profile-scoped (screen_v14 + _profile_signature),
                          # so it's only ever reused by this SAME profile's own later rounds/re-searches,
                          # and the default few-minutes TTL was letting it expire between them -- see
                          # rank_cache_key's comment and _FINAL_EVAL_CACHE_KEY's for the judge's version
                          # of this same tradeoff.
                          cache_retention="24h",
                          system="You screen job listings for role-function fit (match/ambiguous/"
                                 "mismatch), the candidate's own hard filters, whether the text is even "
                                 "a real single job posting, seniority, requirements, skills, salary, "
                                 "and work-arrangement fit. Be inclusive when unsure. Return exactly one "
                                 "decision object per listing -- never skip one.")
                for d in json.loads(clean_json(raw)).get("decisions", []):
                    n = d.get("n")
                    if isinstance(n, int) and 1 <= n <= len(items):
                        sector_confidence = str(d.get("sector_confidence", "match")).strip().lower()
                        if sector_confidence not in ("match", "ambiguous", "mismatch"):
                            sector_confidence = "match"
                        seniority_signal = d.get("seniority_signal")
                        got[n] = (sector_confidence,
                                  bool(d.get("hard_gate_ok", True)),
                                  bool(d.get("listing_ok", True)),
                                  bool(d.get("seniority_ok", True)),
                                  str(seniority_signal) if seniority_signal else None,
                                  bool(d.get("requirements_ok", True)),
                                  bool(d.get("skills_ok", True)),
                                  bool(d.get("salary_ok", True)),
                                  bool(d.get("work_arrangement_ok", True)),
                                  str(d.get("reason", "ok")))
                        reqs[n] = _sanitize_key_requirements(d.get("key_requirements"))
            except Exception as e:
                emit(f"[gate:screen] batch parse failed ({e}); keeping batch (fail-open).")
                return None
            return got, reqs

        decisions: dict[int, tuple[str, bool, bool, bool, str | None, bool, bool, bool, bool, str]] = {}
        key_reqs_by_n: dict[int, list[dict]] = {}
        result = _ask(batch)
        if result is not None:
            decisions, key_reqs_by_n = result
            # The model routinely returns valid JSON that simply OMITS some of the
            # listings it was given (a live 113-listing run silently skipped 17).
            # That used to fall through to the "everything ok" default below AND get
            # written to gate_cache as if it were a real verdict -- so a listing no
            # model had ever ruled on was recorded as passing every axis, permanently.
            # Re-ask for just the skipped ones instead: a much shorter prompt, and in
            # practice they come back. Bounded to a single retry.
            missing = [i for i in range(len(batch)) if (i + 1) not in decisions]
            if missing:
                emit(f"[gate:screen] {len(missing)} of {len(batch)} listing(s) omitted from the "
                     f"response; re-asking for those only.")
                retry = _ask([batch[i] for i in missing])
                if retry:
                    retry_decisions, retry_reqs = retry
                    for pos, original_i in enumerate(missing, start=1):
                        if pos in retry_decisions:
                            decisions[original_i + 1] = retry_decisions[pos]
                            key_reqs_by_n[original_i + 1] = retry_reqs.get(pos, [])
                still_missing = [i for i in missing if (i + 1) not in decisions]
                if still_missing:
                    emit(f"[gate:screen] {len(still_missing)} listing(s) still unjudged after retry; "
                         f"keeping them (fail-open) and NOT caching a verdict for them.")

        for i, (c, key) in enumerate(batch):
            # Whether this listing got a real verdict, as opposed to the fail-open
            # default. Gates the cache write below: a fabricated pass must never be
            # persisted, or the job is never re-screened and the omission becomes
            # permanent. It still flows on through this run, unfiltered, as before.
            judged = (i + 1) in decisions
            # Explicit, because it is otherwise unrecoverable downstream: the
            # fail-open default sets every axis true, and "missing_decision" is
            # normalised out of the packed _gate_reason by the seniority_ok branch
            # below, so an unjudged listing is byte-identical to one the model
            # actively cleared. Diagnostics (tests/gate_harness.py) read this to
            # tell "passed" from "never looked at".
            c["_gate_unjudged"] = not judged
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
            if judged:
                new_entries.append((key, sector_ok, packed_reason,
                                    json.dumps(key_reqs) if key_reqs else None))
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
        with ThreadPoolExecutor(max_workers=min(_GATE_MAX_WORKERS, len(batches))) as pool:
            for entries in pool.map(_screen_one_batch, batches):
                new_entries.extend(entries)

    _gate_cache_store(new_entries)
    entry_floor_fixed = _apply_entry_level_floor(candidates, profile)
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
    fixed_note = f", {entry_floor_fixed} entry-level-floor override(s)" if entry_floor_fixed else ""
    emit(f"[gate:screen] {len(candidates)} in -> {in_sector} in-sector ({ambiguous_sector} ambiguous), "
         f"{all_soft_ok} pass all soft axes, {hard_dropped} hard-dropped ({threshold}+ soft-axis "
         f"failures), seniority drops: {seniority_high} high / {seniority_low} low{fixed_note} "
         f"({n_cached} from cache)")
    return candidates


_SALARY_PERIOD_WORD = {"year": "per year", "month": "per month", "week": "per week",
                       "day": "per day", "hour": "per hour"}


def _listing_salary_suffix(c: dict) -> str:
    """The structured pay figures for a listing block, WITH their period.

    The period is the load-bearing part. These numbers reach the salary axis of
    screen_gate and HARD DOWNGRADE (e) of rank_gate, both of which compare them
    against the candidate's stated (annual) floor -- so a bare "Salary: 25-32"
    from an hourly listing reads as a catastrophically underpaid role rather
    than as roughly £50k. Where no source stated a period, none is claimed here
    either: the models get the raw figures and the same absence of information
    the pipeline has.

    Note these fields were empty for essentially every gated candidate until
    salary was persisted on JobSeen -- the pipeline's candidates come from the
    store (engine._rows_to_dicts), which carried no salary columns, so this
    suffix only ever fired on the freshly-discovered dicts that never reach a
    gate. Populating it is a change in what the cheap tiers can see.

    Adzuna MODELS a figure for postings that state no salary at all
    (salary_is_predicted) -- see the fetch_adzuna comment. That is not a stated
    bar, so it is labelled "(estimated by the job board, not employer-stated)"
    rather than presented as a fact: HARD DOWNGRADE (e)/the salary axis both
    require CLEAR stated evidence before downgrading, and an explicitly-hedged
    figure reads as exactly the kind of ambiguity that rule is told never to
    fire on."""
    salary_min, salary_max = c.get("salary_min"), c.get("salary_max")
    if not salary_min and not salary_max:
        return ""
    currency = (c.get("salary_currency") or "").strip()
    period = _SALARY_PERIOD_WORD.get((c.get("salary_period") or "").strip().lower(), "")
    figure = (f"{salary_min}-{salary_max}" if salary_min and salary_max
              else f"{salary_min or salary_max}")
    parts = [p for p in (currency, figure, period) if p]
    estimated = " (estimated by the job board, not employer-stated)" if c.get("salary_is_predicted") else ""
    return f" | Salary: {' '.join(parts)}{estimated}"


def _location_scope_note(profile: dict) -> str:
    """Human-readable gloss on profile['location_scope'] for the HARD DOWNGRADE (d)
    GEOGRAPHY check below -- without it, the model has only the candidate's raw place
    name and no way to know "national"/"international" scope means they deliberately
    opted OUT of a city-radius search (see snapshot.build_snapshot's location_scope
    handling), so it judged bare distance instead and downgraded a same-country
    on-site/hybrid role for merely being far from the candidate's city -- exactly the
    scope a national search opts into. Mirrors the "Location search scope" CV line
    snapshot.py adds for the final judge (same fix, same reasoning, independent text
    since this stage builds its prompt from profile fields, not the synthetic CV)."""
    scope = (profile.get("location_scope") or "national").lower()
    location = profile.get("location") or "their stated location"
    if scope == "local":
        return f"local -- only near {location} is workable; a distant on-site/hybrid role is impractical."
    if scope == "international":
        return "international -- the candidate will consider on-site/hybrid roles anywhere in the world; distance/country alone is never a GEOGRAPHY downgrade for them, only an unmet visa/right-to-work requirement is."
    return (f"national -- the candidate opted into a country-wide search, not narrowed to {location}; "
            "an on-site/hybrid role elsewhere in their OWN country is NOT a GEOGRAPHY downgrade merely "
            "for being a different city or far away, only a different country or an unmet visa/"
            "right-to-work requirement is.")


def _rank_prompt(profile: dict, listing_block: str) -> str:
    multi_note = (
        "\nNote: the candidate has more than one distinct role interest; judge fit ONLY against the "
        "target roles listed above for THIS batch, not any other goals they may have listed elsewhere -- "
        "don't penalize a listing for not matching an unrelated interest of theirs.\n"
        if profile.get("_multi_cluster") else ""
    )
    salary_floor = profile.get("salary_floor") or 0
    # The candidate's own words, when they wrote any (snapshot.build_snapshot's
    # engine_profile["intent_text"]). Placed immediately after the target-role
    # titles because that is what it disambiguates: the titles alone can't say
    # which of an ambiguous title's two professions the candidate means, and this
    # stage previously had no access to the answer at all. Marked as outranking
    # the inferred tags for the same reason it does at the final judge -- it is
    # the one signal the candidate wrote themselves.
    intent = (profile.get("intent_text") or "").strip()
    intent_block = (
        "\nWhat the candidate says they are looking for, IN THEIR OWN WORDS (this is the most "
        "authoritative signal here -- where it conflicts with the inferred tags below, believe "
        f"this):\n\"{intent}\"\n"
        if intent else ""
    )
    # Only rendered when the candidate's own "Maximum listing age" preference is
    # enforced Hard (config default) -- a Soft preference is downgrade-only, and
    # that's already covered by SCORING component 3 (STALENESS) below, so adding
    # this rule for a Soft candidate would just contradict it. See
    # snapshot.build_snapshot's engine_profile["max_listing_age_days"/"_hard"].
    # Which of the candidate's stated PREFERENCES they marked binding. A Hard one
    # keeps its old cap-the-score-at-15 downgrade; a Soft one moves to the
    # SOFT-PREFERENCE MISMATCHES section below, which sets a flag instead of
    # destroying the score.
    #
    # Why this split exists: RANK_REJECT_SCORE_FLOOR was doing two unrelated jobs
    # at once. It is nominally a quality bar ("is this clearly not a fit"), but
    # the work-arrangement and salary downgrades cap the score at 15 precisely so
    # that the floor would catch them -- which meant the floor could not be
    # lowered to let more borderline roles reach the judge without simultaneously
    # disabling the enforcement of two soft preferences. Splitting them lets the
    # floor go back to being only a quality bar (engine.RANK_REJECT_SCORE_FLOOR,
    # now 32) while soft-preference mismatches demote via
    # engine._selection_score's SOFT_VIOLATION_SELECTION_PENALTY -- ordering only,
    # never elimination, which is what "Soft" meant all along.
    hard_axes = set(profile.get("hard_axes") or [])
    arrangement_hard = "_work_arrangement_ok" in hard_axes
    salary_hard = "_salary_ok" in hard_axes
    _arrangement_rule = """{lbl} STATED ARRANGEMENT: classify the LISTING's own arrangement first (explicit remote/distributed/
   work-from-home wording means remote; explicit hybrid wording means hybrid; a stated city/office with
   no remote/hybrid mention means ON-SITE there, never remote-by-default), then check whether it matches
   ANY of the candidate's stated work-type preferences above. A fully REMOTE listing is not automatically
   fine -- remote is always geographically workable, but a candidate who listed On-site and/or Hybrid and
   did NOT list Remote has said they want office presence, and a remote-only role gives them none of it.
   A HYBRID listing matches any stated preference (it has both office and remote days). If the candidate
   stated no work-type preference at all, this rule does not apply.
"""
    _salary_rule = """{lbl} SALARY: the listing states a salary clearly below the candidate's stated floor (never fires
   when either is unstated or the ranges could plausibly overlap).
"""
    # See _overqualified_note (the screen_gate twin) for why the apprenticeship
    # carve-out is repeated at every tier rather than stated once.
    overqualified_rank_note = ("""
   THIS CANDIDATE IS OPEN TO MORE JUNIOR ROLES -- they have said so explicitly. So do not deduct here
   for a role being pitched below their stated seniority; score it on how well it matches otherwise.
   HARD DOWNGRADE (f) is unaffected: an apprenticeship or structured training scheme they are
   over-qualified for is excluded because they are ineligible for the course, not because it is junior,
   and being open to junior roles is not consent to that.""" if profile.get("allow_overqualified") else "")
    max_age_days = profile.get("max_listing_age_days")
    max_age_rule = (
        f"""g. MAX LISTING AGE: the candidate has set a maximum listing age of {max_age_days} days. If the
   "[listing age: ...]" tag marks this listing "ELIMINATE", or the listing's own text states an explicit
   posting/opening date or unambiguous staleness that itself works out to more than {max_age_days} days ago,
   treat it the same as CLOSED LISTING above -- cap the score at 15. Never apply this from a vague sense
   that a listing "feels old", from silence, or from a closing/deadline date alone (that's a separate
   concern, see STALENESS below) -- only a definite, confirmed age past the stated limit qualifies.
"""
        if max_age_days and profile.get("max_listing_age_hard", True) else ""
    )
    # Assemble the two sections. A preference the candidate marked binding stays a
    # score-destroying downgrade; a soft one only raises the flag.
    # Rule letters/numbers are assigned by position within whichever section the
    # rule lands in, so neither list ever shows a gap. The hard list continues
    # from (d) LOCATION, which is always present; the soft list numbers from s1.
    downgrade_extra = ""
    soft_pref_rules = ""
    # "f" (OVER-QUALIFIED) and "g" (MAX LISTING AGE) are hard-coded in the template
    # below, so an inserted rule may only take "e" or "h" -- taking "f" produced two
    # rules labelled (f) when both preferences were Hard.
    hard_labels = iter("eh")
    soft_labels = iter(("s1.", "s2."))
    for rule, is_hard in ((_arrangement_rule, arrangement_hard), (_salary_rule, salary_hard)):
        if is_hard:
            downgrade_extra += rule.format(lbl=f"{next(hard_labels)}.")
        else:
            soft_pref_rules += rule.format(lbl=next(soft_labels))
    soft_pref_block = (
        f"""
SOFT-PREFERENCE MISMATCHES -- a SEPARATE, much weaker check, and the scoring rules above still apply
in full. These are things the candidate said they PREFER but explicitly did NOT ask to be rejected on.
When one of these CLEARLY fires for a listing, do NOT cap or crush its score: score the role on its
merits exactly as you otherwise would, and set "soft_violation": true on it instead. Something
downstream uses that flag to rank such a role below an equally-good one that matches, which is what a
soft preference is supposed to mean. Setting it wrongly costs the candidate a good role's position, so
require the same CLEAR, explicit evidence as the downgrades above -- silence, ambiguity or truncation
never raises it. If none of these fire, set "soft_violation": false.
{soft_pref_rules}"""
        if soft_pref_rules else ""
    )
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    return f"""You are estimating how well each job listing fits ONE candidate, as a rough numeric score.
This score gates which listings proceed to detailed review -- a wrong score buries a job silently, so
when genuinely unsure between two scores, prefer the higher one.

Today's date: {today_str}

Candidate target roles: {_annotate_with_weight_tiers(profile.get('search_terms') or [], profile.get('target_role_weight_tiers'))}{intent_block}
Candidate seniority: {profile.get('seniority', 'mid-level')}
Candidate core skills: {_annotate_with_weight_tiers(profile.get('key_skills') or [], profile.get('skill_weight_tiers'), profile.get('skill_evidence_tiers'))}
Candidate location: {profile.get('location') or 'none stated'}
Candidate location search scope: {_location_scope_note(profile)}
Candidate work-type preference: {', '.join(profile.get('work_types') or []) or 'none stated'}
Candidate stated salary floor: {salary_floor if salary_floor else 'none stated'}
{_candidate_background_block(profile)}{multi_note}
Some listings carry a bracketed "[key requirements (extracted by an earlier pass): ...]" tag. Those are
this posting's own stated asks, each tagged "required" or "nice_to_have", pulled out by an earlier
screening pass that had NEVER seen the candidate -- so they describe what the employer asked for, not
what anyone thought would suit this person. Start the HARD DOWNGRADES check below from that list where it
is present: a "required" item there is exactly the kind of stated bar rule (a) and (b) turn on. Two
limits. The list is capped at a few items and was read from a SHORTER excerpt than you are shown, so it
is never complete -- an ask that appears only in the fuller text below still counts, and you must read
that text rather than treating the tag as the whole picture. And it is an extraction, not a verdict: it
says what the posting asks for, never whether the candidate meets it, which is yours to judge. A listing
with no such tag is not a listing with no requirements.

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
   WISH-LIST EXCEPTION (applies to rules a and b, and to nothing else): some postings list an ideal
   hire rather than a bar, and downgrading those to 15 buries roles the candidate would realistically
   be interviewed for. Three tells, all readable from the text you are shown: the poster is a
   RECRUITMENT OR STAFFING AGENCY rather than the employer (agency adverts are a recruiter's padded
   summary of a manager's brief, written for a wide funnel); the role is CONTRACT / interim /
   fixed-term / day-rate rather than permanent (contract bars are lower and more negotiable -- the org
   needs a competent gap-filler, with less long-term risk); or the "essential" list is LONG (roughly
   eight or more items, especially grouped into technical / analytical / communication sections) and/or
   the pay is stated as "negotiable"/"competitive". Where one or more of these clearly holds, do NOT
   fire rule (a) or (b): score the role normally on function and depth fit, taking a real shortfall
   as an ordinary DEPTH FIT deduction instead. This exception NEVER applies to rules (c) through (h) --
   a closed listing, a geography/right-to-work failure, a maximum-age elimination and an apprenticeship
   over-qualification are facts about eligibility, not negotiable expectations -- and it is never a
   reason to pretend evidence exists.
b. REQUIRED CREDENTIAL OR TOOL: the listing names a specific certification, qualification level, or
   tool as REQUIRED (not nice-to-have) and nothing in the candidate's skills/background above
   evidences it or plainly covers it.
c. CLOSED LISTING: the text says the vacancy is closed -- e.g. "the application deadline has now
   passed", "no longer accepting applications" -- or its "[listing age: ...]" tag says the stated
   closing date has PASSED. ALSO fires when the listing states an explicit application deadline/
   closing date (e.g. "Apply by 9 August 2026", "Closing date: 09/08/26") and that date is BEFORE
   today's date given above -- a listing routinely keeps its original forward-looking "apply by"
   wording verbatim after the date has quietly passed, so do not require backward-looking phrasing
   or the age tag for this half: work out the date yourself from what the listing states and compare
   it to today. A deadline that is today or in the future, vague ("rolling", "ongoing"), or not
   stated at all never fires this.
d. LOCATION / WORK ARRANGEMENT: first classify the LISTING's own arrangement -- explicit remote/
   distributed/work-from-home wording means remote; explicit hybrid wording means hybrid; a stated
   city/office with no remote/hybrid mention means ON-SITE there (never remote-by-default). What
   follows from that classification:
   - GEOGRAPHY: the classified arrangement clearly cannot work given the candidate's stated location
     search scope (see "Candidate location search scope" above -- it defines what "cannot work"
     means and overrides a bare distance/city reading: under "national" scope, an in-country on-site
     or hybrid role is NEVER a GEOGRAPHY failure merely for being a different city or far away, and
     under "international" scope distance/country alone is never one either), or the listing requires
     an already-held work permit / right-to-work in a country that clearly isn't the candidate's --
     that half applies regardless of scope.
{downgrade_extra}f. OVER-QUALIFIED FOR A TRAINING SCHEME: the listing is a formal apprenticeship, traineeship or
   structured training scheme ("Apprentice"/"Apprenticeship" in the title, "Level 2/3/4/5", "you will
   study towards a qualification", "we will train you in ..."), AND the candidate above already holds a
   qualification at or above the level it awards (for a degree-holder: any below-degree scheme), AND
   their listed skills already cover what it says it will teach. Such a scheme exists to train someone
   who lacks those things -- matching its skill list well makes it a WORSE fit, not a better one, and
   many carry an eligibility bar against applicants who already hold an equivalent qualification. A
   DEGREE apprenticeship or Level 7/master's-level scheme is not covered by this, nor is a graduate
   scheme or graduate programme -- those hire at the candidate's level and are normal listings.
{max_age_rule}{soft_pref_block}
SCORING -- four components, in this order (for listings with no hard downgrade):
1. FUNCTION MATCH (the primary driver of the score): does the role's actual day-to-day work match the
   target roles above -- a same-function role in a different industry is a good match; a different-
   function role in the candidate's own industry is not. Within "analyst"-type titles specifically,
   distinguish analytical work (interpreting data, building insights, reporting) from operational work
   (data entry, processing, validation, administration) -- a role titled "Analyst" or "Technician" that is
   mostly the latter is a WEAKER function match than the title alone suggests, even within the right field.
   Some titles name two different professions and are told apart only by the duties -- "Automation
   Engineer" (software/test/RPA) vs (industrial PLCs, control systems, robotics), "Engineer" (software) vs
   (mechanical/electrical/civil), "Designer" (product/UX) vs (mechanical/graphic), "Analyst" (data) vs
   (financial/intelligence). An exact title match against a target role counts for nothing on those: read
   the duties, and score the wrong profession low however precisely the titles agree.
   A listing tagged "[gate note: role-function fit vs target roles was ambiguous, not a confirmed match]"
   means an earlier, shorter-text screening pass could not confidently tell -- judge FUNCTION MATCH
   yourself from the fuller text below rather than assuming it's already settled; score it on what you
   actually see, high or low.
2. DEPTH FIT (a secondary adjustment -- do NOT let it override a poor function match): given the
   candidate's background evidence above, how plausible is it they can do THIS role's tasks at its stated
   seniority? Use this to move the score up or down a moderate amount within a function-match band, not to
   rescue a role whose core function doesn't match.{overqualified_rank_note} A target role or skill tagged "strongly
   preferred"/"preferred" reflects the candidate's own past tick feedback -- nudge the score up a little for
   a strong match on it. One tagged "deprioritize"/"lower priority" reflects past cross feedback -- nudge
   the score down a little if the listing leans heavily on it.
3. STALENESS (a small adjustment, applied last): a listing whose "[listing age: ...]" tag marks it STALE
   has been open a while relative to the candidate's own stated tolerance -- usually still live, but most
   of its shortlist is already decided, so it is a worse use of an application than an equally-good fresh
   role. Subtract roughly 10 points, more for a very old one. This is a nudge, never a downgrade: a stale
   role that fits well still outscores a fresh one that doesn't, and a listing with NO age tag has an
   unknown date and is never penalised for it. A tag marked ELIMINATE instead of STALE is a stronger,
   confirmed signal -- see HARD DOWNGRADE (g) above, which applies instead of this nudge whenever the
   candidate's maximum listing age is enforced Hard; when it is a Soft preference (or the tag never reaches
   ELIMINATE), this nudge is all that ever applies, however old the listing.
   The age tag may report TWO different clocks and they mean different things: "posted N days ago" is
   what the job board itself claims, while "we have been finding this same listing ... for N days" is
   how long this system has been seeing it advertised -- a lower bound on its real age, never a sign it
   is new. When the tag says the posting date "appears to have been refreshed", trust the longer clock:
   that is an old advert re-dated, not a new vacancy.
4. STANDING/PIPELINE AD (a small adjustment, applied last): a listing carrying a "[possible
   standing/pipeline ad: ...]" tag, or whose age tag says it has been advertised near-continuously on
   many separate days, may be an always-open talent-pool advert rather than one specific vacancy --
   applications to those often go into a database rather than to a hiring manager for an open role.
   Subtract roughly 15 points where the fuller text supports it. Check the text before applying it: if
   the posting clearly describes one specific role with its own duties and requirements, the phrase was
   just boilerplate in a careers-page footer and you should NOT penalise it. Like staleness this is a
   nudge and never a downgrade -- it can lower a score but must never by itself push a well-matched role
   below a poorly-matched one, and an untagged listing is never penalised for it.

Give each listing a fit_score from 0 (clearly wrong fit) to 100 (excellent fit). Judge relatively across
the whole batch -- spread scores out rather than clustering everything near one number.

Output ONLY JSON: {{"scores":[{{"n":1,"fit_score":72,"note":"...","soft_violation":false}},
{{"n":2,"fit_score":40,"note":"...","soft_violation":false}}]}}
"note": one short phrase (under 12 words) naming the main driver of the score -- e.g. "strong function +
title match" or "operational role, weak function match despite title". For a hard-downgraded listing,
the note MUST name the downgrade, e.g. "hard: 3+ years paid experience bar" or "hard: on-site Cyprus,
candidate UK". For audit purposes only, never shown to the candidate. Include one object per listing,
numbered exactly as shown.
"soft_violation": true only when a SOFT-PREFERENCE MISMATCH above clearly fired for that listing, false
otherwise (and always false when no such section appears above). It must NOT change "fit_score" -- the
two are independent, and a role can score 80 with "soft_violation": true. When you set it, say which one
fired in the note too, e.g. "soft: remote-only, candidate wants on-site".

Listings:
{listing_block}"""


def _score_rank_batch(
    batch: list[tuple[dict, str]], profile: dict
) -> tuple[dict[int, float], dict[int, str], dict[int, bool], bool]:
    """Scores one rank_gate batch. Returns (scores, notes, soft_flags, batch_failed) without
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

    def _req_tag(c: dict) -> str:
        # See _key_requirements_text: screen_gate already extracted this listing's
        # asks, tagged required vs nice-to-have, and until now handed them only to
        # the final judge -- so this stage was re-deriving them from raw prose while
        # a cleaner version of the same information sat unused on the candidate dict.
        req_text = _key_requirements_text(c)
        return f"[key requirements (extracted by an earlier pass): {req_text}] " if req_text else ""

    def _age_tag(c: dict) -> str:
        return _listing_age_tag(
            c, store_age_days=profile.get("store_age_days"),
            max_age_days=profile.get("max_listing_age_days"),
            hard=profile.get("max_listing_age_hard", True),
        )

    listing_block = "\n".join(
        f"{i+1}. {c['title']} @ {c.get('company','')} | "
        f"{(c.get('location') or 'location unknown')}{_listing_salary_suffix(c)} | "
        f"{_teaser_tag(c)}"
        f"{_age_tag(c)}"
        f"{_listing_liveness_tag(c)}"
        f"{'[gate note: role-function fit vs target roles was ambiguous, not a confirmed match] ' if c.get('_sector_ambiguous') else ''}"
        f"{_req_tag(c)}"
        f"{(c.get('full_text') or c.get('snippet') or '')[:RANK_LISTING_TEXT_CHARS]}"
        for i, (c, _k) in enumerate(batch)
    )
    prompt = _rank_prompt(profile, listing_block)
    scores: dict[int, float] = {}
    notes: dict[int, str] = {}
    # Which listings tripped a SOFT-PREFERENCE MISMATCH (see _rank_prompt). Kept
    # entirely separate from `scores`: this demotes a role's ORDERING into the
    # judge pool (engine._selection_score) and must never touch the score the
    # reject floor and the card's "Fit estimate" chip are computed from.
    soft_flags: dict[int, bool] = {}
    # See _safe_temperature -- gpt-5.6-luna 400s on temperature=0 just
    # like gpt-5.6-terra does on 0.2, which was silently tripping the
    # fail-open except branch below on every single rank_gate batch (every
    # job scored a flat neutral 50.0, i.e. no real ranking signal at all)
    # until this was added.
    rank_temperature = _safe_temperature(MID_MODEL, 0)
    rank_system = ("You estimate rough candidate-job fit scores. Spread scores out; "
                    "don't cluster everything near one value.")
    # Prompt-cache routing key. Mirrors the gate_cache key's own scoping (profile
    # signature + intent hash) because those are exactly the profile facets
    # _rank_prompt interpolates ABOVE the listing block, i.e. what the shared
    # ~2.5k-token prefix is made of. Recomputed per batch rather than threaded
    # down from rank_gate: it's one small sha1 against several thousand tokens of
    # prompt, and keeping it local means the two concurrent batch workers can't
    # disagree about it.
    rank_cache_key = (f"rank_v17:{_profile_signature_v2(profile)}:"
                      + hashlib.sha1((profile.get("intent_text") or "")
                                     .strip().lower().encode()).hexdigest()[:8]
                      + f":{_rank_age_cache_tag(profile)}")
    # cache_retention="24h", same as the judge's _FINAL_EVAL_CACHE_KEY and for the
    # same reason: the default few-minutes-of-inactivity TTL was measured giving
    # this stage a 0% cache-hit rate (rank_cache_key already varies by profile
    # signature/intent/age tag, so a changed profile naturally rotates onto a
    # fresh, uncached key -- retention can't serve stale content). Unlike the
    # judge's prefix this one is profile-scoped, not global, so it only pays off
    # across this SAME profile's own rounds/re-searches -- but within a run, a
    # cluster's later gate+rank rounds reuse the identical fixed prefix computed
    # here, and across runs an unchanged profile does too (see the module's
    # cross-run-reuse note), both of which the short default TTL was losing.
    raw = None
    try:
        raw = llm(prompt, require_json=True, temperature=rank_temperature, model=MID_MODEL,
                  system=rank_system, stage="rank", cache_key=rank_cache_key,
                  cache_retention="24h")
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
                      system=rank_system, stage="rank", cache_key=rank_cache_key,
                      cache_retention="24h")
        except Exception as e2:
            status2 = getattr(e2, "status_code", None)
            emit(f"[gate:rank] {MID_MODEL} retry also failed (status={status2}): {e2}; "
                 f"falling back to {CHEAP_MODEL}.")
            try:
                raw = llm(prompt, require_json=True,
                          temperature=_safe_temperature(CHEAP_MODEL, 0), model=CHEAP_MODEL,
                          system=rank_system, stage="rank_fallback",
                          cache_key=rank_cache_key)
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
                    if d.get("soft_violation") is True:
                        soft_flags[n] = True
        except Exception as e4:
            emit(f"[gate:rank] batch response parse failed ({e4}); no rank signal for this batch.")
            batch_failed = True

    if batch_failed:
        emit(f"[gate:rank] no rank signal for {len(batch)} candidate(s) in this batch -- "
             f"fail-open (bypassing RANK_REJECT_SCORE_FLOOR).")
    return scores, notes, soft_flags, batch_failed


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
    Cached per (profile signature + intent hash + max-listing-age tag, job id) in
    gate_cache under gate="rank_v17.{intent_tag}.{age_tag}"
    bumped from "rank_v16": the prompt now states TODAY'S DATE and HARD DOWNGRADE (c)
    CLOSED LISTING fires on a stated application deadline that has already passed,
    even when the listing's own wording is still forward-looking ("Apply by 9 August
    2026") rather than an explicit closure notice. Before this, a passed deadline was
    only ever caught via the "[listing age: ...]" tag, which only exists when the
    SOURCE'S structured expires_at field was populated -- most sources never supply
    one (JSearch/organic-board listings routinely come back with expires_at=None even
    though the JD text states a real date), so a JD stating a lapsed "Apply by" date
    verbatim was unreachable by any check in this pipeline: a live case (Met Office
    "Junior Software Developer" via jsearch, deadline 09/08/2026, judged after that
    date) reached "strong fit" with the model correctly extracting "deadline":
    "09/08/2026" for display and never once comparing it to the current date because
    nothing told it what date it was. The model must now read the date itself out of
    the listing text and compare it, so this can misfire on an ambiguous date format
    (day/month order) the same way any date reasoning can -- acceptable, since the
    prior failure mode was a silent miss on every source lacking structured expiry,
    not an occasional false positive. "rank_v16" was bumped from "rank_v15": the two
    SOFT-preference downgrades (STATED ARRANGEMENT
    and SALARY) no longer cap the score at 15 when the candidate left them Soft.
    They now live in their own SOFT-PREFERENCE MISMATCHES section which sets a
    "soft_violation" flag instead, and only the Hard-enforced ones stay in HARD
    DOWNGRADES. This untangles RANK_REJECT_SCORE_FLOOR, which had been doing two
    unrelated jobs -- a quality bar AND the enforcement path for those two soft
    preferences (they capped at 15 precisely so the floor would catch them), so
    the floor could not be lowered without silently disabling soft enforcement.
    A v15 score for a soft-mismatching listing is a 15 that means "preference
    mismatch", not "bad fit", and is not comparable to a v16 score. The gate_cache
    text column also gained a third packed field for the flag. "rank_v15" was
    bumped from "rank_v14": new HARD DOWNGRADE (g), MAX LISTING AGE -- replaces the
    old fixed STALE_LISTING_DAYS=45 downgrade-only mechanism with the candidate's own
    configurable "Maximum listing age" preference (default 30 days), and caps the
    score at 15 (like CLOSED LISTING) instead of only ever nudging it down, whenever
    that preference is enforced Hard. The max-listing-age tag folded into the gate
    name (_rank_age_cache_tag, alongside the existing intent hash) means editing the
    preference re-scores without a global cache-version bump; a v14 score was reached
    under a rule that could never cap a listing for this regardless of confirmed age,
    and cannot be reused. "rank_v14" was
    bumped from "rank_v13": SCORING gained component 4 (STANDING/PIPELINE AD, ~-15)
    and component 3 (STALENESS) now explains the age tag's TWO clocks -- the board's
    claimed posting date and our own observation window -- including what to do when
    they disagree, which is the signature of a re-dated old advert. A v13 score was
    reached without either signal and cannot be reused. "rank_v13" was
    bumped from "rank_v12": the GEOGRAPHY half of HARD DOWNGRADE (d) judged bare
    distance between the candidate's stated place and the listing's, with no idea the
    candidate's location_scope ("local"/"national"/"international") had already opted
    them INTO a country-wide or worldwide search -- so a "national"-scope candidate's
    in-country on-site/hybrid role could still be downgraded to <=15 for merely being a
    distant city, exactly the scope national search exists to allow. The prompt now
    carries a "Candidate location search scope" line (_location_scope_note) that
    GEOGRAPHY must defer to instead of inferring from distance alone. A v12 score for
    any non-remote listing may have been reached under a rule that could not tell
    "far away" from "outside the candidate's declared scope" apart.
    "rank_v12" (bumped from "rank_v11": HARD DOWNGRADE (d) was a pure FEASIBILITY test -- "clearly
    cannot work given the candidate's stated location" -- which a fully-remote listing
    always passes, so a candidate's stated On-site/Hybrid preference could never fire it.
    It is now split into a geography half and a stated-arrangement half, so a v11 score
    was reached under a rule that could not downgrade a remote-only role. "rank_v11" was
    bumped from "rank_v10": listing blocks now carry a "[listing age: ...]" tag and
    SCORING gained component 3, staleness, so a v10 score was reached with no way to
    know how long the posting had been open. The same bump also covers the ATS text
    fix -- an ATS listing's stored text now includes the requirements sections several
    vendors return in separate fields, so a v10 score for one of those was reached on
    the company blurb alone. "rank_v10" was itself
    bumped from "rank_v9" for three changes at once, any one of which makes a v9 score
    non-comparable. (1) HARD DOWNGRADES gained rule f, over-qualification for a training
    scheme: a v9 score was reached under a rule set in which an apprenticeship whose
    skill list the candidate already matched scored HIGHER for that match, not lower --
    a live run scored two below-degree analyst apprenticeships 84 and 90 for a graduate
    who already held every tool they offered to teach. (2) RANK_LISTING_TEXT_CHARS went
    3000 -> 5000, so a v9 score for any job with a scraped page was reached on strictly
    less text than this one sees -- see that constant's own comment for the offsets that
    justified it. (3) The listing block now carries screen_gate's extracted
    "[key requirements]" tag, previously handed only to the final judge. "rank_v9" was
    bumped from "rank_v8": the prompt gained the candidate's own intent text --
    previously visible only to the final judge, leaving this stage scoring
    function fit against bare role TITLES with no access to what the candidate
    meant by them. The intent hash rides in the gate name so an edit to that box
    re-scores without also invalidating screen_gate, which never sees it.
    "rank_v8" was bumped from "rank_v7": FUNCTION MATCH gained the shared-title carve-out --
    titles like "Automation Engineer" name two different professions, so an exact
    match against a target role is worth nothing without reading the duties. A v7
    score could have been driven by exactly that title agreement and isn't
    comparable. "rank_v7" was
    bumped from "rank_v6": the prompt gained a boilerplate-scope guard telling the
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
    # _v2 for the same reason screen_gate uses it -- see there.
    sig = _profile_signature_v2(profile)
    # "rank_v15" (not "rank_v14"): the gate name doubles as part of the cache key, and
    # _gate_cache_key has no model field -- bumping it forces every previously
    # scored job to be re-ranked under the reworded prompt (new HARD DOWNGRADE (g),
    # MAX LISTING AGE, see the docstring) instead of serving a stale score forever.
    # Bump again if the rank model/prompt changes again.
    #
    # The intent text is folded into the GATE NAME rather than into
    # _profile_signature, which is shared with screen_gate: screen_gate is
    # deliberately never shown intent (see _rank_prompt), so putting it in the
    # shared signature would re-run every cheap screen call for a guaranteed
    # identical answer every time the candidate edits that box. Here it must
    # participate, or an edit to the single most authoritative want-signal would
    # keep serving scores computed without it.
    intent_tag = hashlib.sha1(
        (profile.get("intent_text") or "").strip().lower().encode()
    ).hexdigest()[:8]
    # The max-listing-age preference rides in the gate name for the same reason
    # intent does -- see _rank_age_cache_tag -- so editing it re-scores without
    # re-running screen_gate, which never sees this preference at all.
    age_tag = _rank_age_cache_tag(profile)
    keys = [_gate_cache_key(f"rank_v17.{intent_tag}.{age_tag}", sig, _gate_job_id(c)) for c in candidates]
    cached = _gate_cache_lookup(keys)

    to_judge: list[tuple[dict, str]] = []
    for c, key in zip(candidates, keys):
        if key in cached:
            _keep, reason, _req_json = cached[key]
            # Packed "{score}|{note}|{soft_violation}". A note can never contain
            # "|" (stripped at write time in _score_rank_batch), so a plain split
            # is unambiguous; a 2-field entry is a pre-v16 row and reads as no
            # soft violation, which is also what an unflagged v16 row means.
            fields = (reason or "").split("|")
            try:
                c["_rank_score"] = float(fields[0])
            except (TypeError, ValueError, IndexError):
                c["_rank_score"] = 50.0
            c["_rank_note"] = fields[1] if len(fields) > 1 else ""
            c["_rank_soft_violation"] = len(fields) > 2 and fields[2] == "1"
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
        with ThreadPoolExecutor(max_workers=min(_GATE_MAX_WORKERS, len(batches))) as pool:
            futures = [pool.submit(_score_rank_batch, batch, profile) for batch in batches]
            for batch, fut in zip(batches, futures):
                scores, notes, soft_flags, batch_failed = fut.result()
                for i, (c, key) in enumerate(batch):
                    if batch_failed:
                        # Cosmetic placeholder only -- _rank_gate_failed (not this score) is
                        # what engine.py's floor check actually keys off of. Not cached: a
                        # failure that isn't fully understood yet should retry fresh next
                        # run instead of permanently poisoning gate_cache with no signal.
                        c["_rank_score"] = 50.0
                        c["_rank_note"] = ""
                        c["_rank_soft_violation"] = False
                        c["_rank_gate_failed"] = True
                    else:
                        score = scores.get(i + 1, 50.0)
                        note = notes.get(i + 1, "")
                        soft = soft_flags.get(i + 1, False)
                        c["_rank_score"] = score
                        c["_rank_note"] = note
                        c["_rank_soft_violation"] = soft
                        new_entries.append((key, True, f"{score}|{note}|{'1' if soft else '0'}", None))

    _gate_cache_store(new_entries)
    # Score-distribution diagnostic: a rank stage that never rejects anything
    # is indistinguishable from a healthy one in the old "(N from cache)"-only
    # log line -- this surfaces the actual spread so a run where the mid-tier
    # model is clustering everything above the reject floor is visible without
    # having to separately query gate_cache.
    all_scores = [c.get("_rank_score", 50.0) for c in candidates]
    n_failed = sum(1 for c in candidates if c.get("_rank_gate_failed"))
    n_soft = sum(1 for c in candidates if c.get("_rank_soft_violation"))
    if all_scores:
        emit(f"[gate:rank] scored {len(candidates)} candidates ({n_cached} from cache, "
             f"{n_failed} fail-open/no-signal, {n_soft} soft-preference mismatch) -- "
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
    # A short clause often sits between the noun and "is no longer" -- e.g. a
    # board-stated post date ("This job from 24 Jul 2026 is no longer available
    # for applications."), which the old direct-concatenation pattern missed
    # entirely (a live case: it never matched at all). Bounded to <=40 chars
    # with no sentence break, so this can't reach across into an unrelated
    # sentence and pick up a false positive.
    r"this (?:vacancy|job|role|position|posting|listing)(?:[^.\n]{0,40})? "
        r"is no longer (?:available|active|live)|"
    r"(?:vacancy|position|role) has (?:already )?been filled|"
    r"job (?:posting|listing|advert) has expired|"
    r"(?:this )?posting has been removed|"
    r"sorry,? this job is no longer (?:available|live)|"
    # Both measured on the nijobs.com page an Adzuna tracking redirect resolved
    # to, whose whole visible content was "This listing went offline. Sorry, the
    # listing that you're looking for is expired." Neither clause matched: the
    # "has/have expired" branch above wants the auxiliary verb ("this listing
    # HAS expired"), and nothing covered "went offline" at all. Boards routinely
    # write the copular form, so this was a general gap that happened to surface
    # on Adzuna. The <=40-char no-sentence-break bridge is the same bounded
    # device the "is no longer available" branch above already uses, for the
    # same reason -- here it spans "that you're looking for".
    r"this (?:vacancy|job|role|position|posting|listing|advert(?:isement)?) went offline|"
    # (?![\w-]) so "is expired" cannot match inside a longer hyphenated token --
    # "the role is expired-air-handling maintenance" is a real (if contrived)
    # sentence shape and matched before the guard. Cheap insurance: this branch
    # ends in a word that routinely prefixes compounds, and dead_reason cannot
    # be undone.
    r"the (?:vacancy|job|role|position|posting|listing)(?:[^.\n]{0,40})? "
        r"is (?:expired|no longer available)(?![\w-])",
    re.I,
)


_EXPIRED_LISTING_HEAD_CHARS = 1500   # a real closure notice replaces the posting


# Standing/pipeline ("ghost") advertising: an ad that collects applications
# without a specific open vacancy behind it. Distinct from _EXPIRED_LISTING_RE,
# which is about a posting that HAS closed -- this is about one that was never a
# single vacancy to begin with, and it is a DOWNGRADE signal, never a drop.
_EVERGREEN_LISTING_RE = re.compile(
    r"we(?:'re| are) always (?:looking|recruiting|hiring|interested)|"
    r"always on the lookout|"
    r"join our talent (?:pool|community|network|bank)|"
    r"talent (?:pool|pipeline|bank|community)|"
    r"register your interest|expressions? of interest|"
    r"speculative applications?|"
    r"(?:applications?|candidates?) (?:are )?(?:reviewed|considered) on a rolling basis|"
    r"future opportunit(?:y|ies)|"
    r"no specific (?:vacancy|opening|role) at (?:this|the) (?:time|moment)|"
    r"we accept applications year[- ]round",
    re.I,
)


def _listing_liveness_tag(job: dict) -> str:
    """Bracketed note when the listing's own text reads as a standing/pipeline ad
    rather than one specific vacancy.

    Reads job["full_text"] directly at tag-render time rather than being plumbed
    through the store, so it automatically picks up text an enricher supplied
    mid-run. Deliberately NOT run at discovery on the snippet: 96% of rows carry
    only a ~500-char API teaser, which is the opening marketing blurb, and this
    boilerplate lives in the closing paragraph -- so a discovery-time check would
    both miss nearly everything and, being a drop, violate the downgrade-only
    rule this signal is bound by.

    Unlike the age tag, this IS an earlier pass's reading of text the model can
    see for itself, so it carries no special standing -- the model's own reading
    of the fuller text always wins."""
    text = job.get("full_text") or job.get("snippet") or ""
    m = _EVERGREEN_LISTING_RE.search(text)
    if not m:
        return ""
    quote = " ".join(text[m.start():m.start() + 90].split())
    return (f'[possible standing/pipeline ad: the text says "{quote}..." -- check whether this '
            f'is one specific open vacancy or an always-open talent-pool advert] ')


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
    boilerplate around it, unlike a bare redirect stub.

    The length gate is paired with a POSITION gate rather than simply widened.
    Length alone was the only defence against a long, live page that mentions
    "this vacancy has closed" inside a related-jobs sidebar or a cookie/archive
    footer -- but at 3000 chars it also missed genuinely-dead pages, since the
    live store's scraped pages run to a p75 of 4592 and a p90 of 6079. A real
    closure notice REPLACES the posting and therefore appears at the top, so
    requiring the match to start within the first _EXPIRED_LISTING_HEAD_CHARS is
    strictly narrowing (fewer false positives than the old rule) while letting
    the length ceiling double. dead_reason is unrecoverable, so this must only
    ever move in the conservative direction.

    THE LENGTH CEILING IS NOW GONE and position is the only gate, which sounds
    like a loosening and is the same argument taken one step further. The
    ceiling was only ever a proxy for "the phrase is somewhere incidental" -- a
    related-jobs sidebar, an archive footer -- and it is a bad one, because a
    genuinely dead page can be long: a live case (flexa.careers, an Accenture
    listing that reached rank 6 on the results page) kept the ENTIRE job
    description rendered below the closure notice, at 16k chars of markdown, so
    the ceiling hard-blocked it however the phrase was worded. Position answers
    what the length was proxying for, and answers it directly.

    What makes that safe is the measurement that also removed the original
    reason for the ceiling. The bebee false positive it was defending against
    was never in the page's TEXT -- it was in the inlined <script>/<style> that
    _strip_html leaves behind, which is why the verification path now reads
    _visible_text instead (see there). Re-measured over 382 live pages -- the
    376 scraped pages in the store carrying real text, plus 6 live bebee
    postings of 3.0k-6.6k visible chars fetched fresh -- this pattern matches
    ZERO of them at any offset. The two bebee rows that did match were both hard
    404s, at offset 36, and check 1 catches those anyway. So the head window is
    the only guard left and it is the load-bearing one: the opening of a page is
    its title, company and header, and a LIVE posting cannot say there that it
    is no longer available. Don't widen it, and don't feed this raw HTML."""
    m = _EXPIRED_LISTING_RE.search(markdown)
    return bool(m) and m.start() < _EXPIRED_LISTING_HEAD_CHARS


# Some ATS/company career pages 301-redirect a closed/removed requisition's URL
# straight to the employer's general careers/jobs-index page instead of a 404 --
# a normal 200 OK, well past the redirect-stub length gate, so neither
# _looks_like_redirect_stub nor _looks_like_expired_listing (which needs an
# explicit closure PHRASE, not just "this isn't the job you asked for") catches
# it. A live case: a scrape for CFC's "Associate Data Scientist" landed on CFC's
# general careers material, and the final judge could only note the mismatch as
# a concern ("the supplied page contains CFC's general careers material rather
# than the ... specific duties") rather than disqualify, because nothing marked
# the listing as unverifiable/likely-closed -- it graded "Ok fit" off a page
# that was never actually about this role.
_GENERIC_CAREERS_HUB_RE = re.compile(
    r"(?:browse|view|explore|search) (?:all )?(?:our )?(?:current |open |available )?"
        r"(?:job|role|vacanc|career|position)|"
    r"current (?:job )?(?:vacancies|openings|opportunities)|"
    r"no (?:current |open )?vacanc(?:y|ies) (?:match|found)|"
    r"join our (?:talent|team)\b|"
    r"see all (?:our )?(?:jobs|vacancies|roles|openings)",
    re.I,
)
_GENERIC_CAREERS_HUB_MAX_CHARS = 6000    # kept: this rule has no 382-page miss
                                         # measurement behind it, unlike the
                                         # expired-phrase gate above
_GENERIC_CAREERS_HUB_HEAD_CHARS = 1500   # a careers-index page leads with its own nav, same as a closure notice


def _title_present(markdown: str, title: str) -> bool:
    """Whether the job's OWN title (or a close paraphrase) appears anywhere in
    the scraped text. A genuine job-detail page almost always names its own
    role at least once (H1/breadcrumb/opening line, however much site chrome
    surrounds it) -- its complete absence is the distinguishing signal a
    generic careers-index page doesn't share. Requires all but one significant
    title word to appear (order-independent), so a minor paraphrase ("Associate
    Data Scientist" vs "Data Scientist, Associate") doesn't trip a false
    positive. An empty/degenerate title has nothing to check, so it never
    counts as "absent" -- that would make the generic-hub check fire on
    title-less rows for an unrelated reason."""
    sig_words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 2]
    if not sig_words:
        return True
    text = markdown.lower()
    hits = sum(1 for w in sig_words if w in text)
    return hits >= max(1, len(sig_words) - 1)


def _looks_like_generic_careers_hub(markdown: str, title: str) -> bool:
    """True if the page reads as the employer's general careers/jobs-index
    rather than this specific job's own listing. Gated on BOTH conditions to
    stay conservative (dead_reason is unrecoverable, same as the checks above):
    generic careers-index boilerplate near the top of the page, AND the job's
    own title never mentioned anywhere at all. Either alone is too weak --
    plenty of genuine JD pages carry a "browse our other roles" footer, and a
    title can legitimately be paraphrased -- but the combination (index
    language up front, and the specific role never named even once) is not
    something a real single-vacancy page produces."""
    if len(markdown) >= _GENERIC_CAREERS_HUB_MAX_CHARS:
        return False
    m = _GENERIC_CAREERS_HUB_RE.search(markdown)
    if not m or m.start() >= _GENERIC_CAREERS_HUB_HEAD_CHARS:
        return False
    return not _title_present(markdown, title)


def _dead_listing_signal(result, markdown: str, title: str = "") -> str | None:
    """A short machine-readable reason ('status_404'/'status_410'/
    'expired_phrase'/'generic_hub') ONLY when this fetch is a HIGH-CONFIDENCE
    signal the listing itself is gone -- as opposed to an ambiguous failure
    (anti-bot block, rate-limit, timeout, generic 4xx/5xx, empty shell) that
    must stay on the existing fail-open snippet-fallback path. 403/429/5xx are
    deliberately excluded: those mean "blocked/rate-limited/erroring", not
    "gone". Only 404/410 (HTTP-spec "not found"/"permanently gone"), an
    explicit closure-phrase match, or landing on the employer's general
    careers hub instead of this job's own page, count."""
    status = _effective_status_code(result)
    if status in (404, 410):
        return f"status_{status}"
    if markdown and _looks_like_expired_listing(markdown):
        return "expired_phrase"
    if markdown and _looks_like_generic_careers_hub(markdown, title):
        return "generic_hub"
    return None


def _scrape_succeeded(result, markdown: str, title: str = "") -> bool:
    """Whether this fetch counts as a real-posting scrape success. A >=400
    status is NEVER a success regardless of markdown length or content --
    crawl4ai has no way to know an anti-bot/soft-404 error page's rendered
    text isn't real content."""
    status = _effective_status_code(result)
    if status is not None and status >= 400:
        return False
    return bool(result.success and markdown and len(markdown) > 150
                and not _looks_like_redirect_stub(markdown)
                and not _looks_like_expired_listing(markdown)
                and not _looks_like_generic_careers_hub(markdown, title))


def _scrape_worth_retrying(e: Exception) -> bool:
    """Whether a failed scrape attempt is worth a second try. Only the transient
    "empty shell / incomplete markup" case is -- a page whose JS hadn't finished
    hydrating on the first pass can render on a retry (that's the
    "[RETRY SUCCESS] Bypassed script wall" path). A navigation/anti-bot TIMEOUT
    (crawl4ai's "Failed on navigating ACS-GOTO ... Timeout Nms exceeded",
    typically an aggregator redirect wall like jobviewtrack.com) never resolves
    on retry -- it just burns another full page_timeout, historically the single
    biggest waste in Phase 5 -- so we give up on it immediately and fall straight
    through to the alt-source / snippet fallback. Dead-listing and redirect-stub
    ValueErrors are terminal too: retrying a confirmed-dead or click-tracking
    page recovers nothing."""
    msg = str(e).lower()
    return "empty page shell" in msg or "incomplete markup" in msg


def _is_antibot_timeout(e: Exception) -> bool:
    """A navigation/anti-bot timeout -- crawl4ai's "Failed on navigating ACS-GOTO
    ... Timeout Nms exceeded", typically an aggregator redirect/anti-bot wall.
    These pages already burned the full page_timeout and reliably never resolve, so
    the caller also skips the alt-source lookup for them: re-searching for the same
    posting almost never recovers an anti-bot-walled aggregator repost and just
    spends another ~20s page load. Empty-shell/dead/redirect-stub cases are
    excluded here so they still get their alt-source attempt."""
    msg = str(e).lower()
    return "timeout" in msg or "navigating" in msg


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
            if _scrape_succeeded(result, markdown, job.get("title", "")):
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
    total_budget_seconds: float = SCRAPE_BUDGET_SECONDS, country_code: str = "gb",
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
                        page_timeout=SCRAPE_PAGE_TIMEOUT_MS,
                        markdown_generator=DefaultMarkdownGenerator(content_filter=PruningContentFilter()),
                    )

                    result = await crawler.arun(url=url, config=run_config)
                    markdown = _best_markdown(result)
                    dead_signal = _dead_listing_signal(result, markdown, job.get("title", ""))

                    if _scrape_succeeded(result, markdown, job.get("title", "")):
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

                except Exception as e:
                    # Give up now if this is the last attempt OR the failure is one
                    # a retry can't fix (anti-bot navigation timeout, dead listing,
                    # redirect stub). Only the transient empty-shell case loops back
                    # for the cool-down retry -- see _scrape_worth_retrying.
                    if attempt < max_retries and _scrape_worth_retrying(e):
                        emit(f"   [BLOCKED/SHELL] Cool-down applied for {job['company']} (Attempt #{attempt}). Retrying...")
                        continue
                    alt_text = ""
                    # Skip the alt-source lookup on an anti-bot timeout -- re-searching
                    # an anti-bot-walled aggregator repost almost never recovers it and
                    # just spends another ~20s page load. Fall straight to snippet.
                    if alt_budget[0] > 0 and not _is_antibot_timeout(e):
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
                    break

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
        emit(f"   [phase 5] confirmed {dead_confirmed} listing(s) dead/expired (404/410, an explicit "
             f"closure notice, or a redirect to the employer's general careers hub, no alternate "
             f"posting found) -- excluded before the final judge")
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
# 28 also adds a THIRD listing-age severity to the WHAT THE BRACKETED HINTS ARE
# paragraph. The tag had two (STALE = open for months, downgrade-only; ELIMINATE
# = past a HARD maximum listing age, DISQUALIFIER 8), and a SOFT maximum matched
# neither -- so a listing confirmed past the number the candidate themselves set
# was read as ordinary staleness, i.e. "one step down IF the grade is borderline".
# Measured on a live 7-day Soft profile: 18 of 36 shown roles were over it and 9
# past DOUBLE it, at ranks 1-12 (a 22-day listing at rank 1, a 28-day at rank 5).
# The engine-side _selection_score demotion added alongside this (see
# engine.STALE_SELECTION_PENALTY) only reorders the judge POOL -- a role well
# above the floor still reaches the judge, which then had no rule telling it to
# care -- so the grade itself had to take the hit. Expressed through the EXISTING
# mechanical rubric rather than as a parallel adjustment: over the stated maximum
# counts as ONE concern touching a core requirement, more than double counts as
# TWO (capping the role at "ok"). Never excludes, and explicitly never a reason to
# move a role out of "backup". Folded into 28 rather than taking a 29 because 28
# has not been run in production, so this costs no extra store-wide re-judge.
# 28 (from 27): the step-D checklist rework. The checklist is the INPUT the
# fit_level rubric and "concerns" are both derived from, so anything missing from
# it is silently invisible downstream -- and it had been shrinking, run over run,
# while the postings got LONGER:
#     run 20  22 judged, 0 backup, text median 2926 -> checklist mean 6.00 items
#     run 21  14 judged, 2 backup, text median 3232 -> 4.33
#     run 22  31 judged, 18 backup, text median 2578 -> 4.33
#     run 23  40 judged, 12 backup, text median 4919 -> 3.75
#     run 24  40 judged, 20 backup, text median 4114 -> 3.67
# Across 160 persisted checklists the median is 4 items / 3 core, only 20 of them
# reach this prompt's own stated "4-10 core, 2-6 secondary", 46 carry NO secondary
# item at all, and size barely tracks the posting's length (pearson r = 0.25;
# a 6k-char JD gets a median of 5 items against a sub-1k JD's 3). Re-measure with
# scripts/audit_judge_checklists.py.
# The cause is OUTPUT PRESSURE, not a wrong rule, and tests/judge_harness.py
# reproduces it causally: the unmodified v27 prompt, same profile, same model,
# scores 6.33 checklist items at 16 jobs/9 picks per call and 5.15 at 20 jobs/13
# picks. judge_pool_size went 22 -> 40 (the cap) over those runs and v26 widened
# "backup" from 3 to FINAL_PICKS, roughly doubling the number of full card outputs
# one call has to produce, while judge completion tokens rose only sub-linearly
# (415 -> 368 per job). Faced with that, the model economised on the one field
# v27 opened by calling "internal reasoning only -- not shown to the candidate":
# the checklist. It is the worst possible field to cut, because the rubric reads
# "core" items ONLY -- a short checklist yields an INFLATED grade, not a cautious
# one. Live examples: a 4,979-char JD naming Node.js/TypeScript, Ruby/Rails, Nuxt,
# AWS CDK and Salesforce produced a ONE-item checklist ("Full stack / software
# engineering", met) graded "strong" with zero concerns.
# Five changes, all in step D and the rubric, and every one of them makes a v27
# checklist non-comparable rather than merely differently-worded:
#   * The "internal reasoning only" framing is GONE, replaced by what the field
#     actually is: the input the grade and concerns are computed from, so dropping
#     an item does not simplify the answer but silently changes it toward
#     flattering the role. Prose is now named explicitly as what to shorten first
#     when a response runs long.
#   * New rule ONE ITEM PER ASK -- NEVER A HEADING THAT SWALLOWS SEVERAL, with the
#     "could a competent person in this field plausibly FAIL this item?" test. The
#     existing specificity rule did not catch the Bionic case: "Full stack /
#     software engineering" is not a CAPACITY ("ability to ...") and reads as a
#     legitimate skill name, so it slipped past a rule aimed at "ability to learn".
#   * New rule THE POSTING'S OWN HEADING DECIDES core. A live pick tagged the two
#     asks under "Must have hands on experience with SQL, and data visualisation
#     tools" as SECONDARY while promoting two Key-Responsibilities duties to core
#     -- which, since the rubric reads core only, made the posting's own stated
#     bar unable to affect the grade at all.
#   * The "[key requirements]" anchoring paragraph gained THE HINT IS A FLOOR, NOT
#     A CEILING. The hint pass reads the truncated OPENING (the blurb); the
#     requirements section sits further down in text only the judge can see, so a
#     checklist matching the hint and adding nothing is the symptom of skipping
#     the ADD step. Names the two things measured as most often lost: a named
#     platform/tool the role runs on, and a stated years-of-experience bar.
#   * The fit_level rubric now states the dependency in the direction that
#     matters: reading core items only means an under-tiered checklist inflates
#     the grade rather than making it cautious.
# 27 (from 26): new DISQUALIFIER 9, PASSED APPLICATION DEADLINE. The per-call user
# message (never _FINAL_EVAL_SYSTEM -- that string must stay byte-identical across
# calls, see _FINAL_EVAL_CACHE_KEY) now opens with "Today's date: YYYY-MM-DD", and rule
# 9 excludes a role whose listing states an explicit deadline before that date, even
# when the listing's own wording is still forward-looking ("Apply by 9 August 2026")
# rather than an explicit closure notice -- a listing keeping its original wording
# after the date quietly passes is the normal case, not evidence it's still open. Before
# this the judge had no notion of the current date at all, so it could extract a
# "deadline" fact for display (the schema already asked for one) without ever being
# able to notice the date had passed: a live case (Met Office "Junior Software
# Developer" via jsearch, no structured expires_at from the source, JD stating "Apply
# by 09/08/2026") was graded "strong fit" and shown after that date, with "deadline":
# "09/08/2026" sitting right there in the model's own output. A v26 verdict was reached
# by a model with no access to the current date and cannot be reused.
# 26 (from 25): the leniency/output rework. Four changes, all of which make a v25
# verdict non-comparable rather than merely differently-worded:
#   * "backup" stopped being a 3-item last-resort list of "least-bad survivors" and
#     became the second half of the result set (capped at FINAL_PICKS, explicitly
#     described as SHOWN to the candidate). Roles that used to be filed under
#     "not_selected" -- which carries only an internal audit phrase and therefore
#     cannot be displayed at all -- now belong in "backup" whenever nothing is
#     actually wrong with them. engine._evaluate_cluster contributes both tiers
#     together to match.
#   * A WISH-LIST bar paragraph attached to that list: agency-posted, contract, and
#     long-"essential"-list/negotiable-pay postings are named as concrete tells that
#     the stated requirements are a recruiter's ideal-hire sketch rather than a bar,
#     and push toward including a role and toward the more generous of two adjacent
#     fit_levels. Explicitly NOT a licence to mark an unmet requirement met or to
#     soften a DISQUALIFIER.
#   * "sector_match" and reasoning step G are GONE, along with DISQUALIFIER 5's
#     closing "judged separately as a ranking signal" paragraph and the sector
#     clauses in steps B/E and the fit_level rubric. The field's only user-visible
#     effect was a "this is not within your stated ... sector interests" line in
#     "concerns" that read as noise, and it also silently gated "very_strong". The
#     rubric's very_strong condition changes as a direct result, so every v25 grade
#     was computed under a different rule.
#   * "concerns" is capped at 3 items (was uncapped in the prompt, 4 in effect), and
#     a "strengths" list is now REQUIRED for an "ok"/"stretch" pick so the card can
#     show what the candidate does bring alongside what they don't.
# 25 (from 24): new DISQUALIFIER 8, MAX LISTING AGE -- replaces the old fixed
# STALE_LISTING_DAYS=45 downgrade-only mechanism with the candidate's own configurable
# "Maximum listing age" preference (default 30 days, see
# backend/app/config.DEFAULT_MAX_LISTING_AGE_DAYS), enforced Hard by default: a listing
# DEFINITELY known to be older is now excluded outright rather than merely nudged down a
# few points, mirroring how CLOSED LISTING/LOCATION are hard rules elsewhere. The
# WHAT THE BRACKETED HINTS ARE paragraph now distinguishes the tag's "ELIMINATE"
# severity (a confirmed fact, feeds this new disqualifier) from plain "STALE" (unchanged,
# downgrade-only) -- deliberately worded profile-independently (naming no specific day
# count or Hard/Soft state) since this system prompt is shared byte-for-byte across every
# profile/run to keep its 24h prompt-cache retention paying off; the actual threshold and
# Hard/Soft state ride in the per-run CV text (snapshot.build_snapshot's "Maximum listing
# age" line) and the per-job age tag instead. A v24 verdict was reached under a rule that
# could never exclude a role for this no matter how old it definitely was, and cannot be
# reused. This also naturally re-opens the whole store once, since the age-tag threshold
# itself moved (45 -> each candidate's own setting, default 30).
# 24 (from 23): the WHAT THE BRACKETED HINTS ARE paragraph now explains the age tag's
# TWO clocks (the board's claimed posting date vs how long this system has been
# finding the ad) and what a "refreshed" posting date means, and adds the new
# "[possible standing/pipeline ad: ...]" hint as a verify-in-the-text concern -- a
# talent-pool advert with no vacancy behind it. Both are grade-nudging concerns and
# neither is a disqualifier, deliberately: the ghost signals are downgrade-only. A v23
# verdict was reached with neither hint in the prompt and cannot be reused.
# 22 (from 21): DISQUALIFIER 2(a) GEOGRAPHY now defers to the profile's new "Location
# search scope" CV line (snapshot.build_snapshot) instead of judging bare distance
# between the candidate's stated place and the listing's. A "national"/"international"
# scope means the candidate deliberately opted into a country-wide/worldwide search
# (see location_scope in CLAUDE.md/snapshot.py), but v21 GEOGRAPHY had no access to
# that and would still exclude an on-site/hybrid role for merely being a different,
# distant city in the candidate's OWN country -- a live case rejected a Newcastle
# hybrid role for a Southend candidate searching nationally as "geographically
# impractical", when national scope exists precisely to allow that. A v21 verdict was
# reached under a rule that could not tell "far away" from "outside the candidate's
# declared scope" apart.
# 21 (from 20): DISQUALIFIER 2 (LOCATION/VISA/RELOCATION) split into a geography
# check and a stated-work-arrangement check. Under v20 the rule ended "If location is
# remote ... do not raise a location objection", so a fully-remote listing was waved
# through for every candidate, whatever they had stated -- and NO LOCATION COMMENTARY
# forbade even mentioning it in "concerns", so the judge could neither reject nor flag
# it. A v20 verdict was reached under a rule that could not fail a remote role.
FINAL_EVAL_PROMPT_VERSION = 28

_FINAL_EVAL_QUOTE_PROTOCOL = """QUOTE-THEN-CLASSIFY (applies to every disqualifier below before you exclude a role under
it): quote the exact clause you're relying on, verbatim, max 20 words, then classify it HARD
(stated as mandatory -- "must have", "required", "X+ years required", a named eligibility
restriction) or SOFT (a preference or ideal-candidate sketch -- "would suit", "ideal for",
"we'd love", "looking for someone with roughly", "a nice to have"). THIS VOCABULARY IS ALWAYS
SOFT, whatever follows it and however central the thing sounds: "ideally", "preferably",
"desirable", "desired", "advantageous", "an advantage", "a plus", "a bonus", "beneficial",
"welcome", "we'd like to see", "experience with X would be great", "familiarity with X is
helpful", "X or Y a plus". A list of named tools introduced by any of those words (e.g.
"ideally Databricks or Snowflake") is a wish-list, not a bar. "Would suit X" / "ideal
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
   This rule is about the role demanding MORE than the candidate has. The opposite direction - a role
   pitched BELOW the candidate's level - is never a disqualifier under this rule. If the profile above
   carries an "Open to more junior roles" line, the candidate has explicitly said they will consider
   such roles: do not exclude one for being junior, do not raise it as a concern, and do not let it
   lower "fit_level". That line changes NOTHING about DISQUALIFIER 3's OVER-QUALIFIED FOR A TRAINING
   SCHEME paragraph - an apprenticeship or student placement is excluded because the candidate is
   INELIGIBLE for it, not because it is junior, so keep applying that rule exactly as written.
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
   Then run TWO separate checks. They fail in opposite directions, and passing one is not passing
   the other.
   (a) GEOGRAPHY -- can the candidate physically take it? If the role's location under that
   classification (or an explicit on-site/relocation/visa/work-authorization requirement in the text)
   clearly puts it outside where the candidate can realistically work, exclude it. A remote role, or
   one plainly compatible with the candidate's location above, passes this check. If the profile
   carries a "Location search scope" line, it defines what "outside where the candidate can
   realistically work" means and OVERRIDES a bare distance/city/country reading -- a "national" or
   "international" scope means an in-country (respectively, any-country) on-site/hybrid role is NOT
   geographically impractical merely for being far from the candidate's stated place; per that line,
   GEOGRAPHY then fails only for what it explicitly still allows (e.g. a different country under
   "national" scope, or an unmet visa/right-to-work requirement under either). Never fail GEOGRAPHY
   on distance alone when that line says the candidate opted into a country-wide or worldwide search.
   (b) STATED WORK ARRANGEMENT -- is it the arrangement they asked for? The candidate profile may
   carry a "Work arrangement" line listing the arrangements they want (On-site / Hybrid / Remote,
   any combination). If it does, the listing's classified arrangement must match at least ONE of
   them. A HYBRID listing matches any of the three (it has both office days and remote days). A
   fully REMOTE listing matches ONLY a candidate who listed Remote -- being remote makes a role
   geographically workable for everyone, but a candidate who listed On-site and/or Hybrid and did
   NOT list Remote has stated they want office presence, and a remote-only role offers none. Do not
   read "Hybrid" on the CANDIDATE's side as a wildcard. When that line marks the preference as
   binding, a listing matching none of the stated arrangements is a disqualifier, exactly like a
   geography failure; when it marks it as a preference only, keep the role, but when choosing which
   roles go in "strong" prefer an otherwise-comparable one that does match. Either way this is
   handled here and via the "work_style" fact -- NO LOCATION COMMENTARY below still applies, so it
   never appears in "concerns" or any other prose field. If the profile states no work arrangement at all, or
   you could not confidently classify the listing's arrangement, this check does not apply -- never
   invent an arrangement objection from silence.
   If the listing's own location/eligibility signals are internally contradictory (e.g. a "compatible
   timezone" framing alongside an explicit country-selector or eligibility list that excludes the
   candidate's country), do not silently resolve the contradiction either way - keep the role but add a
   concern naming the specific contradiction so the candidate can verify eligibility before applying.

3. LISTING TYPE: The listing must be a real, direct job vacancy the candidate could be hired into. If
   it is actually a paid training course, "traineeship"/placement programme, bootcamp, or any scheme
   where the candidate enrols in (or pays/finances) training and is only promised a job or interview
   afterwards rather than being hired directly, exclude it entirely - it is not a job.
   OVER-QUALIFIED FOR A TRAINING SCHEME. Separately from the above, a formal apprenticeship,
   traineeship or structured training scheme IS a real job (the candidate is employed while training),
   so it is not excluded by the paragraph above -- but it is a place on a course, and it exists to teach
   someone who does NOT yet hold the qualification or the skills. Exclude it when the candidate already
   holds a qualification at or above the level the scheme awards (for a degree-holder: any below-degree
   scheme -- Level 2-5, "advanced"/"higher" apprenticeship) AND their evidence already covers the core
   things it says it will train them in. Matching such a scheme's skill list closely makes it a WORSE
   fit, not a better one, and many carry an explicit eligibility bar against applicants already holding
   an equivalent qualification (quote it when the text states one). A training rate of pay ("National
   Minimum Wage", "apprentice rate") corroborates this but is not required. NOT covered: degree
   apprenticeships, Level 7/master's-level schemes, graduate schemes and graduate programmes -- those
   hire at the candidate's own level and are judged as normal roles. Also not covered: an apprenticeship
   in a field the candidate genuinely lacks, which is a real opportunity for them.

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

5. PROFESSIONAL FIELD FIT: Exclude a role whose core professional domain or job function is clearly in a
   different field from what the candidate is targeting - judged against their target roles and their
   own words about what they are looking for (both in the profile above).
   Use a HIGH bar: only genuinely unrelated professional fields disqualify (e.g. a hands-on nursing
   role for a marketing candidate, a field-sales role for someone targeting research/policy, a
   qualified-accountant role for a software engineer). Do NOT exclude adjacent, transferable, or
   specialisation-level differences within the same broad field, or a role that plausibly applies the
   candidate's core skills in a new domain - those are normal and acceptable. When the candidate targets
   more than one distinct field, judge sector fit against the NEAREST one, never penalise a role for not
   matching their OTHER field. If genuinely unsure whether the field is unrelated, do not raise a sector
   objection.
   SHARED / AMBIGUOUS JOB TITLES: a number of titles name two genuinely different professions and can
   only be told apart by the duties described - e.g. "Automation Engineer" (software test/RPA/pipeline
   automation) versus (industrial control systems, PLCs, robotics, plant machinery); "Analyst" (data)
   versus (financial, intelligence, business-process); "Engineer" (software) versus (mechanical,
   electrical, civil); "Designer" (product/UX) versus (mechanical, graphic); "Architect" (software)
   versus (buildings). For any such title, a WORD-FOR-WORD match between the listing's title and one of
   the candidate's target roles is NOT evidence the field matches - it is exactly the case this rule
   exists for. Decide the field from the duties the listing actually describes, and from the domain the
   candidate's own evidence sits in; when those turn out to be different professions, exclude the role
   under this rule however precisely the titles agree, and say so in the "disqualified" reason. Being a
   supported graduate/trainee entry route into the other profession does not change this: it makes the
   role a career change, which is a different question from fit, and one the candidate has not asked for
   unless their own words say so.
   This rule is about the PROFESSIONAL FIELD ONLY, never the industry, sector or cause the employer
   happens to operate in. A same-function role at an employer in an industry the candidate has never
   mentioned is a normal, good match. Never raise an industry/sector/cause objection anywhere in your
   output - not here, not in "concerns", not in a "not_selected" reason.

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
   candidate to verify. If the profile states no such hard filters, this rule does not apply.

8. MAX LISTING AGE: The candidate's profile may state a "Maximum listing age" as either a hard limit or a
   preference (see the profile above for which, and the number of days). This rule applies ONLY when it is
   stated as a HARD limit - a preference is never a disqualifier, see the age-tag guidance above instead
   (STALE vs ELIMINATE). Exclude a role under this rule only when you have CLEAR evidence it is definitely
   older than that many days: either the job's "[listing age: ...]" tag explicitly marks it "ELIMINATE" (a
   fact from this system's own data, not something to re-derive), or the posting's own text states an
   explicit posting/opening date, or unambiguous staleness ("originally posted", a dated "last updated"
   notice, a specific month/date long past) that itself works out to more than that many days ago. Silence,
   a vague sense that a posting "feels old", or a closing/deadline date alone (a different concern - see the
   age-tag guidance above) never trigger this rule on their own. If the profile states no maximum listing
   age at all, or the tag/text gives you nothing definite to go on, this rule does not apply.

9. PASSED APPLICATION DEADLINE: The candidate's own message below states TODAY'S DATE. If the listing's
   text states an explicit application deadline or closing date (e.g. "Apply by 9 August 2026", "Closing
   date: 09/08/26", "applications close Friday 7 August") and that date is BEFORE today's date, exclude the
   role - the vacancy is no longer open to applications. This fires from the date alone: the listing keeping
   its original forward-looking wording ("Apply by ...") after the date has quietly passed is the NORMAL
   case, not evidence the listing is still open, so do not require backward-looking closure language
   ("no longer accepting applications") for this rule - that phrasing is covered separately above. Work out
   the calendar date the listing states and compare it to today yourself. Never fire this from a vague or
   relative deadline ("rolling basis", "ongoing", "apply soon"), from silence, or from a deadline that is
   today or still in the future."""

_FINAL_EVAL_WORDING = """WORDING -- WHOSE SIDE A SHORTFALL IS STATED FROM. In "can_do_fit", "concerns" and "not_selected"
reasons, describe a gap as something the POSTING asks for or prefers, never as a deficiency in the
candidate. Job descriptions routinely list an ideal hire rather than a bar (see the "backup" rules), so
"the role prefers X" is both the more accurate statement and the one a candidate can act on; "you would
be a stretch because the role expects X" states the same fact as a verdict on the person. Never write
"you would be a stretch", "you lack", "you fall short", "you are under-qualified", "you do not meet", or
"you are not a fit". Write the same content as "the posting prefers X", "the posting asks for X", "they
have listed X as essential", "this one leans more on X than your evidence covers". Where the candidate
genuinely does clear a bar, say so plainly in the same register.

WORDING: When you reference the candidate's OWN background in "summary", "highlight" or
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
status in "summary", "role_type", "can_do_fit", "concerns", or "highlight" - that's already shown
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
   stated - do not present it as satisfying the requirement in "can_do_fit" or "highlight". Put
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
   - want-fit: does the candidate actually WANT this role -- judged against their target roles and their
     OWN words about what they're looking for? A role the candidate is
     well-qualified for but clearly does NOT want (wrong function, a domain they've moved away from,
     something their own words rule out) is NOT a strong fit however well the skills line up. A strong
     can-do-fit must never paper over a weak want-fit. This assessment isn't reported in its own field --
     it feeds the strong/backup decision, and a want-fit mismatch worth flagging goes in "concerns".
     Judge this on the FUNCTION and the day-to-day work only. The employer's industry, sector or cause
     is NOT a want-fit signal and must never lower a grade or appear as a concern.
   - can-do-fit: can the candidate actually DO the job to the REAL bar from A -- weighing evidence
     STRENGTH, not mere presence (see EVIDENCE STRENGTH). "Used professionally, 2 years" is strong
     evidence; "self-directed, one project" is weak evidence for the very same skill tag. Report this
     as a direct, second-person verdict in "can_do_fit" (e.g. "You're mostly qualified for this role,
     though..." or "You cover most of what this role asks for, though the posting prefers ..."), the
     way you'd tell the candidate to their face -- see WORDING for how to phrase a shortfall.
   A role belongs in "strong" only when BOTH want-fit and can-do-fit are genuinely strong.
C. List the notable gaps in "concerns", ONE item per gap (a missing requirement, weak evidence for a
   load-bearing skill, a seniority gap, a want-fit mismatch worth flagging) -- put the single one most
   likely to sink this application FIRST, since the candidate sees these before they expand the list.
   AT MOST THREE ITEMS. If you have more than three, keep the three most likely to sink the application
   and drop the rest: a list longer than that stops being a set of things to address and reads as a
   verdict that the candidate should not bother, which is the opposite of what a shown role means.
   NEVER list the employer's industry, sector or cause as a concern -- see rule 5 and step B.
   A gap the POSTING ITSELF says it doesn't screen on -- one it trains for, or labels beneficial/
   desirable/not essential (step D's fourth rule) -- is only worth listing when it is genuinely material,
   must never be listed first, and must carry the JD's own framing in the same breath (e.g. "no prior use
   of their in-house analytics platform, though the posting says training is provided"). An unqualified
   "no evidence of X" for an X the employer has said it will teach reads to the candidate as a rejection
   on a requirement that was never asked of them, and it is the fastest way to talk a viable application
   out of an honest fit.
D. Build a REQUIREMENTS CHECKLIST. This list is not shown to the candidate, but it is NOT optional
   working-out and it is never the field to economise on: "fit_level" and "concerns" are both derived
   MECHANICALLY from it (see the rubric in the schema), so an ask you leave off the checklist cannot
   become a concern and cannot move the grade, however plainly the posting states it. Dropping an item
   does not simplify your answer -- it silently changes it, always in the direction of flattering the
   role. When you are judging many postings at once and the response is getting long, shorten "summary",
   "highlight" and your other prose FIRST; the checklist is the one thing every other field depends on.
   List the JD's individually-judgeable requirements (both explicitly
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
   WRITE EACH REQUIREMENT AS THE JD STATES IT, at the JD's own level of specificity -- never as an
   abstraction the candidate happens to satisfy. This is the single most common way this judgment goes
   wrong: the checklist gets drafted AFTER an impression has already formed, pitched at whatever level
   makes every item "met", and then reports a clean sheet for a role the candidate plainly could not do.
   The rules that prevent it:
   - THE SHAPE TEST, applied to each item as you write it: could a competent person in this field,
     looking at this posting, plausibly FAIL this item? An item nobody could fail always comes back
     "met", the rubric counts it toward "every core requirement met", and it therefore INFLATES the
     grade -- padding a checklist and under-filling one do the same damage by opposite routes. This is
     a test of an item's SHAPE, not a licence to shorten the list: an unfailable item is nearly always
     one of exactly two things, and the fix is to REPLACE it, not to delete it and move on. If it is a
     HEADING over several asks, list the individual asks underneath it instead. If it is a CAPACITY OR
     AN ATTITUDE, look for the concrete ask the posting states nearby and list that. Both shapes are
     covered separately below. A checklist that gets SHORTER when you apply this test has been pruned
     rather than corrected -- go back and find what the unfailable item was standing in for.
   - ONE ITEM PER ASK -- NEVER A HEADING THAT SWALLOWS SEVERAL. If the posting names Node.js/TypeScript,
     Ruby/Rails, Nuxt and AWS CDK, that is four items, not one "full stack / software engineering". An
     item that restates the role's whole field, or effectively repeats the job title, is unfailable for
     anyone in that field and hides exactly the differences that decide the application -- a Python/
     FastAPI candidate and a Ruby/Rails one both "meet" it. This is the same error as the widening rule
     below, but it is easier to miss, because such an item does not read as vague: it reads as a
     perfectly respectable skill name. The test is whether a competent person in this field could
     plausibly FAIL the item. If not, it is a heading, and the individual asks underneath it are the
     requirements.
   - NOT A CAPACITY OR AN ATTITUDE. "Ability to learn X", "willingness to train", "interest in Y",
     "graduate-level technical foundation", "analytical problem-solving", "eagerness" and the like are
     not judgeable requirements, because no candidate can fail them -- never write one as a checklist
     item. THIS HOLDS EVEN WHEN THE POSTING PRINTS IT UNDER "Requirements" OR "Essential", which most
     postings do: "strong attention to detail", "excellent communication skills", "a genuine interest in
     data", "able to work independently and as part of a team", "a proactive mindset" are boilerplate
     every advert carries and no applicant is ever screened out on. Leave them off entirely -- do not
     demote them to "secondary", which still puts an unfailable "met" on the list. Dropping one is only
     half the job: this posting screens on SOMETHING, so replace it with the failable asks in the same
     text -- the named tools, systems, platforms, deliverables, qualifications and stated experience
     bars, which is where the real requirements always are. Ending up with a SHORTER checklist than you
     started is a sign you deleted the boilerplate without going to look for what it was covering.
     Where the JD says the person will be TRAINED in X on the job, the judgeable requirement is
     still X itself ("met": false when there is no evidence of X); the training on offer is a fit
     argument for step E, not a reason the requirement is met.
   - KEEP THE JD'S OWN SPECIFICITY. If it asks for a Computer Science degree, the item is "Computer
     Science degree", not "a related technical degree". If it asks for PLC/control-system experience,
     the item is that, not "systems experience". If it asks for ETL work, the item is "ETL", not "data
     transformation". Widening the ask until the candidate's profile covers it is the same error as
     marking it met with no evidence, and is harder to spot afterwards.
   - THE DOMAIN-DEFINING ASKS ARE ALWAYS "core". Whatever the person actually spends most days doing --
     the named technical domain, the named tooling, the subject matter that makes this job the job it is
     -- is core by definition and cannot be filed as "secondary" because the candidate lacks it. If you
     find yourself putting the role's central technical subject in "secondary" while the core list holds
     only general aptitudes, stop: that is the shape of a role the candidate is not actually equipped
     for, and the checklist is being bent to hide it.
   - THE POSTING'S OWN HEADING DECIDES core, AND IT OUTRANKS YOUR IMPRESSION. An ask printed under
     "Requirements", "Experience required", "Essential", "What you'll need", or introduced by "must
     have"/"required", is "core" -- unless the SOFT vocabulary in QUOTE-THEN-CLASSIFY applies to that
     specific ask, or one of the three cases in the next rule does. A duty lifted from a
     "Responsibilities"/"Key responsibilities"/"What you'll be doing" list is a description of the work,
     not a screening bar, and must never be tagged "core" IN PLACE OF an ask the posting explicitly
     required. Getting this backwards is as damaging as leaving the ask off the checklist altogether and
     is much harder to see, because the item is right there on the list: the fit_level rubric reads
     "core" items ONLY, so a required ask filed as "secondary" cannot lower the grade no matter how
     plainly it is unmet. A posting whose "Experience required" section names SQL and a BI tool, and
     whose responsibilities mention analysing data, has SQL and the BI tool as core -- not "analysing
     large datasets" as core with SQL demoted underneath it.
   - NEVER HOLD THE CANDIDATE TO A BAR THE JD ITSELF DOES NOT SET. This is the mirror of the widening
     error above and is just as common: an ask the posting explicitly marks as NOT an entry condition
     gets scored as though it were one. Three cases, all of which make the item "secondary", never
     "core", and none of which may be the concern that drives the grade down:
       (a) The JD says it will TRAIN the hire in it, or that it is learned on the job (e.g. "successful
           candidates complete a two-week training period on our platform"). The item still belongs on
           the checklist as X itself with "met": false if there is no evidence of X (see the capacity
           rule above) -- but an employer who is budgeting to teach X is not screening on X, so it
           cannot be a core requirement.
       (b) The JD introduces it with any of the SOFT vocabulary in QUOTE-THEN-CLASSIFY above --
           beneficial / desirable / advantageous / "a plus" / "a bonus" / "not essential", and
           equally "ideally", "preferably", "welcome", "would be great". "Ideally Databricks or
           Snowflake" is a secondary item, exactly like "Databricks desirable". The posting's own
           word for it governs, even where the named tool sounds central to you, and this holds
           whether the ask is one skill or a list of alternatives.
       (c) The JD states it as a preference while stating a DIFFERENT, weaker bar as the actual
           requirement (e.g. "degree in a quantitative subject preferred; 2:2 minimum required") -- the
           stated minimum is the core item, the preference is secondary.
     Where the candidate MEETS the JD's own stated minimum, say so as a positive in "can_do_fit" or step
     E rather than passing over it in silence; a checklist that records only shortfalls misrepresents a
     posting the candidate genuinely clears.
   START FROM THE "[key requirements]" HINT WHERE THE JOB BLOCK CARRIES ONE. Those items were pulled
   out of this listing by an earlier screening pass that had never seen the candidate, so they cannot
   have been shaped to fit them -- which is exactly the failure mode the three rules above exist to
   prevent, and the reason this is worth anchoring on rather than re-deriving. Carry each hint item into
   your checklist in the JD's own words, keeping its "required"/"nice_to_have" tag as your core/secondary
   split unless the fuller text you have plainly contradicts it; then ADD whatever further requirements
   that pass could not see (it read a truncated opening, you have the full posting), and only then judge
   "met" for each. Doing it in that order saves you re-deriving the JD side from scratch and keeps the
   list honest. Where the hint is absent, build the checklist yourself under the same rules.
   THE HINT IS A FLOOR, NOT A CEILING, AND THE SECOND HALF IS THE HALF THAT GETS SKIPPED. That pass
   usually read only the posting's truncated OPENING -- which is the marketing blurb. The requirements
   section, the named tools and the stated years almost always sit further down, in text ONLY YOU CAN
   SEE. So a checklist that matches the hint and adds nothing is the expected symptom of skipping the
   ADD step, not evidence that the posting asked for little. Before you move on from a listing, re-read
   its requirements/responsibilities section once and check that every distinctly-judgeable ask in it is
   either on your checklist or consciously excluded under one of the rules above. Two specific things go
   missing this way and both belong on the list: a named platform, tool, language or dataset the role
   runs on ("Google Cloud Platform, including BigQuery"), and a stated experience bar ("around 1-2 years
   of development experience"). A stated years-of-experience bar is never optional to record.
   This sweep is a search for FAILABLE asks you missed, never a licence to lengthen the list: it does
   not override the governing test above, and an item added here that no candidate could fail has made
   the checklist worse, not more complete.
E. APPLICATION GUIDANCE -- write "filters_on" and "highlight". This is the one part of the output whose
   job is not to explain your verdict but to tell the candidate what to DO with this posting, so write it
   as advice, not as a rationale. Do NOT restate the grade, do NOT argue the role is a good or bad fit,
   and do NOT summarise "concerns" -- all three are already on the card.
   - "filters_on": 2-4 items from your step-D checklist that this employer will actually screen this
     application on AND that the candidate has some genuine evidence for. Take them from the "core" items
     first, in the JD's own words and at the JD's own specificity ("Power BI", "advanced Excel", "SQL
     against a production warehouse", "3+ years in a commercial analytics team") -- never a capacity or
     an attitude, never a vague competency ("analytical thinking", "attention to detail"). Include an
     item whose evidence is partial or indirect where it is clearly load-bearing for this posting, since
     that is precisely the one the candidate has to argue for. EXCLUDE anything the candidate has NO
     evidence for at all -- an item they cannot speak to is a gap, and gaps belong in "concerns"; this
     field is only for what they can put on the page.
   - "highlight": 2-3 sentences, SECOND PERSON, naming which of the candidate's OWN specific evidence to
     lead with against those items -- the named project, tool, employer, dataset, report or result from
     their profile, not a restatement of the skill tag. "Hence lead with the customer-churn pipeline you
     built in Python and the Power BI dashboard you shipped for the ops team" is the register; "hence
     highlight your data skills" is not. Where the strongest evidence for one of the "filters_on" items
     is weak, self-directed, academic or AI-assisted, say how to frame it honestly rather than pretending
     it is commercial (e.g. "your Salesforce work is self-directed, so pitch it as the reporting problem
     you solved with it rather than as production experience"). Name at most one thing to leave out or
     de-emphasise, and only when it would actively distract.
   If the candidate's evidence is so thin that you cannot
   name two real things for them to lead with, return the one or two you can and stop; do not invent
   evidence that is not in their profile, and never name a project, employer or tool the profile does
   not mention.
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
G. STRENGTHS -- required for any pick you grade "ok" or "stretch", omitted for "very_strong"/"strong".
   Those two grades are shown to the candidate with their gaps listed and NOTHING alongside them, which
   misrepresents a role they are being told is worth applying to. Write 1-3 "strengths" items: the
   specific things the candidate genuinely DOES bring to THIS posting, each naming a real requirement
   from your step-D checklist marked "met": true and the candidate's own concrete evidence for it (a
   named tool, project, employer, dataset or result from their profile). Same discipline as "concerns":
   one item each, most persuasive first, concrete rather than generic ("you have built Power BI
   dashboards used by an ops team, which is the reporting stack this role runs on" -- not "you have
   strong analytical skills"), and never evidence the profile does not actually contain. Where the best
   evidence for an item is self-directed, academic or AI-assisted, say so in the same breath rather than
   letting it read as commercial. If you genuinely cannot name one real strength for a role, that role
   should not be in either list at all -- reconsider whether it belongs in "not_selected".
   Give at least as many "strengths" as "concerns" where the evidence honestly supports it; a card
   showing three gaps and one strength for a role you graded worth applying to is usually a sign the
   checklist was written to fail rather than to judge."""

_FINAL_EVAL_SCHEMA = """Output ONLY a valid JSON object (no markdown), with two required lists and one
optional list, using this item shape for "strong"/"backup":
{"strong": [
  {
    "job_number": 1, "title": "...", "company": "...", "url": "...",
    "role_type": "1 short sentence classifying the FUNCTIONAL NATURE of the day-to-day work -- see reasoning step F. Written FIRST, since it's shown immediately before \\"summary\\" as one continuous sentence pair.",
    "summary": "1 concise, PLAIN-LANGUAGE sentence on what this specific role/project/mission actually involves (not why it fits the candidate) -- see PLAIN LANGUAGE and NO LOCATION COMMENTARY above. Must add information NOT already given by \\"role_type\\" -- never restate its functional-category classification (see reasoning step F's no-overlap rule).",
    "fit_level": "very_strong" | "strong" | "ok" | "stretch",
    "can_do_fit": "a direct, second-person qualification verdict -- see reasoning step B.",
    "filters_on": ["2-4 concrete things this employer will screen on that the candidate CAN evidence, in the JD's own words -- see reasoning step E"],
    "highlight": "2-3 second-person sentences naming which of the candidate's own specific projects/tools/results to lead with against those -- see reasoning step E.",
    "requirements": [{"text": "ONE JD requirement, short and concrete and in the JD's own words -- never a heading covering several, never a capacity anyone would pass; see reasoning step D", "category": "core" | "secondary", "met": true}],
    "strengths": ["1-3 concrete things the candidate DOES bring to this posting, strongest first -- REQUIRED when \\"fit_level\\" is \\"ok\\" or \\"stretch\\", omit otherwise; see reasoning step G"],
    "concerns": ["the notable gaps, one per item, most sink-worthy first, AT MOST 3 -- see reasoning step C; [] if none"],
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
 ],
 "not_selected": [
   {"job_number": 7, "reason": "one short phrase naming the main thing that kept this out of both lists, quoting the JD clause it rests on where one exists"}
 ]}
Include a "disqualified" entry for every job you excluded from BOTH lists above because it failed one of
the DISQUALIFIERS rules. The "reason" must contain the exact quoted clause from QUOTE-THEN-CLASSIFY, not
just a paraphrase of the rule name, so a misfire can be checked against the listing text afterward.

EVERY REMAINING JOB GETS A "not_selected" ENTRY. Between "strong", "backup", "disqualified" and
"not_selected", every job_number you were given must appear exactly once -- no job may be silently
dropped from all four lists. "not_selected" is for the jobs that passed the disqualifiers but weren't
among the best-fitting options: say in one short phrase what actually kept each one out (e.g. "core
requirement unmet: \\"3+ years in a commercial analytics team\\"", "function is data entry rather than
analysis", "weaker tooling overlap than the picks above"). Where a specific JD clause drove it, quote
that clause verbatim inside the reason exactly as QUOTE-THEN-CLASSIFY requires; where the job was simply
out-competed by better picks rather than failing anything, say that plainly instead of inventing a fault.
The reason is held to the SAME discipline as "concerns" and the step-D checklist, not a looser one: an
ask introduced by the SOFT vocabulary in QUOTE-THEN-CLASSIFY ("ideally Databricks or Snowflake",
"Tableau desirable") is a secondary item and may NEVER be the stated reason a role was not selected, and
neither may an ask the posting says it trains for. If the only things separating this job from the picks
are nice-to-haves, the honest reason is that it was out-competed -- say that.
These reasons are never shown to the candidate -- they exist so a later audit can tell a job that was
beaten from one that was quietly misread, which a blank reject cannot distinguish.
"scam_suspect" (on "strong"/"backup" items only): true if the SCAM/CV-FARMING rule's softer signals
raised exactly ONE flag on this listing (not enough alone to disqualify it into the list above); false
otherwise. Omit or leave false when you saw none of those signals.

"strengths" (on "strong"/"backup" items only): see reasoning step G. REQUIRED whenever "fit_level" is
"ok" or "stretch" -- those are the grades whose card would otherwise show the candidate a list of gaps
and nothing else for a role you are telling them to apply to. Omit it for "very_strong"/"strong", where
the grade and "can_do_fit" already say the candidate clears the bar.

"fit_level" is used internally to ORDER the picks the candidate sees; it is not printed as a
label on their card, so grade it honestly rather than protectively -- an accurate "ok" costs
the candidate nothing and a flattering "strong" corrupts the ordering. Derive it mechanically
from your own step-D checklist and your own "concerns" list, and never grade a role higher than
those two support. Note what that dependency means in practice: this rubric reads "core" items
ONLY, so a short or under-tiered checklist does not produce a cautious grade, it produces an
inflated one -- which is why step D is the field to protect when the response is running long. A concern "touches a core requirement" when it names, qualifies, or weakens the
evidence for one of your "core" items (an evidence-strength caveat on a core skill -- "your
evidence is portfolio-based, not paid" -- IS such a concern, not a footnote):
- "very_strong": every core requirement "met": true and NO concern
  touching a core requirement. Genuinely rare -- often none in a batch, seldom more than one.
- "strong": every core requirement "met": true, and at most ONE concern touching a core
  requirement, which the candidate's other evidence plausibly closes.
- "ok": exactly one core requirement "met": false, OR two or more concerns touching core
  requirements.
- "stretch": two or more core requirements "met": false.
Grade every pick this way whichever list it is in. "ok" and "stretch" are the expected grades
for a "backup" item, but they are also the correct, honest grades for a "strong"-list role
that survived the disqualifiers and is worth showing while still leaving the candidate real
gaps to close -- a role does NOT become a "strong" fit_level by virtue of being in "strong".
If a role reads better to you than the rubric allows, do not soften the rule: re-check whether
your step-D checklist was written at the JD's own level of specificity, since a checklist
padded with unfailable aptitudes is what produces an unearned grade.

"role_salary"/"work_style"/"role_seniority"/"deadline" are FACTS READ OFF THIS POSTING, not
judgements about the candidate. Report only what this posting's own description actually says: use null
when it is silent, and never infer, estimate, or borrow a figure from another posting's text in the
payload (see SCOPE OF EACH POSTING'S TEXT). For "work_style" apply the same classification as the
LOCATION/VISA/RELOCATION rule -- a stated office location with no remote/hybrid/work-from-home
wording anywhere is "On-site", not null and not "Remote".

"can_do_fit", "filters_on" and "highlight" are shown directly to the candidate -- "can_do_fit" as the
headline "are you qualified", and "filters_on"/"highlight" together as a "Highlight when applying"
block reading "This role likely filters on: <filters_on>." followed by your "highlight" sentences. Write
them to read that way: short, plain-language, standing alone without the rest of the analysis, and with
"highlight" continuing naturally from the filters_on list rather than repeating it."""

# Static system prefix -- identical across every cluster/call, so it's a stable
# (prompt-cache-friendly) prefix instead of being rebuilt into each user prompt. It
# carries all the rules; the per-call user prompt is just the CV + the jobs payload.
_FINAL_EVAL_SYSTEM = f"""You are an elite talent placement advisor matching a candidate to open job vacancies.
You are given a candidate profile and a set of job postings, and must return TWO lists: "strong" and "backup".

WHAT THE BRACKETED HINTS IN A JOB BLOCK ARE. Some blocks carry notes in square brackets --
"[key requirements: ...]", "[screen note: ...]", "[earlier screening pass thought: ...]", "[source: ...]",
"[posting-volume note: ...]". These come from cheaper earlier passes that saw LESS of the posting than you
do (often only its truncated opening) and, in the case of "[key requirements]", had not seen the candidate
at all. Use them to know WHERE TO LOOK, not what to conclude: they save you re-deriving the JD side from
nothing, and they flag which checks are likely to matter for this listing. You have the fuller text, so
your reading always wins where they disagree, and a hint is never grounds to skip a DISQUALIFIER or to
soften a "fit_level" the rubric doesn't support. In particular, "[earlier screening pass thought: ...]" is
one cheap model's one-line impression -- treat it as a claim to verify, never as evidence of fit.
"[listing age: ...]" is the one exception to all of the above: it is not an earlier pass's opinion but a
FACT from the job board's own API, which the posting text itself usually does not state, so you cannot
check it and must take it as given. It carries two different severities, and they mean different things.
A listing marked STALE has been open for months. It is normally still live, so this is not a disqualifier
and never on its own a reason to reject -- but most of its shortlist is already decided, so it is a
materially worse use of an application than an equally-good fresh role. Treat it as a real concern: name it
in "concerns" and let it push a borderline grade down one step, and prefer the fresher role when choosing
between two comparable picks. A listing marked ELIMINATE is different in kind, not just degree: it means
this system's own data has CONFIRMED the posting is older than the maximum listing age the candidate
themselves set in their profile -- that is what DISQUALIFIER 8 (MAX LISTING AGE) below is for, and only
this ELIMINATE wording (or your own reading of an explicit date/staleness in the text) carries that weight;
STALE alone never does, and never treat the two as the same thing. A block with NO age tag has an unknown
posting date: say nothing about its age and never assume it is old.
There is a THIRD severity, between those two, and it is the one most likely to be under-weighted. A tag
reading "OVER THE CANDIDATE'S STATED MAXIMUM (a preference, not a hard limit)" means this system has
CONFIRMED the posting is older than the maximum the candidate set, but they chose to state that as a
preference rather than a binding limit -- so it is not a disqualifier and the role must still be shown and
still be judged on its merits. It is, however, much stronger evidence than plain STALE: the candidate named
a number and this listing is past it. Do not treat it as the "borderline grades only" nudge above. Name it
in "concerns" in the candidate's own terms ("the posting has been open longer than the maximum age you set")
and, when you apply the fit_level rubric, count it as ONE concern touching a core requirement -- so a role
that would otherwise be "very_strong" becomes "strong", and so on down. A tag reading "MORE THAN DOUBLE THE
CANDIDATE'S STATED MAXIMUM" is the same rule at twice the weight: count it as TWO such concerns, which caps
the role at "ok" however well it otherwise fits. Neither ever excludes a role, and neither is ever a reason
to move a role out of "backup" into "not_selected" -- an old posting the candidate can do is still a role
they may want; it simply must not outrank a fresher equal.
The age tag may cite TWO different clocks, and only the first is the board's own claim. "posted N days ago"
is what the board states. "we have been finding this same listing ... for N days" is how long this system
has been seeing it advertised -- a LOWER bound on its real age, never evidence that it is new. When both
appear and the tag says the posting date "appears to have been refreshed", believe the longer one: that is
an old advert re-dated rather than a new vacancy, and it is the shortlist-already-decided concern above.
"[possible standing/pipeline ad: ...]" is NOT in this exception -- it quotes wording from the posting text
you can read yourself, so verify it. Some employers run an always-open advert to collect CVs into a talent
pool with no single vacancy behind it, and an application there often reaches a database rather than a
hiring manager. If the fuller text confirms it (no specific role, duties or requirements of its own; asks
only to "register interest" or join a pool), name it in "concerns" and let it push a borderline grade down
one step -- same weight as staleness. If the posting does describe one specific role with its own duties
and requirements, the phrase was careers-page boilerplate: say nothing about it. It is never a
disqualifier on its own.

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

"backup" -- every OTHER role worth the candidate's time, not just the least-bad two or three. Include a
role here whenever it passes the DISQUALIFIERS above and a reasonable candidate might actually apply to
it, even though it fell short of the "strong" bar. In this list, evidence weakness and cumulative
nice-to-have gaps are EXPECTED and ACCEPTABLE -- do NOT use them to exclude a role, only note them
honestly in "concerns" and let them show in an honest "ok"/"stretch" fit_level. This list is SHOWN to
the candidate, below the strong picks and labelled as the lesser fits, so fill it properly: a role with
nothing actually wrong with it belongs here, NOT in "not_selected". A short "backup" list is only
correct when the remaining roles genuinely are poor fits. Leave it empty only when every remaining role
is disqualified or is plainly not worth an application.

Why this bar is set where it is: employers hire "under-qualified" candidates far more often than their
adverts suggest, so a role the candidate can plausibly do most of is a real opportunity and not padding.
Three concrete tells that a posting's stated requirements are a wish-list rather than a bar, each of
which should push you toward including a role in "backup" rather than passing over it, and toward the
more generous of two adjacent fit_levels:
 - It is posted by a RECRUITMENT OR STAFFING AGENCY rather than the employer directly. Agency adverts
   are compiled by a recruiter from a hiring manager's brief and then padded; they are written to
   attract a wide funnel, not to gatekeep precisely.
 - It is a CONTRACT, interim, fixed-term or day-rate role rather than a permanent one. Contract hiring
   bars are lower and more negotiable than the advert implies -- the org needs someone competent to
   cover a gap for a fixed period, with far less long-term risk if it doesn't work out.
 - The "essential" list is LONG (roughly eight or more items, especially split into technical /
   analytical / communication groupings) and/or the pay is "negotiable"/"competitive". A list that long
   is a description of an ideal hire nobody will match; the real bar is closer to "can do most of this
   and talk credibly about the rest".
None of these three is a reason to overstate fit, to mark an unmet requirement "met", or to soften a
DISQUALIFIER -- they change whether a role is worth SHOWING and how harshly a shortfall is graded, never
what the evidence actually says. Say the gap plainly in "concerns" and put the role in the list anyway.

{_FINAL_EVAL_REASONING}

{_FINAL_EVAL_WORDING}

{_FINAL_EVAL_SCHEMA}"""

# Prompt-cache routing key for the judge. Deliberately a CONSTANT, not scoped to
# the profile or the run: _FINAL_EVAL_SYSTEM interpolates nothing, so every judge
# call ever made shares this ~12k-token prefix byte for byte -- the per-call
# variation starts in the user message, at the CV. That makes it the one prefix
# here worth `cache_retention="24h"`: the default few minutes of inactivity would
# expire it between runs, and this pipeline's biggest fixed cost is paying for
# those 12k tokens once per cluster per run. Versioned so a prompt edit routes to
# a fresh entry instead of colliding with the old text's key.
_FINAL_EVAL_CACHE_KEY = f"final_eval_v{FINAL_EVAL_PROMPT_VERSION}"


def _final_eval_job_block(i: int, j: dict, store_age_days: float | None = None,
                          max_age_days: int | None = None, hard: bool = True) -> str:
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
    req_text = _key_requirements_text(j)
    if req_text:
        hint += f"[key requirements: {req_text}]\n"
    # rank_gate's own one-line verdict on this listing (_rank_note, see
    # _rank_prompt's "note" field) -- already computed by the mid tier and, until
    # now, thrown away after it decided which jobs got here. It is the cheapest
    # possible hand-off between tiers: one already-written phrase naming what the
    # mid tier thought drove the fit, so this call can verify a specific claim
    # instead of re-deriving the same read from scratch. The numeric score is
    # deliberately NOT passed -- a number anchors a grade, a phrase points at
    # something checkable.
    rank_note = (j.get("_rank_note") or "").strip()
    if rank_note:
        hint += f"[earlier screening pass thought: {rank_note}]\n"
    age_tag = _listing_age_tag(j, store_age_days=store_age_days,
                               max_age_days=max_age_days, hard=hard).strip()
    if age_tag:
        hint += f"{age_tag}\n"
    liveness_tag = _listing_liveness_tag(j).strip()
    if liveness_tag:
        hint += f"{liveness_tag}\n"
    return (f"JOB {i+1}: {j['title']} at {j['company']}\n"
            f"Location: {j.get('location') or 'not stated'}\nURL: {j['url']}\n{hint}\n"
            f"{j.get('full_text','')[:FINAL_EVAL_JOB_TEXT_CHARS]}")


def _sanitize_filters_on(raw) -> list[str]:
    """Validate/cap the judge's "filters_on" list (reasoning step E) -- at most 4
    short strings, so a malformed or runaway response can't corrupt the persisted
    verdict or spill a paragraph into the card's one-line "This role likely filters
    on: ..." lead."""
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:4]:
        item = str(r).strip()
        if item:
            out.append(item[:80])
    return out


# Both bullet lists the card renders ("concerns" and its new "strengths" twin) are
# capped at the same number. The cap is a display decision, not a modelling one: the
# card shows them as two matched blocks under one heading each, so an overrunning
# "concerns" list turns a role the judge just recommended into a wall of reasons not
# to apply -- which is precisely the complaint that prompted the "strengths" field.
# Asked for in the prompt too (reasoning steps C and G); enforced here because a
# prompt-only cap is a request, and a malformed reply must not be able to corrupt a
# persisted verdict.
_BULLET_LIST_MAX = 3


def _sanitize_bullets(raw) -> list[str]:
    """Validate/cap one of the card's bullet lists ("concerns"/"strengths")."""
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:_BULLET_LIST_MAX]:
        item = str(r).strip()
        if item:
            out.append(item[:300])
    return out


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


def _run_final_eval(jobs: list[dict], cv_text: str | None,
                    store_age_days: float | None = None,
                    max_age_days: int | None = None, hard: bool = True,
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

    jobs_block = "\n\n---\n\n".join(
        _final_eval_job_block(i, j, store_age_days, max_age_days, hard) for i, j in enumerate(jobs))
    # Injected per-call, never into _FINAL_EVAL_SYSTEM (that constant must stay
    # byte-identical across every call for the 24h prompt cache to pay off -- see
    # _FINAL_EVAL_CACHE_KEY). DISQUALIFIER 9 (PASSED APPLICATION DEADLINE) reads this.
    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    prompt = f"""Today's date: {today_str}

Candidate Background Profile:
{cv_text}

Judge the {len(jobs)} complete job postings below. Return up to {FINAL_PICKS} genuinely strong fits in
"strong" (best first), and up to {FINAL_PICKS} further worth-applying-to roles in "backup" (best first;
see the "backup" rules above -- this list is shown to the candidate, so put every role with nothing
actually wrong with it here rather than in "not_selected"). For any job you hard-exclude from both lists
via a DISQUALIFIERS rule, add it to "disqualified" with a short reason. Put every job that is in none of
those three lists into "not_selected" with a short reason -- all {len(jobs)} job numbers must be
accounted for.

Jobs Payload:
{jobs_block}"""

    # See _safe_temperature -- gpt-5.5/gpt-5.6-terra 400 on any
    # non-default temperature. Keyed off EXP_MODEL so switching models can't
    # silently 400 into the unverified-fallback path again without anyone
    # noticing (see 691cf89 -- that's exactly how this was missed for a full
    # day: the 400 was swallowed by the except branch below). Confirmed
    # gpt-5.6-terra also 400s on 0.2 -- it was silently hitting this fallback
    # on every single Phase 6 call until this was added.
    temperature = _safe_temperature(EXP_MODEL, 0.2)
    try:
        raw = llm(prompt, system=_FINAL_EVAL_SYSTEM, model=EXP_MODEL, require_json=True,
                  temperature=temperature, stage="judge",
                  cache_key=_FINAL_EVAL_CACHE_KEY, cache_retention="24h",
                  max_output_tokens=FINAL_EVAL_MAX_OUTPUT_TOKENS)
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
            # Same stage/cache_key/cache_retention as the primary call above.
            # Without them this retry paid the full ~12k-token _FINAL_EVAL_SYSTEM
            # prefix uncached on the most expensive model, AND was invisible to
            # _record_llm_usage -- so the judge stage's recorded cache-hit ratio
            # was computed over a denominator that systematically excluded the
            # calls most likely to miss.
            raw = llm(prompt, system=_FINAL_EVAL_SYSTEM, model=EXP_MODEL, require_json=True,
                      temperature=temperature, stage="judge",
                      cache_key=_FINAL_EVAL_CACHE_KEY, cache_retention="24h",
                      max_output_tokens=FINAL_EVAL_MAX_OUTPUT_TOKENS)
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
                # Application guidance (reasoning step E), which replaced the old
                # first-person top_match_reason narrative in FINAL_EVAL_PROMPT_VERSION
                # 23. Same 700-char runaway guard the narrative had; filters_on is
                # capped at 4 short items to match what step E asks for and to keep
                # the card's one-line "This role likely filters on: ..." readable.
                merged["filters_on"] = _sanitize_filters_on(merged.get("filters_on"))
                merged["highlight"] = str(merged.get("highlight") or "").strip()[:700]
                # "concerns" is capped here as well as asked for in the prompt: it is
                # rendered as a bullet list under one heading, and a model that
                # overruns turns a role it just recommended into a wall of reasons
                # not to bother. "strengths" is capped to match, so the card can
                # never show more of one than the other by accident.
                merged["concerns"] = _sanitize_bullets(merged.get("concerns"))
                merged["strengths"] = _sanitize_bullets(merged.get("strengths"))
                out.append(merged)
        return out

    # Both exclusion lists ride home in the same slot, each entry tagged with
    # _disqualifier so the caller can still tell them apart (the disqualifier COUNT
    # is a real diagnostic -- see engine.py's funnel_counts["judge_disqualified"]).
    # Merged rather than returned as a fourth element because every caller and the
    # failure sentinel are built around a 3-tuple, and both lists are consumed the
    # same way: a reject verdict plus the AI's own reason for it.
    excluded = _merge(data.get("disqualified"), len(jobs))
    for d in excluded:
        d["_disqualifier"] = True
    seen = {d.get("_identity") for d in excluded}
    # Disqualified wins if the model listed a job in both -- a hard exclusion is the
    # more specific statement, and its reason carries the quoted clause.
    for d in _merge(data.get("not_selected"), len(jobs)):
        if d.get("_identity") in seen:
            continue
        d["_disqualifier"] = False
        excluded.append(d)
    # Both lists capped at FINAL_PICKS. "backup" used to be capped at 3 ("least-bad
    # survivors"), which made it a last-resort filler rather than the second half of
    # the result set -- and pushed every other perfectly-applicable role into
    # "not_selected", which carries only an internal audit phrase and so cannot be
    # shown at all. Widening the list is what lets a thin run fill its 12 slots with
    # roles the judge has actually written display-quality output for.
    return (_merge(data.get("strong"), FINAL_PICKS),
            _merge(data.get("backup"), FINAL_PICKS),
            excluded)


def final_evaluation_split(jobs: list[dict], profile: dict, cv_text: str | None = None
                           ) -> tuple[list[dict] | None, list[dict] | None, list[dict] | None]:
    """Backend entry point: (strong, backup, excluded) from Phase 6. The backend
    uses `strong` when non-empty, else `backup` (tagged as a non-strong fallback);
    `excluded` carries a short AI-authored reason for EVERY job that reached the judge
    and didn't make either list, persisted into its reject verdict instead of leaving it
    blank. Each entry is tagged `_disqualifier`: True for a hard DISQUALIFIERS exclusion,
    False for one that passed the disqualifiers but was simply out-competed. Both used to
    collapse into a blank reject unless a disqualifier fired -- a ground-truth audit found
    15 of 24 rejects in the judge pool carrying no reason at all, which made it impossible
    to tell whether the cheaper tiers upstream could have known. Returns (None, None, None)
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
    store_age_days = (profile or {}).get("store_age_days")
    max_age_days = (profile or {}).get("max_listing_age_days")
    age_hard = (profile or {}).get("max_listing_age_hard", True)
    if len(jobs) <= FINAL_EVAL_MAX_JOBS_PER_CALL:
        return _run_final_eval(jobs, cv_text, store_age_days, max_age_days, age_hard)

    # Balanced, not greedy-fixed-size: a plain jobs[i:i+CAP] slice puts a lopsided
    # remainder in the last chunk (27 jobs at CAP=20 -> one call judging 20, another
    # judging only 7), so the two calls read the SAME cluster under very different
    # amounts of cross-listing context to compare against. Splitting into the ceil
    # number of chunks and dividing as evenly as possible instead gives 27 -> 14+13.
    num_chunks = -(-len(jobs) // FINAL_EVAL_MAX_JOBS_PER_CALL)  # ceil division
    base_size, remainder = divmod(len(jobs), num_chunks)
    chunks: list[list[dict]] = []
    start = 0
    for i in range(num_chunks):
        size = base_size + (1 if i < remainder else 0)
        chunks.append(jobs[start:start + size])
        start += size
    emit(f"[phase 6] {len(jobs)} jobs split into {len(chunks)} concurrent batches "
         f"of {sorted(set(len(c) for c in chunks))} (cluster exceeds single-call cap of "
         f"{FINAL_EVAL_MAX_JOBS_PER_CALL})")
    strong: list[dict] = []
    backup: list[dict] = []
    disqualified: list[dict] = []
    any_succeeded = False
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        futures = [pool.submit(_run_final_eval, chunk, cv_text, store_age_days, max_age_days, age_hard)
                   for chunk in chunks]
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
    return strong[:FINAL_PICKS], backup[:FINAL_PICKS], disqualified


def final_evaluation(jobs: list[dict], profile: dict, cv_text: str | None = None) -> list[dict]:
    """Judge `jobs`; return the best flat list -- strong fits, or the least-bad backups
    when nothing is strong. Standalone/CLI callers use this; `cv_text`, when given,
    overrides reading CV_PATH (the backend passes a role-cluster-scoped bio)."""
    strong, backup, _disqualified = _run_final_eval(
        jobs, cv_text, (profile or {}).get("store_age_days"),
        (profile or {}).get("max_listing_age_days"), (profile or {}).get("max_listing_age_hard", True))
    return (strong or backup) or []


# ── Phase 7: Save & Output ───────────────────────────────────────────────────────


def save_and_display(results: list[dict]):
    sep = "─" * 60
    emit(f"\n{'═'*60}\n  TOP PIPELINE MATCHES SUMMARY\n{'═'*60}")

    conn = get_db()
    for i, job in enumerate(results, 1):
        emit(f"\n#{i}  {job['title']}\n    {job['company']}\n    {job['url']}\n\n    {job.get('summary', '')}")
        if job.get("filters_on"):
            emit(f"    ✓  Likely filters on: {', '.join(job['filters_on'])}")
        if job.get("highlight"):
            emit(f"    ✓  {job['highlight']}")
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
            if job.get("filters_on"):
                f.write(f"- ✓ Likely filters on: {', '.join(job['filters_on'])}\n")
            if job.get("highlight"):
                f.write(f"- ✓ {job['highlight']}\n")
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