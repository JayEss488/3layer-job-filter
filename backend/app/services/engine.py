"""The search-engine service boundary.

The existing engine (full_auto.py) already works and must not be refactored.
This module is the ONLY place that imports it. It:
  1. builds the engine's input from a profile snapshot (weights applied),
  2. discovers listings into a persistent per-profile store (jobs_seen),
  3. enriches only new (+backlog top-up) rows through an adaptive funnel,
  4. persists the ranked output as `roles` rows.

full_auto is imported lazily so the API (and its lightweight LLM features) boot
even without crawl4ai/playwright present, and only a real search pays that cost."""
import asyncio
import base64
import hashlib
import json
import math
import queue
import random
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import numpy as np
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import CATEGORY_EXPAND_ENABLED, DISCOVERY_ATS_CACHE_TTL_HOURS, ROLE_STALE_DAYS
from ..database import SessionLocal
from ..models import Role, SearchRun, JobSeen, JobEmbedding
from .families import ensure_families
from .profile_intel import ensure_profile_intel
from .snapshot import build_snapshot, cv_text_for_cluster
from .moderation import filter_blocked, get_blocked_domains
from .sources import (
    ATS_KEYS,
    canonical_key,
    counts_from_breakdown,
    get_ats_batch_stale,
    get_disabled,
    get_full_scrape_enabled,
    mark_ats_batch_fetched,
    save_last_run_counts,
)

# ── Adaptive funnel tuning ───────────────────────────────────────────────────
# Funnel: per cluster, batches of embed-score-ranked candidates are fed through
# gate+rank (see _gate_rank_refill_cluster) until that cluster's fair share of
# JUDGE_POOL rank-floor survivors accumulate, or the cluster's candidate queue
# is exhausted, or its fair share of the examine budget (see
# RANK_EXAMINE_BUDGET below) has been examined
# -- unlike a one-shot capped batch, this keeps pulling from the idle
# above-RELEVANCE_FLOOR pool instead of leaving hundreds of unexamined,
# fair-scoring candidates on the table every run. Survivors -> expensive
# full-text judge -> engine.FINAL_PICKS capped final results.
TARGET_POOL       = 90     # gate-survivor checkpoint per cluster per refill round
# Examine-round sizing inside _gate_rank_refill_cluster. A round gates then ranks
# one batch and only THEN reports its judge-eligible snapshot for provisional
# "Verifying…" cards, so the size of the FIRST round is what gates time-to-first-
# card. A small first round (20) paints cards ~4x sooner than the old effectively-
# single round of examine_cap (80); later rounds are larger (40) to keep the total
# round count -- and thus the per-round screen_gate/rank_gate orchestration
# overhead -- low. (These cap the round; TARGET_POOL still bounds it from above.)
GATE_FIRST_ROUND  = 20     # small first examine round -> earliest provisional cards
# Later rounds raised 40 -> 80 alongside the RANK_EXAMINE_BUDGET increase below.
# Round count, not batch size, is what costs wall time here: a round is a blocking
# screen_gate call followed by a blocking rank_gate call, and each of those already
# fans its own _GATE_BATCH(=20)-sized sub-calls out over a 3-worker pool -- so an
# 80-candidate round is 4 sub-calls across 3 workers (~2 waves) rather than 4
# sequential round-trips. At the old 40, a 240-candidate budget would have meant
# ~7 sequential rounds per cluster; at 80 it is 4 (20 + 80 + 80 + 60). The FIRST
# round is deliberately left at 20: it alone sets time-to-first-provisional-card,
# since report() only fires once a whole round has gated AND ranked.
GATE_ROUND_SIZE   = 80     # subsequent examine rounds
MIN_RESULTS       = 3      # below this many strong matches, broaden the threshold
# Below this many characters, a job's discovery-time snippet is assumed too
# thin (e.g. a short Google-organic blurb) to judge fit against without
# reading the real page. ATS-sourced snippets (greenhouse/lever/etc.) already
# carry the full posting description and skip this check entirely. Adzuna's
# API teaser is truncated at exactly 500 chars -- keep this strictly above
# that or every Adzuna snippet waves through as "sufficient" purely because
# its truncation length happens to land on the threshold.
SNIPPET_SUFFICIENT_CHARS = 600
# Raised from 0.35 -> 0.37: a diagnostic re-score of ~1,700 already-discovered
# jobs (analyze_embedding_gate.py) against this profile's live cluster
# embeddings found the lowest-scoring listing that was still a genuinely
# decent match sitting at 0.379 -- everything below that in the sample was
# off-target (wrong seniority band the title regex doesn't catch, wrong
# function entirely, or low-quality/templated listings). 0.35 was letting
# through real noise with room to spare; 0.37 still clears that 0.379 floor
# with margin.
# Raised again, 0.37 -> 0.39, and RELEVANCE_FLOOR raised in lockstep from
# 0.20 to the same value: _cluster_candidate_queues always admits everything
# down to RELEVANCE_FLOOR into the candidate queue regardless of
# RELEVANCE_PRIMARY (see `broadened` below) -- RELEVANCE_FLOOR, not
# RELEVANCE_PRIMARY, was the actual pool-admission gate, and a live
# tests/gate_harness.py run against real cached listings confirmed jobs
# scoring below ~0.39 were reliably screened out later anyway (wrong
# role/sector, wrong seniority), so admitting them at all just burned
# gate/rank calls on jobs with no realistic path to a final pick. Kept equal
# rather than reintroducing a gap, since RELEVANCE_FLOOR must never exceed
# RELEVANCE_PRIMARY -- `broadened` (>= floor) has to stay a superset of
# `strong` (>= primary) or the harsh/floor_fallback bookkeeping below stops
# meaning what its own comments say it means.
RELEVANCE_PRIMARY = 0.39   # strict strong-fit threshold
RELEVANCE_FLOOR    = 0.39  # never include anything weaker than this
BACKLOG_TOPUP     = 40     # enriched rows pulled in when fresh discovery is thin
STORE_SCORE_CAP   = 6000   # max 'new' rows relevance-scored per run (whole store)
# Fresh-row embedding fetch (_ensure_embeddings): texts are capped at ~2000
# chars (~500 tokens) each, so 250/chunk is ~125k tokens/request -- well
# inside OpenAI's per-request limits. This is now a CEILING on chunk size, not
# the chunk size itself -- see _embed_chunk_size. A fixed 250 was tuned
# against one measured 651-new-row run (7 chunks/2 sequential rounds at
# chunk=100 -> 3 chunks/1 round at chunk=250), but a fixed size ignores batch
# count entirely: as the shared JobEmbedding cache has matured, most runs now
# discover well under 250 new jobs, which used to mean the WHOLE batch landed
# in a single chunk on a single worker thread -- zero use of the other 5
# workers, and the phase's wall time became just one OpenAI call's latency
# regardless of how few items were in it. Measured across this store's run
# history: batches that split into >=4 chunks ran at ~0.007-0.05 sec/embed,
# while single-chunk batches (run 17: 121 new embeds, one chunk) ran at
# ~0.24 sec/embed -- 4-30x worse per item, not because the API got slower but
# because nothing was left to parallelize. Worker count raised from 4: the
# embeddings endpoint's rate limits run well above the chat-completion
# endpoints gate/rank's "cap at 3-4" was calibrated against; kept at 6 rather
# than higher since a burst still risks a short-window rate cap.
EMBED_CHUNK_SIZE  = 250
EMBED_MAX_WORKERS = 6


def _embed_chunk_size(n: int) -> int:
    """Chunk size that spreads `n` texts over (up to) EMBED_MAX_WORKERS
    chunks, capped at EMBED_CHUNK_SIZE so a huge batch still respects the
    per-request token budget above. For n <= EMBED_CHUNK_SIZE * workers (the
    common case now -- see the comment above), this always yields exactly
    min(n, EMBED_MAX_WORKERS) chunks, so every available worker gets used
    instead of leaving 5 of 6 idle on a batch that happens to be under 250."""
    return min(EMBED_CHUNK_SIZE, max(1, math.ceil(n / EMBED_MAX_WORKERS)))
# Cheap numeric-ranking stage (rank_gate), between the sector/seniority gate and
# the expensive full-text judge: an extra cheap-model pass that scores gate
# survivors 0-100 on fit instead of a boolean pass/fail, so the expensive judge
# only ever sees a curated top slice instead of every gate survivor.
JUDGE_POOL = 40               # top-ranked candidates sent on to scrape + judge
# The mid tier is deliberately run WIDER than the judge is: it accumulates up to
# RANK_TARGET_POOL approvals and the judge then takes the best JUDGE_POOL of them,
# so the expensive stage chooses from a curated best-of rather than from whatever
# happened to survive. Before this, the accumulation target WAS JUDGE_POOL, which
# made the judge's input "everything that got through" -- a live run reached it
# with 35 candidates, 5 of them in one cluster, and the judge could only pick the
# least-bad of five. Widening the mid tier is cheap relative to the judge (CHEAP
# screen + MID rank vs. a STRONG full-text call on a scraped page), which is the
# whole reason the ratio is worth paying for.
RANK_TARGET_POOL = 80         # run-wide judge-eligible target for the gate+rank stage
# Run-wide ceiling on how many candidates the cheap+mid stages examine, split
# evenly across active clusters at the call site. Replaces the old per-cluster
# SINGLE_CLUSTER_EXAMINE_CAP(80)/MULTI_CLUSTER_EXAMINE_CAP(40) pair, whose total
# depended on cluster count in the wrong direction: a 2-cluster profile examined
# 80 in total while a 1-cluster profile examined 80 as well, and a live 2-cluster
# run left queues of 329 and 194 with only 40 examined each. A single run-wide
# number is both the honest cost dial and the thing worth tuning.
# Raised 240 -> 320: a live single-cluster run against a 760-deep queue stopped
# on "absolute pool cap" at 240 examined / 69 gated / 9 judged -- the budget
# itself, not a thin queue or MIN_RESULTS/JUDGE_POOL, was the limiting factor,
# with hundreds of unexamined above-floor candidates still sitting in that
# cluster's queue.
RANK_EXAMINE_BUDGET = 320
# Symmetric FLOOR to the JUDGE_POOL ceiling: an absolute rank cutoff
# (RANK_REJECT_SCORE_FLOOR) plus per-cluster examine caps can leave the judge with
# far fewer than JUDGE_POOL candidates even when dozens of gate survivors exist
# (a live run sent only 20). Rather than pay the cheap rank stage to silently kill
# borderline roles the expensive judge never gets to weigh in on, backfill the
# judge pool up to this many by running one bounded EXTRA gate+rank round per
# still-open cluster over its next unexamined candidates (best-effort -- only
# clusters whose first round stopped on a cap, not a genuinely exhausted queue,
# have anything fresh to examine; see the call site in _run_engine_pipeline).
JUDGE_POOL_FLOOR = 25
# Per-cluster examine cap for that extra round -- deliberately small (one
# GATE_FIRST_ROUND-sized batch) so a shortfall costs at most one more cheap
# gate+rank call per needy cluster rather than re-running a full
# per-cluster share of RANK_EXAMINE_BUDGET. Rarely fires now that the main
# budget is 320 rather than 40-80, but kept as the safety net for a profile
# whose queues are genuinely thin.
JUDGE_POOL_FLOOR_EXTRA_CAP = 20
# How many of the judge pool's top-ranked candidates are persisted as
# provisional "being verified..." Role rows the moment gate+rank finishes --
# roughly halfway through a run, before the ~90s scrape+judge tail -- so the
# user sees real cards (with the cheap 0-100 fit estimate) while the expensive
# judge works. Matches full_auto.FINAL_PICKS so the end-of-run upgrade swaps
# card content in place rather than visually collapsing a longer list.
PROVISIONAL_MAX = 12
# Progressive paint: how many candidates are shown straight off the embedding
# pre-filter, before any LLM has looked at them. These land seconds into a run
# (the embed+score phases measured 6.8s and 0.4s live) instead of the ~90s the
# first gate+rank round takes, but they are also the least-informed cards the app
# ever shows -- cosine similarity against a cluster embedding, nothing more. Kept
# deliberately small for that reason: it is a "we're working, here's the shape of
# it" signal, not a result set. Most of these get replaced by the rank-stage paint
# (in place -- see _upsert_provisional_rows), so a run typically ends with fewer
# than this many still sitting in the embedding section.
EMBED_PAINT_MAX = 8
# Stage values for Role.provisional_stage, in pipeline order. A row is only ever
# promoted forwards along this chain.
PROVISIONAL_STAGE_EMBED = "embed"
PROVISIONAL_STAGE_RANK = "rank"
_PROVISIONAL_STAGES_AFTER_EMBED = (PROVISIONAL_STAGE_RANK,)
# A candidate whose text is already rich enough to judge (ATS description, a
# Reed/Adzuna full description fetched pre-gate by _enrich_pre_gate, a persisted full_text
# from an earlier run, or a long-enough snippet -- i.e. _has_judgeable_text is
# True) gets this added to its ORDERING score when the judge pool is filled.
# Not to its rank score, and not to the floor test: a bad job stays rejected
# (see _selection_score). Purely a tie-break, so among candidates the mid tier
# rated the same, the judge pool fills with the ones it can actually read --
# which both shortens phase 5 (fewer pages to fetch, the run's longest tail and
# its main anti-bot exposure) and gives the judge better text on the roles it
# does see. Small on purpose: 3 points on a 0-100 scale breaks a near-tie and
# nothing more, so this can never promote a 60 over a 75.
RICH_TEXT_SELECTION_BONUS = 3.0
# Run-wide cap on how many about-to-be-gated candidates _enrich_reed_full_text is
# offered before the cluster pool starts. This step is pure blocking HTTP on the
# main thread (it commits), so it sits directly in front of time-to-first-card:
# ~12 ids resolve per wave, and a live run enriched 45 in 4.1s. Handing it the
# whole RANK_EXAMINE_BUDGET slice would have made that ~12s of dead air before
# the first "Verifying…" card could possibly appear. Sized instead to cover
# roughly the first two examine rounds (GATE_FIRST_ROUND + GATE_ROUND_SIZE), so
# the candidates the gate reaches SOONEST get real text and the deep tail --
# lower embed-score by construction, and re-scraped by phase 5 anyway if it
# survives to the judge -- rides its teaser.
REED_ENRICH_PRE_GATE_CAP = 100
# The same budget for Adzuna (_enrich_adzuna_full_text), set lower on purpose. Every
# argument above applies, but each Adzuna fetch is a ~100KB HTML page rather than a
# small JSON body and the host 429s after a handful of rapid requests, so it runs at
# ADZUNA_DETAIL_MAX_WORKERS(3) rather than Reed's 12 -- roughly a quarter of the
# throughput per wave. 40 keeps its worst case comparable to Reed's measured 4.1s
# while still covering the whole first examine round (GATE_FIRST_ROUND=20) plus
# headroom, which is the slice that actually decides what the user sees first.
ADZUNA_ENRICH_PRE_GATE_CAP = 40
# Absolute cutoff on rank_gate's 0-100 fit score, replacing the old relative
# bottom-20%-of-whatever-batch trim (RANK_AUTOREJECT_FRACTION). MID_MODEL is a
# materially stronger model now (see full_auto.py's model tier comments), so
# its numeric score is worth trusting as an absolute judgment rather than only
# a relative ranking within whatever batch happened to be fed in -- anything
# scoring below this is dropped regardless of how large the surviving pool is,
# so the expensive judge never spends a call on a candidate the mid tier
# already knows doesn't fit.
# Raised from 40 -> 55: live runs were showing e.g. "85 gate survivors -> 85
# judge-eligible" -- literally nothing scored below 40, which made this floor
# a no-op rather than a real cutoff. 40 out of a 0-100 "how well does this fit"
# scale is a low bar (below-average-but-not-terrible still clears it); 55
# requires an actual above-the-middle score. The MIN_RESULTS rank-side floor
# backfill below still guarantees a cluster with any gate survivors reaches
# the judge with at least MIN_RESULTS candidates, so raising this can't
# starve a cluster to zero -- it can only promote the harsher floor's
# rejects back in when a cluster is otherwise thin.
# Lowered again 55 -> 50 as the other half of the RANK_TARGET_POOL widening:
# this floor is now a "not clearly a no" bar rather than a selection mechanism.
# Selection is done by taking the best JUDGE_POOL of up to RANK_TARGET_POOL
# approvals, which is a strictly better instrument -- an absolute cutoff set
# high enough to select is also high enough to starve a cluster (which is what
# 55 did to a 5-candidate cluster), whereas a low floor plus a wide pool plus a
# top-N cut cannot. Anything at or above the midpoint of the mid tier's own
# 0-100 scale proceeds; ordering after that is _selection_score's job.
RANK_REJECT_SCORE_FLOOR = 50
# Same-source posting-volume signal (scam/CV-farming detection, see
# _company_title_counts): a company posting at least this many DIFFERENT
# titles in one run's discovery is surfaced to the final judge as a hint --
# never a hard drop by itself, a legitimate high-volume recruiter/ATS
# aggregator can trip this too.
TEMPLATE_FACTORY_TITLE_THRESHOLD = 4
# Cross-site duplicate-content verification (scam/CV-farming corroboration,
# see full_auto.verify_not_duplicated): caps how many extra search calls one
# run will spend confirming a judge-flagged "scam_suspect" pick, regardless
# of how many are flagged.
SCAM_VERIFY_MAX_PER_RUN = 5


def get_pipeline_caps() -> dict:
    """Current pipeline cap constants, for the Settings/Analytics page's
    per-role-track table -- lets a "stopped because: absolute pool cap" row be
    checked against the actual number instead of the reader needing to know it
    from memory. Not run-specific: these are just today's live constants, the
    same for every run until this module (or full_auto.FINAL_PICKS) is edited."""
    import full_auto as _fa  # lazy: see run_search_task
    return {
        "rank_examine_budget": RANK_EXAMINE_BUDGET,
        "rank_target_pool": RANK_TARGET_POOL,
        "judge_pool": JUDGE_POOL,
        "judge_pool_floor": JUDGE_POOL_FLOOR,
        "rank_reject_score_floor": RANK_REJECT_SCORE_FLOOR,
        "target_pool_per_round": TARGET_POOL,
        "min_results_floor": MIN_RESULTS,
        "final_picks": _fa.FINAL_PICKS,
    }


def _external_id(engine, job: dict) -> str:
    return engine.make_job_id(job.get("board", ""), job.get("url", ""))


# Adzuna's /jobs/land/ad/... click-tracking redirect resolves 200 OK but is just a
# "you're being redirected" interstitial, not the posting -- full_auto's
# _looks_like_redirect_stub already catches this by content, but only after paying
# for a full fetch (with retries). The URL pattern itself is a free, pre-fetch tell.
_KNOWN_DEAD_END_URL_RE = re.compile(r"/jobs/land/ad/")


def _has_judgeable_text(job: dict) -> bool:
    """Whether this candidate already carries enough real text for the final
    judge to assess it -- a scraped/enriched full_text, an ATS description
    (which arrives whole), or a snippet long enough to stand in for one.

    Deliberately NOT the negation of _needs_full_scrape, which answers the
    different question "would a phase-5 fetch help". Those two came apart on
    exactly one case and it mattered: an un-enriched Adzuna row. Its URL is a
    /jobs/land/ad/ interstitial, so a fetch cannot help and _needs_full_scrape
    correctly returns False -- but the text it's stuck with is a 500-char
    company blurb. Reading that False as "text is fine" handed those rows
    RICH_TEXT_SELECTION_BONUS in _selection_score, i.e. the most text-starved
    candidates in the store were being PREFERENTIALLY promoted into the judge
    pool over candidates the judge could actually read."""
    if job.get("_has_full_text"):
        return True
    if canonical_key(job.get("board")) in ATS_KEYS:
        return True
    return len((job.get("snippet") or "").strip()) >= SNIPPET_SUFFICIENT_CHARS


def _needs_full_scrape(job: dict) -> bool:
    """Whether phase 5 should bother reading this job's real page before final
    evaluation. ATS-sourced snippets (greenhouse/lever/ashby/workable/
    recruitee/personio) already carry the full posting description -- they
    never need it. Everything else (Reed/Adzuna/Google Jobs/etc.) only needs
    it when its snippet is too short to judge seniority/requirements from,
    which is the actual cost driver: most of a run's full-page fetches (and
    the anti-bot blocking they trigger) buy nothing over what the API already
    handed us.

    A False here means "don't spend a fetch on this one", which is NOT the same
    as "this one has enough to judge" -- see _has_judgeable_text."""
    if _has_judgeable_text(job):
        return False
    if _KNOWN_DEAD_END_URL_RE.search(job.get("url") or ""):
        return False  # known-dead redirect stub -- skip straight to snippet fallback
    return True


def _selection_score(j: dict) -> float:
    """Ordering key for filling the judge pool: the mid tier's own 0-100 fit
    score, plus RICH_TEXT_SELECTION_BONUS for a candidate whose text is already
    good enough to judge without a phase-5 page fetch.

    Deliberately SEPARATE from `_rank_score`, which stays exactly what the model
    said. Three things depend on that separation: the card's "Fit estimate N/100"
    chip shows an unmodified model score; RANK_REJECT_SCORE_FLOOR is tested
    against the unmodified score, so already-scraped text can never lift a
    genuinely poor job over the floor; and gate_cache still stores the model's
    own number, so the bonus can be retuned without invalidating a single cached
    score. Only ORDER changes -- which of two acceptable candidates gets the
    judge slot."""
    return j.get("_rank_score", 50.0) + (RICH_TEXT_SELECTION_BONUS if _has_judgeable_text(j) else 0.0)


# Free, high-confidence seniority pre-reject: a junior/graduate candidate will never
# get a Director/VP role and a senior candidate won't take an internship. Matched
# against the job TITLE only, so it never fires on a stray body-text mention.
# Note: literal "senior"/"junior" are matched as their own tokens (not folded into
# a broader word list like "lead" would be) since "lead" collides with legitimate
# titles a junior candidate might target, e.g. "Lead Generation Specialist" --
# _SENIOR_BAND above already carries "lead" for _heuristic_prescreen's OWN
# seniority-label check, which is a different, safer use (matched against the
# candidate's stated seniority text, not every job title in the feed).
_SENIOR_TITLE_RE = re.compile(
    r"\b(senior|director|vice[- ]president|vp|head of|principal|chief|c[tefo]o|partner)\b", re.I)
_JUNIOR_TITLE_RE = re.compile(
    r"\b(junior|intern(ship)?|graduate|placement|apprentice(ship)?|trainee|entry[- ]level)\b", re.I)
_JUNIOR_BAND = ("intern", "graduate", "entry", "junior", "student", "trainee", "apprentice", "placement")
_SENIOR_BAND = ("senior", "lead", "principal", "head", "director", "manager",
                "staff", "vp", "chief", "executive", "president")


def _heuristic_prescreen(scored: list[dict], eng_profile: dict) -> tuple[list[dict], int]:
    """Drop obvious seniority mismatches by title before any LLM gate spends a token
    on them. Only fires when the profile's seniority is unambiguously junior OR senior
    (mid-level profiles are left untouched), and only on unambiguous title tokens --
    everything else passes through to the soft gate. Returns (kept, dropped_count)."""
    seniority = (eng_profile.get("seniority") or "").lower()
    is_junior = any(b in seniority for b in _JUNIOR_BAND)
    is_senior = (not is_junior) and any(b in seniority for b in _SENIOR_BAND)
    if not (is_junior or is_senior):
        return scored, 0
    reject_re = _SENIOR_TITLE_RE if is_junior else _JUNIOR_TITLE_RE
    kept, dropped = [], 0
    for j in scored:
        if reject_re.search(j.get("title") or ""):
            dropped += 1
        else:
            kept.append(j)
    return kept, dropped


def _derive_tags(job: dict, skills: list[str], seniority: str | None) -> list[str]:
    """Display-only tags: profile skills that actually appear in the role text."""
    text = f"{job.get('title','')} {job.get('full_text', job.get('snippet',''))}".lower()
    tags = [s for s in skills if s.lower() in text][:5]
    if seniority and seniority.lower() in text and seniority not in tags:
        tags.insert(0, seniority)
    return tags


def _salary_text(job: dict) -> str | None:
    text = job.get("full_text", "") or job.get("snippet", "")
    m = re.search(r"[£$€]\s?\d[\d,]*\s?(?:k|,\d{3})?\s?(?:-|to|–)\s?[£$€]?\s?\d[\d,]*\s?k?", text)
    return m.group(0).strip() if m else None


SNAPSHOT_SAMPLE_SIZE = 3  # sample roles kept per pipeline stage (see _sample_stage)


def _sample_stage(items, n: int = SNAPSHOT_SAMPLE_SIZE) -> list[dict]:
    """A few random roles from one pipeline stage, for the Settings > Snapshot
    panel. Random rather than head-of-list on purpose: every stage from the
    embedding onward is score-sorted, so the first N would always be that
    stage's best and would never show what it's actually letting through.

    Handles both shapes the pipeline carries: plain dicts (every stage except
    the pool) and JobSeen ORM rows (the pool). `url` is the normalised key every
    source is mapped onto (Adzuna's redirect_url included) -- there is no `link`.

    For the "rank_rejected" stage specifically, items carry rank_gate's
    _rank_score/_rank_note (see full_auto.rank_gate) -- surfaced here as `note`
    so a borderline drop is auditable (why it scored below the cutoff) instead
    of just vanishing. Absent on every other stage, where `_field` returns ""
    and `note` is simply omitted."""
    def _field(it, key: str) -> str:
        val = it.get(key) if isinstance(it, dict) else getattr(it, key, None)
        return (val or "").strip() if isinstance(val, str) else (val or "")

    pool = list(items or [])
    picked = random.sample(pool, n) if len(pool) > n else pool
    out = []
    for it in picked:
        entry = {"title": _field(it, "title"), "company": _field(it, "company"),
                  "url": _field(it, "url")}
        score = it.get("_rank_score") if isinstance(it, dict) else getattr(it, "_rank_score", None)
        if score is not None:
            note = _field(it, "_rank_note")
            entry["note"] = f"rank {score:.0f}" + (f" — {note}" if note else "")
        out.append(entry)
    return out


# The judge's fit_level grades (full_auto's _FINAL_EVAL_SCHEMA), mapped to the
# label the card leads with. Kept here rather than in the frontend so an
# unrecognised grade degrades to the strong/backup fallback below instead of
# rendering a raw enum at the user.
_VERDICT_GRADES = ("very_strong", "strong", "ok", "stretch")


def _verdict_of(entry: dict) -> str | None:
    """The pick's verdict grade. Falls back to the list it landed in for a
    verdict judged before fit_level existed (FINAL_EVAL_PROMPT_VERSION < 8) or
    for an inconclusive-call fallback pick, so the card always has something
    honest to lead with rather than a blank corner."""
    level = (entry.get("fit_level") or "").strip().lower()
    if level in _VERDICT_GRADES:
        return level
    if entry.get("strong_fit") is True:
        return "strong"
    if entry.get("strong_fit") is False:
        return "ok"
    return None


def _compose_analysis(entry: dict) -> str:
    """The card's analysis text. RoleCard.tsx splits on the §-prefixed markers.

    Always visible (no marker): the cluster-label/closest-match notes, then
    the headline -- `role_type` (functional classification) and `summary`
    (this specific role's mission/duties) joined into ONE sentence pair, in
    that order, per full_auto's step F no-overlap rule (they're written by the
    model knowing they'll be displayed together, so `summary` adds new
    information rather than restating `role_type`). Behind the "Show more"
    toggle: `§qualification` (a direct qualified-or-not verdict, then a
    concern count and its bullets) and `§apply-highlights` (what to put in
    front of this specific employer).

    `§apply-highlights` replaced `§ai-reasoning` at FINAL_EVAL_PROMPT_VERSION
    23. The old block rendered `top_match_reason`, a narrative arguing why the
    role fitted -- which restated what the badge, the headline and the
    qualification verdict had already said three ways over, and gave the
    candidate nothing to act on. The judge now spends those output tokens on
    the one thing the rest of the card can't cover: which of the JD's asks
    this employer will actually screen on, and which of the candidate's own
    named projects/tools to lead with against them. Rows judged under 22 or
    earlier still carry `§ai-reasoning` and keep rendering (RoleCard.tsx
    parses both) until they're next re-judged.

    `role_type` used to render as its own always-visible `§role-type` block
    after the `summary` headline -- the two are independently-generated model
    fields that, despite an existing "distinct from summary" instruction, in
    practice often restated each other (e.g. "a charity data-and-impact role
    combining analysis..." next to "...keep records accurate, analyse
    outcomes..."). Folding them into one headline plus strengthening the
    prompt's no-overlap rule (FINAL_EVAL_PROMPT_VERSION 13) fixes the visible
    redundancy without losing the model's two-step reasoning (classify, then
    describe).

    This replaces the old "Matches N/M core requirements" ratio headline,
    which was unreliable -- the judge freely re-enumerates a fresh
    requirements checklist per job (see full_auto's reasoning step D), with a
    total that doesn't correlate with the fit_level verdict badge shown right
    next to it. `requirements` is still generated as a reasoning scaffold but
    deliberately never surfaced here."""
    parts = []
    if entry.get("_cluster_label"):
        parts.append(f"Matched via: {entry['_cluster_label']} track")
    if entry.get("strong_fit") is False:
        parts.append("⚠ Closest available match — no role fully met the bar this run.")

    headline = " ".join(
        p.strip() for p in (entry.get("role_type"), entry.get("summary")) if p and p.strip()
    )
    if headline:
        parts.append(headline)

    qualification: list[str] = []
    if entry.get("can_do_fit"):
        qualification.append(f"✓ {entry['can_do_fit'].strip()}")
    concerns = [str(c).strip() for c in (entry.get("concerns") or []) if str(c).strip()]
    if concerns:
        noun = "aspect" if len(concerns) == 1 else "aspects"
        qualification.append(f"⚠ You lack {len(concerns)} {noun}:")
        qualification.extend(f"- {c}" for c in concerns)
    if qualification:
        parts.append("§qualification")
        parts.extend(qualification)

    # Two model fields rendered as one block: the screened-on list becomes the
    # lead sentence, the guidance continues from it (the judge writes them
    # knowing they display that way -- see _FINAL_EVAL_SCHEMA's closing note).
    # Either may be absent on its own without suppressing the other.
    highlights: list[str] = []
    filters_on = [str(f).strip() for f in (entry.get("filters_on") or []) if str(f).strip()]
    if filters_on:
        highlights.append(f"This role likely filters on: {', '.join(filters_on)}.")
    if entry.get("highlight"):
        highlights.append(entry["highlight"].strip())
    if highlights:
        parts.append("§apply-highlights")
        parts.extend(highlights)
    elif entry.get("top_match_reason"):
        # Pre-v23 verdict served from cache -- keep its narrative rather than
        # dropping the only reasoning text such a row has.
        parts.append("§ai-reasoning")
        parts.append(entry["top_match_reason"].strip())

    return "\n".join(p for p in parts if p)


def _compose_fallback_warning(role_clusters: list[dict], fallback_notes: dict[int, set[str]]) -> str | None:
    """Turn per-cluster fallback tags (set by _cluster_candidate_queues's
    "broadened"/"floor_fallback" and _run_engine_pipeline's "gate_fallback"/
    "eval_fallback"/"cluster_skipped") into a user-facing message. Names the
    specific role when only one cluster needed a fallback; generic wording
    otherwise."""
    affected = [idx for idx, tags in fallback_notes.items() if tags]
    if not affected:
        return None
    if len(role_clusters) <= 1 or len(affected) > 1:
        return ("Your profile or filters look strict — showing the best available "
                "matches anyway; some may be a stretch.")
    idx = affected[0]
    label = _cluster_label(role_clusters[idx])
    tags = fallback_notes[idx]
    if "cluster_skipped" in tags:
        return f'"{label}" wasn\'t searched this run — it\'ll come up again in a future search.'
    if "eval_fallback" in tags:
        return (f'"{label}" matches were thin this run — showing the closest available '
                f"instead of only confident picks.")
    if "gate_fallback" in tags:
        return f'Few roles cleared our sector/seniority screen for "{label}" — showing the closest matches found.'
    return f'"{label}" matches were sparse this run — showing the best available instead of only strong fits.'


# ── Identity hash ────────────────────────────────────────────────────────────
# The linchpin of the discovery store: a stable cross-source key so the same
# role found via two sources collapses to one identity, while two different
# roles at the same company stay distinct. Do NOT key on (title, company).
_TRACKING_PREFIXES = ("utm_", "gh_", "src", "ref", "source")
_AGGREGATOR_HOSTS = ("adzuna.", "serpapi.", "google.", "jsearch.")  # per-click redirects


def _canonical_url(url: str) -> str | None:
    if not url:
        return None
    try:
        s = urlsplit(url)
    except ValueError:
        return None
    if any(h in s.netloc.lower() for h in _AGGREGATOR_HOSTS):
        return None  # not a stable identity; fall through to text hash
    kept = [(k, v) for k, v in parse_qsl(s.query)
            if not any(k.lower().startswith(p) for p in _TRACKING_PREFIXES)]
    return urlunsplit((s.scheme.lower(), s.netloc.lower(), s.path.rstrip("/"),
                       urlencode(kept), ""))


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


# Legal-entity suffixes only -- deliberately excludes vaguer tokens like "Group"
# or "Co" that risk merging genuinely distinct companies. Applied only to company
# comparisons (identity_hash's fallback, _find_soft_duplicate, the DB pre-filter
# below), never to _norm generally, which also normalizes titles/locations.
_COMPANY_SUFFIX_RE = re.compile(
    r"[,\s]+(ltd|limited|llc|plc|inc|incorporated|corp|corporation|gmbh|llp)\.?$"
)


def _norm_company(s: str) -> str:
    return _COMPANY_SUFFIX_RE.sub("", _norm(s)).strip()


# Trailing "(...)"/"- ..."/", ..." segment on a job title. Used ONLY by
# _norm_title_key, and only ever stripped when corroborated -- see below.
_TITLE_TRAILING_SEGMENT_RE = re.compile(
    r"\s*(?:[\(\[]([^)\]]{2,40})[\)\]]|[-–—,]\s*([^-–—,]{2,40}))\s*$"
)
# A requisition/reference number, which never distinguishes two real vacancies.
# The negative lookahead exempts a plausible YEAR (1900-2099): an intake year
# does distinguish two real vacancies -- "Data Engineering Intern (Fall 2026)"
# and "(Fall 2027)" are different cohorts and the later one is still open to
# apply for. Without it the \d{4,} branch ate the year and left a dangling
# "(fall", collapsing both cohorts onto one key. Real req numbers in the live
# store (6632, 1058, 6314, 1137503) are unaffected; a req number that happens to
# fall in the year range simply isn't stripped, which only ever under-merges.
_TITLE_REQ_NUMBER_RE = re.compile(
    r"\s*[\(\[]?\s*(?:req|ref|requisition|job)?\s*[#:\-]?\s*(?:id|no\.?)?\s*"
    r"(?!(?:19|20)\d{2}\b)\d{4,}\s*[\)\]]?\s*$",
    re.I,
)
# Never strip a segment carrying one of these: a segment that names a level is
# describing the ROLE, not the place, even if it also happens to contain a
# place-name token (a company named after a city, "Analyst - London Lead").
_TITLE_LEVEL_TOKENS = {
    "senior", "junior", "lead", "principal", "graduate", "trainee", "head",
    "director", "apprentice", "intern", "associate", "staff", "chief", "manager",
}
# Connectives and geographic qualifiers that may legitimately pad a location
# suffix ("Cork City", "West Coast", "London or Antwerp based"). Allowed as
# leftovers by the subset test below; on their own they are never evidence.
_TITLE_LOCATION_FILLER = {
    "city", "and", "the", "area", "areas", "region", "regional", "metro",
    "greater", "county", "north", "south", "east", "west", "central", "wide",
    "site", "field",
}


def _norm_title_key(title: str, location: str = "") -> str:
    """Normalized title for CROSS-RUN family matching (_decided_role_keys): the
    same vacancy advertised in several cities must collapse to one key.

    The trailing-segment strip is CORROBORATED, never unconditional -- it fires
    only when the segment shares a real place-name token with the row's OWN
    location string, which is the only evidence available that the suffix names a
    place rather than a specialisation. Measured over the live store's 6,569
    rows, 3,934 titles carry a trailing segment and only 85 are locations; the
    other 3,849 are things like "Data Engineer - AWS", "Principal Engineer
    (Microsoft)" and "Senior DevSecOps Engineer - GCP". Stripping unconditionally
    would merge AWS with Azure to catch those 85 -- so the location test is the
    whole design, not a refinement of it.

    Deliberately NOT stripped:
    * Years. "Graduate Programme 2027" and "... 2026" are different intakes at
      the same employer, and the candidate can still apply to the later one.
    * Seniority, and anything mid-title. "Senior Data Analyst" must never
      collapse into "Data Analyst".

    Under-firing is the safe direction and is expected: a postcode-style location
    ("CV32UN") shares no token with anything, so nothing is stripped, no family
    matches, and no role is suppressed."""
    base = _norm(title)
    if not base:
        return ""
    base = _TITLE_REQ_NUMBER_RE.sub("", base).strip()
    loc_tokens = _location_tokens(location)
    # Loop so "Analyst - London (Hybrid)" peels both trailing segments. Bounded
    # to keep a pathological title from spinning.
    for _ in range(3):
        if not loc_tokens:
            break
        m = _TITLE_TRAILING_SEGMENT_RE.search(base)
        if not m:
            break
        segment = m.group(1) or m.group(2) or ""
        seg_tokens = _location_tokens(segment)
        # The segment must BE a location, not merely CONTAIN one. Requiring only
        # an intersection strips "Non-CDL Driver Des Moines" down to "1st shift -
        # non" for a Des Moines row -- the place name is embedded in a segment
        # that also carries the job function, and the function is what's lost.
        # So: it must overlap the row's location AND have nothing left over
        # afterwards except geographic filler.
        leftover = seg_tokens - loc_tokens - _TITLE_LOCATION_FILLER
        if (not seg_tokens or not (seg_tokens & loc_tokens) or leftover
                or (seg_tokens & _TITLE_LEVEL_TOKENS)):
            break
        base = base[:m.start()].strip()
    base = base.strip(" -–—,:|").strip()
    # A strip that ate the whole title means the heuristic misfired; the raw
    # normalized title is always a safe fallback (it just matches less).
    return base if len(base) >= 3 else _norm(title)


def identity_hash(job: dict) -> str:
    canon = _canonical_url(job.get("url", ""))
    basis = canon or f"{_norm_company(job.get('company',''))}|{_norm(job.get('title',''))}|{_norm(job.get('location',''))}"
    return hashlib.sha1(basis.encode()).hexdigest()


# identity_hash's URL-canonical branch treats two different real URLs (e.g. Reed's own
# jobUrl vs. a Google-indexed employer link) as different identities even when they're
# the same real posting, and its text-hash fallback branch requires an exact location
# match even when two sources just report location at different granularity (Adzuna's
# regional display_name vs. a structured neighbourhood-level address). This corroborated
# soft-match catches that case without keying on (title, company) alone: it additionally
# requires the two locations to share a real place-name token, so two genuinely distinct
# open reqs with the same title at the same company in different real locations still
# stay separate.
_LOCATION_STOPWORDS = {
    "uk", "gb", "united", "kingdom", "great", "britain", "england", "scotland",
    "wales", "ireland", "usa", "us", "remote", "hybrid", "onsite", "office",
    "home", "based",
}


def _location_tokens(location: str) -> set[str]:
    return {t for t in re.findall(r"[a-z]+", (location or "").lower())
            if len(t) > 2 and t not in _LOCATION_STOPWORDS}


def _find_soft_duplicate(job: dict, candidates: list["JobSeen"]) -> "JobSeen | None":
    title, company = _norm(job.get("title", "")), _norm_company(job.get("company", ""))
    if not title or not company:
        return None
    job_tokens = _location_tokens(job.get("location", ""))
    if not job_tokens:
        return None
    for cand in candidates:
        if _norm(cand.title) == title and _norm_company(cand.company or "") == company \
                and job_tokens & _location_tokens(cand.location or ""):
            return cand
    return None


# Judge-pool near-duplicate suppression (see _suppress_judge_duplicates).
# _find_soft_duplicate above deliberately requires a shared location token, so a
# recruiter template posted verbatim across several cities (same company, same
# title, identical description text) stays N separate JobSeen rows -- correct
# for the discovery store, but wasteful at the judge: a live run sent two
# word-for-word identical "Hypercreate Ltd / Data Analyst" teasers (different
# city each) to the expensive judge as two full slots. The text-prefix
# requirement is what keeps this narrower than title+company alone: two
# genuinely distinct openings with the same title at the same company will have
# differently-worded descriptions and both proceed.
_DUP_TEXT_PREFIX_CHARS = 400   # normalized chars that must match to call it the same vacancy text
_DUP_TEXT_MIN_CHARS = 120      # below this there's no real evidence either way -- never collapse


def _dup_key(j: dict) -> tuple | None:
    title = _norm(j.get("title", ""))
    if not title:
        return None
    text = re.sub(r"\s+", " ", (j.get("full_text") or j.get("snippet") or "").lower()).strip()
    if len(text) < _DUP_TEXT_MIN_CHARS:
        return None   # no text evidence either way -- never collapse
    company = _norm_company(j.get("company", ""))
    if company:
        return (company, title, text[:_DUP_TEXT_PREFIX_CHARS])
    # Aggregator listings (careerjet/jobviewtrack and similar) arrive with a
    # BLANK company and a unique per-listing redirect URL, so neither the
    # company-keyed path here nor discovery's URL-canonical identity_hash ever
    # collapses their verbatim reposts -- a live UAE run surfaced four identical
    # "Marketing Assistant / Dubai" cards straight into the provisional view.
    # With no company to key on, fall back to title + location + text prefix.
    # Location is included (not just title+text) so a genuinely different-city
    # repost of the same template stays a separate posting, and the text-prefix
    # requirement (already enforced above) keeps two thin, evidence-free generic
    # titles from ever being merged.
    loc = "|".join(sorted(_location_tokens(j.get("location", ""))))
    return ("", title, loc, text[:_DUP_TEXT_PREFIX_CHARS])


# Cross-BOARD syndication: the same vacancy reached the judge twice because the two
# copies' text is only near-identical, not prefix-identical. A live run judged
# "BI Analyst / Erin Associates" as two separate jobs -- one from reed.co.uk, one from
# jobs.womenforhire.com -- and the judge itself noticed, writing "Duplicate of Job 5"
# as its reason for the second. _dup_key above cannot catch this by construction: the
# two boards wrap the same description in different chrome, truncate it at different
# lengths, and (once one copy has been enriched or scraped and the other hasn't) hold
# very different amounts of it, so their 400-char prefixes never match.
#
# Compared as word-4-gram SETS by CONTAINMENT (shared / smaller side) rather than
# Jaccard, because the two copies routinely differ enormously in length -- a 455-char
# Reed teaser against a 4,000-char scraped page is the same vacancy with a Jaccard of
# ~0.1. Containment asks the right question: is the shorter copy essentially wholly
# inside the longer one.
#
# Kept narrow, since this is the check with the widest reach: same normalized company
# AND same normalized title are both required first (so this only ever adjudicates
# candidates _dup_key was already trying to tell apart), plus enough shingles on the
# shorter side to be real evidence. A false merge costs one judge slot on a
# near-identical listing and is recoverable -- the dropped copy keeps its cached rank
# score and takes no verdict, so it can resurface on a later run if the kept copy dies.
_DUP_SHINGLE_N = 4
_DUP_CONTAINMENT = 0.65    # of the SHORTER text's shingles, how many the longer also has
_DUP_MIN_SHINGLES = 25     # ~28 words of real content before containment means anything


def _text_shingles(j: dict) -> frozenset:
    words = re.findall(r"[a-z0-9]+", (j.get("full_text") or j.get("snippet") or "").lower())
    if len(words) < _DUP_SHINGLE_N + _DUP_MIN_SHINGLES - 1:
        return frozenset()
    return frozenset(
        tuple(words[i:i + _DUP_SHINGLE_N]) for i in range(len(words) - _DUP_SHINGLE_N + 1)
    )


def _same_vacancy(a: frozenset, b: frozenset) -> bool:
    if len(a) < _DUP_MIN_SHINGLES or len(b) < _DUP_MIN_SHINGLES:
        return False
    return len(a & b) / min(len(a), len(b)) >= _DUP_CONTAINMENT


def _suppress_judge_duplicates(
    rank_by_cluster: dict[int, list[dict]],
    decided_keys: set[tuple[str, str]] | None = None,
) -> tuple[int, int, dict[tuple[str, str], int]]:
    """Drops near-duplicate postings from the per-cluster judge-eligible lists
    in place, keeping only the highest-rank_gate-scored copy of each (the lists
    arrive _rank_score-sorted descending, so the first copy seen is the best).
    Runs BEFORE _fair_allocate, so a freed slot goes to the next-ranked real
    candidate instead of just shrinking the judge pool. Suppression is
    judge-pool-only: the dropped copy stays a gate survivor, keeps its cached
    rank score, and gets no persisted verdict -- if the kept copy disappears at
    source, the duplicate can still surface on a future run.

    Three tests, in cost order: an exact normalized-prefix key (_dup_key, catches
    a recruiter template reposted verbatim), a near-identical-text check within
    the same company+title (_same_vacancy, catches the same vacancy syndicated to
    a second board with different chrome and truncation), and -- when
    `decided_keys` is supplied -- a CROSS-RUN family check against roles the user
    has already saved or applied to (see _decided_role_keys).

    This is the placement that actually saves something: running before
    _fair_allocate means a slot freed by a family match goes to a real candidate,
    rather than the duplicate being judged and only then hidden.

    Returns (within_run_suppressed, decided_family_suppressed, per_family_counts).
    The two counts stay SEPARATE: folding them together would destroy the ability
    to tell an aggregator repost from a family match, and would make the Settings
    panel's "same employer, title & text" wording false."""
    decided_keys = decided_keys or set()
    seen: set[tuple] = set()
    # (company, title) -> the shingle sets of the copies kept so far under it.
    kept_texts: dict[tuple[str, str], list[frozenset]] = defaultdict(list)
    suppressed = 0
    decided_hits: dict[tuple[str, str], int] = defaultdict(int)
    for idx, jobs in rank_by_cluster.items():
        kept = []
        for j in jobs:
            fam = _family_key(j.get("company", ""), j.get("title", ""), j.get("location", ""))
            if (fam is not None and fam in decided_keys
                    and decided_hits[fam] < DECIDED_FAMILY_SUPPRESS_MAX):
                decided_hits[fam] += 1
                if DECIDED_FAMILY_SUPPRESS_ENABLED:
                    continue
                # Shadow mode: counted above, but still judged and shown.
            key = _dup_key(j)
            if key is not None and key in seen:
                suppressed += 1
                continue
            company, title = _norm_company(j.get("company", "")), _norm(j.get("title", ""))
            # Blank company is deliberately excluded from the near-text check: with
            # no employer to anchor on, two unrelated postings sharing a generic
            # title and boilerplate could merge. _dup_key's own blank-company path
            # (title + location + exact prefix) still covers aggregator reposts.
            shingles = _text_shingles(j) if company and title else frozenset()
            group = kept_texts[(company, title)] if shingles else None
            if group is not None and any(_same_vacancy(shingles, s) for s in group):
                suppressed += 1
                continue
            if key is not None:
                seen.add(key)
            if group is not None:
                group.append(shingles)
            kept.append(j)
        rank_by_cluster[idx] = kept
    return suppressed, sum(decided_hits.values()), dict(decided_hits)


def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


# ── Discovery store: upsert + selection helpers ─────────────────────────────

def _upsert_discovered(db: Session, profile_id: int, raw_jobs: list[dict]) -> tuple[int, int, int]:
    """Discovery is cheap and runs fully every time. New identities get
    state='new'; seen ones refresh last_seen. A role edited at source after we
    last enriched it is re-queued (state -> 'new').

    Returns (inserted, refreshed, requeued) for logging."""
    now = datetime.utcnow()
    # Same identity can appear twice in one batch (e.g. an aggregator listing the
    # same role under two categories). autoflush is off, so a freshly-added row is
    # not yet visible to the SELECT below; track it here to avoid a duplicate INSERT.
    seen_this_batch: dict[str, JobSeen] = {}
    # Every row touched this batch (new or refreshed), for _find_soft_duplicate's
    # in-batch pass below -- also needed because a freshly-added row isn't visible
    # to a SELECT yet (autoflush is off), so the same-run duplicate this was built
    # for (two sources surfacing one real posting in one discovery pass) would
    # otherwise slip past the DB-backed soft-match query entirely.
    batch_rows: list[JobSeen] = []
    inserted = refreshed = requeued = 0
    for job in raw_jobs:
        title = job.get("title", "")
        if not title:
            continue
        h = identity_hash(job)
        upd_dt = _parse_iso(job.get("updated_at")) if job.get("updated_at") else None
        posted_dt = _parse_iso(job.get("posted_at")) if job.get("posted_at") else None
        expires_dt = _parse_iso(job.get("expires_at")) if job.get("expires_at") else None

        existing = seen_this_batch.get(h)
        if existing is None:
            existing = db.execute(
                select(JobSeen).where(
                    JobSeen.profile_id == profile_id, JobSeen.identity_hash == h
                )
            ).scalar_one_or_none()

        if existing is None:
            # No exact identity match -- the same real posting can still reach us
            # under a different identity_hash (a different URL per source, or a
            # location string at different granularity), see _find_soft_duplicate.
            # Check in-batch candidates first, then the persisted store (coarse
            # SQL company pre-filter, refined by the exact normalized comparison
            # inside _find_soft_duplicate).
            existing = _find_soft_duplicate(job, batch_rows)
            if existing is None:
                company_norm = _norm_company(job.get("company", ""))
                # Exact match covers "DB already has the stripped/short form,
                # incoming has the suffix" (company_norm strips down to it);
                # the LIKE prefix covers the reverse (DB has the longer
                # suffixed form, incoming is already short) -- SQL can't apply
                # _norm_company to the stored value, so a prefix match stands
                # in for it here. Still just a coarse pre-filter: the exact
                # decision happens in _find_soft_duplicate below.
                db_candidates = db.execute(
                    select(JobSeen).where(
                        JobSeen.profile_id == profile_id,
                        or_(
                            func.lower(JobSeen.company) == company_norm,
                            func.lower(JobSeen.company).like(company_norm + "%"),
                        ),
                    )
                ).scalars().all() if company_norm else []
                existing = _find_soft_duplicate(job, db_candidates)

        if existing is None:
            row = JobSeen(
                profile_id=profile_id, identity_hash=h, source=job.get("board", ""),
                title=title, company=job.get("company"), location=job.get("location"),
                url=job.get("url"), snippet=job.get("snippet", ""),
                state="new", source_updated_at=upd_dt, first_seen=now, last_seen=now,
                posted_at=posted_dt, expires_at=expires_dt,
                posted_at_approx=bool(job.get("posted_at_approx")) if posted_dt else None,
                seen_days=1,
            )
            db.add(row)
            seen_this_batch[h] = row
            batch_rows.append(row)
            inserted += 1
        else:
            # ORDER MATTERS: this reads existing.last_seen to decide whether today
            # has already been counted, so it must run BEFORE last_seen is
            # overwritten below. The two lines look independent; reversing them
            # turns seen_days into a per-RUN counter (MAX_SEARCHES_PER_DAY allows
            # 6 a day), which measures how often the user searches rather than how
            # long the employer has been advertising. See JobSeen.seen_days.
            if existing.last_seen is None or existing.last_seen.date() != now.date():
                existing.seen_days = (existing.seen_days or 1) + 1
            existing.last_seen = now
            # A soft-duplicate match means a different source described the same
            # posting -- prefer whichever source's snippet is more complete rather
            # than freezing on whichever was seen first (a mangled/truncated
            # snippet from one source shouldn't outlive a cleaner one from another).
            new_snippet = job.get("snippet", "") or ""
            if len(new_snippet) > len(existing.snippet or ""):
                existing.snippet = new_snippet
            # Keep the EARLIEST posting date any source has claimed, and backfill
            # when we hold none. Earliest rather than latest because a job that
            # reaches us from two sources is usually one posting an aggregator has
            # re-listed, and taking the newer date would let a months-old listing
            # launder itself fresh every time it's re-syndicated -- the precise
            # thing the staleness signal exists to catch. Expiry is the opposite:
            # an employer can genuinely extend a closing date, so the newest wins.
            incoming_approx = bool(job.get("posted_at_approx"))
            if posted_dt and (existing.posted_at is None or posted_dt < existing.posted_at):
                existing.posted_at = posted_dt
                existing.posted_at_approx = incoming_approx
            elif (posted_dt and not incoming_approx and existing.posted_at_approx
                    and posted_dt == existing.posted_at):
                # Same date, better provenance: a source that genuinely states a
                # posting date supersedes an aliased updated_at, so the age tag can
                # stop hedging. Earliest-wins above already handles a different
                # date; this only upgrades what we know about an equal one.
                existing.posted_at_approx = False
            if expires_dt and (existing.expires_at is None or expires_dt > existing.expires_at):
                existing.expires_at = expires_dt
            if upd_dt and existing.source_updated_at and upd_dt > existing.source_updated_at:
                existing.state = "new"
                existing.source_updated_at = upd_dt
                # Posting changed at source -> invalidate the persisted scrape/verdict
                # so the fresh content is re-scraped and re-judged.
                existing.full_text = None
                existing.eval_verdict = None
                existing.eval_analysis = None
                existing.eval_signature = None
                existing.evaluated_at = None
                requeued += 1
            else:
                refreshed += 1
            seen_this_batch[h] = existing
            batch_rows.append(existing)
    db.commit()
    return inserted, refreshed, requeued


def _store_age_days(db: Session, profile_id: int) -> float:
    """Days since the OLDEST row in this profile's discovery store. 0.0 when the
    store is empty. See the call site in _run_engine_pipeline for why the age tag
    needs this rather than judging each row on its own first_seen."""
    oldest = db.execute(
        select(func.min(JobSeen.first_seen)).where(JobSeen.profile_id == profile_id)
    ).scalar()
    if not oldest:
        return 0.0
    return max(0.0, (datetime.utcnow() - oldest).total_seconds() / 86400.0)


def _new_rows(db: Session, profile_id: int, limit: int = STORE_SCORE_CAP) -> list[JobSeen]:
    """Fresh, unprocessed rows. Freshest first. The limit is the whole-store cap:
    relevance scoring runs over all of them (cheap, since embeddings are cached),
    so a genuinely good role can't be excluded by an arbitrary small slice.
    Excludes rows already confirmed dead/expired (dead_reason set) -- a fact
    about the URL that costs nothing to keep re-checking here since it's an
    indexed-free column filter, and saves every downstream stage from ever
    seeing a listing already known gone."""
    return db.execute(
        select(JobSeen)
        .where(JobSeen.profile_id == profile_id, JobSeen.state == "new",
               JobSeen.dead_reason.is_(None))
        .order_by(JobSeen.first_seen.desc())
        .limit(limit)
    ).scalars().all()


def _gate_reopened_rows(
    db: Session, profile_id: int, gate_sig: str, limit: int = STORE_SCORE_CAP,
) -> list[JobSeen]:
    """Rows the CHEAP gate retired under a DIFFERENT profile signature than the one
    this run is using -- i.e. rows whose only reason for being out of the pool is a
    verdict the candidate's own edits have since invalidated. See
    JobSeen.gate_signature for why this exists at all.

    Scoped tightly on purpose:
    * `eval_verdict IS NULL` -- only rows the gate dropped before the judge saw them.
      A row the expensive judge already ruled on is the judge's to resurface, under
      its own eval_signature rule (_backlog_rows), and re-opening a stored 'reject'
      here would route around the "a reject under the current signature is never
      resurfaced" invariant the whole pipeline is built on.
    * `gate_signature IS DISTINCT FROM :sig` -- an unchanged profile re-opens nothing,
      so this can never turn into re-gating the same rows every run. A NULL signature
      (retired before this column existed) counts as different, so the one-off effect
      of shipping this is that the existing backlog re-opens once, which is the
      intent.
    Re-opened rows are cheap: their screen/rank calls are served from gate_cache
    whenever the change didn't actually affect them, and they still have to clear
    RELEVANCE_FLOOR and win an examine slot on embed score like anything else."""
    return db.execute(
        select(JobSeen)
        .where(JobSeen.profile_id == profile_id, JobSeen.state == "enriched",
               JobSeen.dead_reason.is_(None), JobSeen.eval_verdict.is_(None),
               or_(JobSeen.gate_signature.is_(None), JobSeen.gate_signature != gate_sig))
        .order_by(JobSeen.last_seen.desc())
        .limit(limit)
    ).scalars().all()


def _backlog_rows(
    db: Session, profile_id: int, limit: int,
    *, verdicts: tuple[str, ...] | None = None, exclude_rejects: bool = False,
) -> list[JobSeen]:
    """Enriched-but-unshown rows, for resurfacing previously-seen roles across runs.
    Already-processed, so re-considering them is free apart from cache-served gate/
    rank/judge calls. Excludes confirmed-dead rows, same reasoning as _new_rows.
    `verdicts` restricts to a specific set of final-AI verdicts (e.g. only the roles
    the judge already liked -- 'strong'/'backup'); `exclude_rejects` instead keeps
    everything except an expensive-AI 'reject' (used as a thin-run empty-screen
    safety net). Rejects are never resurfaced by default -- re-piping a known reject
    every run only to have it filtered again at the judge cache wastes pool slots."""
    q = select(JobSeen).where(
        JobSeen.profile_id == profile_id, JobSeen.state == "enriched",
        JobSeen.dead_reason.is_(None),
    )
    if verdicts is not None:
        q = q.where(JobSeen.eval_verdict.in_(verdicts))
    elif exclude_rejects:
        q = q.where(or_(JobSeen.eval_verdict.is_(None), JobSeen.eval_verdict != "reject"))
    return db.execute(q.order_by(JobSeen.last_seen.desc()).limit(limit)).scalars().all()


def _rows_to_dicts(rows: list[JobSeen]) -> list[dict]:
    """JobSeen -> the dict shape the engine's filter/eval expect. A persisted
    full_text (scraped on a prior run) is preferred over snippet so the evaluator
    judges on the best text we have without re-scraping; the persisted final-AI
    verdict rides along so an unchanged profile can reuse it instead of re-judging."""
    return [{
        "board": r.source, "title": r.title, "company": r.company or "",
        "location": r.location or "", "url": r.url or "",
        "snippet": r.snippet or "", "full_text": r.full_text or r.snippet or "",
        "_identity": r.identity_hash,
        "_has_full_text": bool(r.full_text),
        # ISO strings rather than datetimes: these ride into full_auto, which is
        # DB-agnostic and formats them via _listing_age_tag. Often None -- see
        # JobSeen.posted_at on why an unknown date must stay unknown.
        "_posted_at": r.posted_at.isoformat() if r.posted_at else None,
        "_expires_at": r.expires_at.isoformat() if r.expires_at else None,
        "_posted_at_approx": bool(r.posted_at_approx),
        # Our OWN observation window, independent of anything a board claims --
        # see JobSeen.first_seen/seen_days. A lower bound on the ad's true age,
        # never an upper one, and never evidence that a listing is fresh.
        "_first_seen": r.first_seen.isoformat() if r.first_seen else None,
        "_seen_days": r.seen_days or 1,
        "_eval_verdict": r.eval_verdict,
        "_eval_signature": r.eval_signature,
        "_eval_analysis": r.eval_analysis,
    } for r in rows]


def _mark(db: Session, profile_id: int, identities: list[str], state: str,
          gate_sig: str | None = None) -> None:
    """`gate_sig` stamps WHICH profile the retirement decision was made under, so it
    can be re-opened when that profile changes -- see _gate_reopened_rows. Passed
    only for the 'enriched' mark; a 'shown' row is out of the pool for a reason that
    has nothing to do with a gate verdict."""
    if not identities:
        return
    values: dict = {JobSeen.state: state}
    if gate_sig:
        values[JobSeen.gate_signature] = gate_sig
    db.query(JobSeen).filter(
        JobSeen.profile_id == profile_id,
        JobSeen.identity_hash.in_(identities),
        JobSeen.state != "shown",   # never downgrade a shown row
    ).update(values, synchronize_session=False)
    db.commit()


def _persist_scrape(db: Session, profile_id: int, jobs: list[dict]) -> None:
    """Persist freshly-scraped page text so a resurfacing job isn't re-scraped. Only
    stores text that actually beats the snippet (a real fetch succeeded), so a blocked
    page that fell back to its snippet is retried next run rather than frozen."""
    by_id: dict[str, str] = {}
    for j in jobs:
        ident = j.get("_identity")
        ft = j.get("full_text") or ""
        if ident and len(ft) > len(j.get("snippet") or ""):
            by_id[ident] = ft[:8000]
    if not by_id:
        return
    rows = db.execute(
        select(JobSeen).where(
            JobSeen.profile_id == profile_id, JobSeen.identity_hash.in_(list(by_id))
        )
    ).scalars().all()
    for r in rows:
        r.full_text = by_id.get(r.identity_hash)
    db.commit()


def _enrich_reed_full_text(engine, db: Session, profile_id: int, jobs: list[dict]) -> int:
    """Fetch the REAL description for Reed candidates about to be gated, and
    persist it as their full_text.

    Reed's search API truncates its description to a ~455-char teaser (measured:
    min 453 / max 500 across a 361-row live sample), and nothing else fills that
    gap before the cheap stages run -- Phase 5's scrape, the only other source of
    real text, happens AFTER gate and rank. So screen_gate's seniority axis and
    rank_gate's DEPTH FIT score were judging a freshly-discovered Reed job on its
    opening blurb, never its requirements section: an audit of jobs the expensive
    judge disqualified on an experience bar ("3+ years as a Data Analyst") found
    the requirement present in the snippet for 1 of 24, and in the scraped
    full_text for 7. The cheap stages weren't miscalibrated, they were starved.

    Reed's per-job endpoint closes that for one plain HTTP call each (no LLM, no
    browser) -- see full_auto.fetch_reed_details. Two free downstream effects:
    _needs_full_scrape skips anything carrying full_text, so Phase 5 shrinks by
    however many are enriched here, and the text persists for every future run
    exactly like a scrape would.

    Mutates the passed dicts in place (full_text + _has_full_text, the latter
    being what _needs_full_scrape and full_auto._gate_job_id's cache-key
    richness marker both read). Returns how many were enriched."""
    return _enrich_pre_gate(engine, db, profile_id, jobs, "reed",
                            engine.reed_job_id, engine.fetch_reed_details)


def _enrich_adzuna_full_text(engine, db: Session, profile_id: int, jobs: list[dict]) -> int:
    """The Adzuna twin of _enrich_reed_full_text -- see full_auto's
    ADZUNA_DETAIL_ENRICH_ENABLED for the measurements behind it.

    Adzuna mattered more than Reed and was fixed later because it looked unfixable:
    its search API truncates at exactly 500 chars, it has no per-job detail route,
    and the tracking URL it hands out is a JS interstitial Phase 5 correctly refuses
    to treat as a posting. So an Adzuna row had NO path to real text at any stage,
    and the expensive judge was grading these on a company blurb. Adzuna's own
    /details/{ad_id} page turns out to serve the whole description as JSON-LD to a
    plain GET, which closes it the same cheap way Reed's detail endpoint did.

    Keyed on the listing URL rather than an extracted id (see fetch_adzuna_details)
    because the ad id alone doesn't say which of Adzuna's country TLDs to ask."""
    return _enrich_pre_gate(engine, db, profile_id, jobs, "adzuna",
                            lambda url: url or None, engine.fetch_adzuna_details)


def _persist_enrich_dead(db: Session, profile_id: int, by_key: dict[str, list[dict]],
                         dead_keys: set[str], board: str) -> int:
    """Mark JobSeen rows whose detail fetch returned 404/410 as dead.

    Same column and semantics as the Phase 5 scrape path (_persist_dead_scrapes):
    a fact about the URL, independent of profile/verdict, and every subsequent row
    selection filters on `dead_reason IS NULL`. Runs on the calling thread, which
    is where _enrich_pre_gate already commits."""
    if not dead_keys:
        return 0
    identities = {
        j["_identity"] for key in dead_keys for j in by_key.get(key, []) if j.get("_identity")
    }
    if not identities:
        return 0
    rows = db.execute(
        select(JobSeen).where(
            JobSeen.profile_id == profile_id,
            JobSeen.identity_hash.in_(list(identities)),
            JobSeen.dead_reason.is_(None),
        )
    ).scalars().all()
    for r in rows:
        r.dead_reason = "status_404_detail"
    if rows:
        db.commit()
        _safe_print(f"[{board}] {len(rows)} listing(s) returned 404/410 from the detail "
                    f"endpoint -- marked dead, excluded from every future run")
    return len(rows)


def _enrich_pre_gate(engine, db: Session, profile_id: int, jobs: list[dict], board: str,
                     key_of, fetch) -> int:
    """Shared body of the per-source pre-gate enrichers above: group the candidates
    of one board by whatever key its detail fetcher is keyed on, fetch, then write
    the winners back to both the in-memory dicts and JobSeen.

    Factored out rather than duplicated because the write-back half is where the
    subtle rules live -- only text that BEATS the snippet is stored (so a degenerate
    detail response can't overwrite a better teaser), `_has_full_text` must be set or
    the gate cache-key richness marker goes stale, and the DB write is one indexed
    SELECT + commit like _persist_scrape. A second copy of that would drift."""
    by_key: dict[str, list[dict]] = defaultdict(list)
    for j in jobs:
        if j.get("_has_full_text") or canonical_key(j.get("board")) != board:
            continue
        key = key_of(j.get("url") or "")
        if key:
            by_key[key].append(j)
    if not by_key:
        return 0

    # A 404/410 from the source's OWN per-job endpoint is a high-confidence "this
    # listing is gone" -- the same bar _dead_listing_signal applies to a scrape,
    # but reached for free on a call already being made. Worth wiring up because
    # dead_reason had never once fired on a 6,569-row store: Phase 5 is the only
    # other place that can set it and it only ever reaches ~4% of rows.
    dead_keys: set[str] = set()
    texts = fetch(list(by_key), dead_keys)
    _persist_enrich_dead(db, profile_id, by_key, dead_keys, board)
    if not texts:
        return 0

    by_identity: dict[str, str] = {}
    enriched = 0
    for key, text in texts.items():
        for j in by_key.get(key, []):
            if len(text) <= len(j.get("snippet") or ""):
                continue
            j["full_text"] = text
            j["_has_full_text"] = True
            enriched += 1
            if j.get("_identity"):
                by_identity[j["_identity"]] = text[:8000]
    if by_identity:
        rows = db.execute(
            select(JobSeen).where(
                JobSeen.profile_id == profile_id, JobSeen.identity_hash.in_(list(by_identity))
            )
        ).scalars().all()
        for r in rows:
            r.full_text = by_identity.get(r.identity_hash)
        db.commit()
    return enriched


def _persist_dead_scrapes(db: Session, profile_id: int, jobs: list[dict]) -> None:
    """Persist a confirmed-dead listing's reason (see full_auto.py's
    _dead_listing_signal) so _run_engine_pipeline's rows-assembly filter can
    exclude it on every future run without re-scraping or re-judging it --
    dead-ness is a fact about the URL, independent of profile/CV changes.

    Also auto-hides any already-shown, still-unreviewed Role for the same
    listing (matched via Role.external_id == JobSeen.identity_hash, see
    where Role rows get created). This is the only point where a
    previously-surfaced role ever gets checked against reality post-hoc --
    Phase 5 only ever scrapes a given job once, so without this an expired
    listing the user hasn't acted on would sit in the inbox forever. Moved to
    "ignored" rather than "deleted": reversible via the Ignored tab's
    re-save, in case the dead-detection was a false positive. saved/applied/
    crossed roles are left untouched -- the user has already acted on those."""
    by_id = {j["_identity"]: j["_dead_reason"] for j in jobs
             if j.get("_identity") and j.get("_dead_reason")}
    if not by_id:
        return
    rows = db.execute(
        select(JobSeen).where(
            JobSeen.profile_id == profile_id, JobSeen.identity_hash.in_(list(by_id))
        )
    ).scalars().all()
    for r in rows:
        r.dead_reason = by_id.get(r.identity_hash)
    db.query(Role).filter(
        Role.profile_id == profile_id,
        Role.status == "new",
        Role.external_id.in_(list(by_id)),
    ).update({Role.status: "ignored"}, synchronize_session=False)
    db.commit()


def _persist_verdicts(db: Session, profile_id: int, judged: list[dict],
                      strong: list[dict], backup: list[dict], excluded: list[dict],
                      eval_sig: str) -> None:
    """Store the final-AI verdict per freshly-judged job so an unchanged profile never
    re-pays the expensive model for it. Jobs the AI omitted are recorded as 'reject',
    and `excluded` carries the AI's own reason for each of them in eval_analysis --
    both the hard DISQUALIFIERS hits (`_disqualifier` True) and the ones that merely
    lost out to better picks (False). Only the former used to get a reason: a
    ground-truth audit found 15 of 24 rejects in the judge pool recording nothing at
    all, so there was no way to tell a job the judge deliberately passed over from one
    the cheaper tiers had misread on its way in. A reject with no matching entry (the
    model failed to account for a job_number) still falls through to the blank
    analysis, so the gap shows up as funnel_counts["final_reject_reasoned"] falling
    short rather than as a silent return to the old behaviour."""
    if not judged:
        return
    strong_ids = {s.get("_identity") for s in strong if s.get("_identity")}
    backup_by = {b.get("_identity"): b for b in backup if b.get("_identity")}
    excluded_by = {d.get("_identity"): d.get("reason", "") for d in excluded if d.get("_identity")}
    now = datetime.utcnow()
    verdicts: dict[str, tuple[str, str]] = {}
    for j in judged:
        ident = j.get("_identity")
        if not ident:
            continue
        if ident in strong_ids:
            verdict, src = "strong", next(s for s in strong if s.get("_identity") == ident)
        elif ident in backup_by:
            verdict, src = "backup", backup_by[ident]
        elif ident in excluded_by:
            verdict, src = "reject", {"concerns": [excluded_by[ident]]} if excluded_by[ident] else {}
        else:
            verdict, src = "reject", j
        analysis = json.dumps({
            "summary": src.get("summary", ""),
            "role_type": src.get("role_type", ""),
            # The judge's explicit reasoning split (see full_auto's
            # _FINAL_EVAL_REASONING): can-do-fit is judged separately from
            # want-fit. filters_on/highlight are reasoning step E's application
            # guidance, which replaced the old top_match_reason narrative in
            # FINAL_EVAL_PROMPT_VERSION 23. Persisted alongside the rest so a
            # cache-served verdict renders identically to a freshly-judged one.
            "can_do_fit": src.get("can_do_fit", ""),
            "filters_on": src.get("filters_on") or [],
            "highlight": src.get("highlight", ""),
            "requirements": src.get("requirements") or [],
            "concerns": src.get("concerns", []),
            # The judge's finer verdict grade and the facts it read off the JD,
            # for the result card. Persisted here for the same reason as the
            # reasoning split above: these are merged straight back onto a
            # cache-served pick, so a reused verdict must render identically to a
            # freshly-judged one.
            "fit_level": src.get("fit_level") or "",
            "role_salary": src.get("role_salary") or "",
            "work_style": src.get("work_style") or "",
            "role_seniority": src.get("role_seniority") or "",
            "deadline": src.get("deadline") or "",
            "scam_suspect": bool(src.get("scam_suspect", False)),
            # Ranking-only signal (see full_auto's reasoning step G) -- never gates
            # a verdict, just persisted so a cache-served pick renders identically.
            "sector_match": bool(src.get("sector_match", True)),
        })
        verdicts[ident] = (verdict, analysis)
    rows = db.execute(
        select(JobSeen).where(
            JobSeen.profile_id == profile_id, JobSeen.identity_hash.in_(list(verdicts))
        )
    ).scalars().all()
    for r in rows:
        verdict, analysis = verdicts[r.identity_hash]
        r.eval_verdict = verdict
        r.eval_analysis = analysis
        r.eval_signature = eval_sig
        r.evaluated_at = now
    db.commit()


def _persist_scam_override(db: Session, profile_id: int, identity: str, reason: str, eval_sig: str) -> None:
    """Overrides an already-persisted strong/backup verdict to a reject, after
    verify_not_duplicated corroborates a judge-flagged scam_suspect pick with
    real cross-site evidence post-hoc. Never resurfaces under this eval_sig,
    same as any other reject.

    Preserves the judge's original eval_analysis (summary/concerns/highlight/
    etc.) rather than replacing it outright -- an earlier version overwrote the
    whole blob with just the corroboration reason, which silently destroyed the
    judge's original "why this was strong" reasoning for every overridden pick,
    making it impossible to later audit whether the override itself was
    reasonable (see the investigation that found several overrides were likely
    false positives off a generic job-aggregator mirror site)."""
    row = db.execute(
        select(JobSeen).where(JobSeen.profile_id == profile_id, JobSeen.identity_hash == identity)
    ).scalar_one_or_none()
    if row is None:
        return
    try:
        analysis = json.loads(row.eval_analysis or "{}")
        if not isinstance(analysis, dict):
            analysis = {}
    except (TypeError, ValueError):
        analysis = {}
    analysis["concerns"] = [reason] + [c for c in (analysis.get("concerns") or []) if c != reason]
    analysis["scam_verified_override"] = True
    row.eval_verdict = "reject"
    row.eval_analysis = json.dumps(analysis)
    row.eval_signature = eval_sig
    row.evaluated_at = datetime.utcnow()
    db.commit()


def _is_first_run(db: Session, profile_id: int) -> bool:
    return db.query(JobSeen.id).filter(JobSeen.profile_id == profile_id).first() is None


def _store_counts(db: Session, profile_id: int) -> dict[str, int]:
    rows = db.execute(
        select(JobSeen.state, JobSeen.id).where(JobSeen.profile_id == profile_id)
    ).all()
    out = {"new": 0, "enriched": 0, "shown": 0}
    for state, _id in rows:
        out[state] = out.get(state, 0) + 1
    return out


def _board_breakdown(jobs: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for j in jobs:
        board = (j.get("board") or "?").split(":")[0]
        counts[board] = counts.get(board, 0) + 1
    return counts


def _company_title_counts(jobs: list[dict]) -> dict[str, set[str]]:
    """company (lowercased) -> distinct job titles seen in this run's filtered
    discovery. A structural proxy for "this source looks like a templated
    catalogue of interchangeable roles" (a known lead-gen/CV-harvesting
    shape) without needing to visit the source site's own listing page --
    Adzuna/Reed/Google Jobs already surface several of a prolific poster's
    listings within one run's results when search terms overlap. Purely
    additive data fed to the final judge (see TEMPLATE_FACTORY_TITLE_THRESHOLD
    / _posting_volume_hint) -- never a hard filter by itself."""
    out: dict[str, set[str]] = defaultdict(set)
    for j in jobs:
        company = (j.get("company") or "").strip().lower()
        title = (j.get("title") or "").strip()
        if company and title:
            out[company].add(title)
    return out


# ── Adaptive enrichment funnel ───────────────────────────────────────────────

def _embed_text(r: JobSeen) -> str:
    """The exact string embedded for a job. Purely job content (title/company/
    snippet) -- no profile data -- which is what makes the resulting vector
    reusable across every profile via the JobEmbedding cache."""
    return f"{r.title} {r.company or ''} {(r.snippet or '')[:2000]}"


def _embed_text_hash(engine, text: str) -> str:
    """Content-address key for JobEmbedding. Folds the embedding model name in
    so switching models transparently recomputes under fresh keys rather than
    serving a stale vector from a different model."""
    return hashlib.sha1(f"{engine.EMBED_MODEL}\n{text}".encode()).hexdigest()


def _embed_chunk_safe(engine, texts: list[str]) -> list[list[float]] | None:
    """One embedding chunk, tolerant of a rate limit. A 429 on this org's shared
    TPM budget is routinely transient (the error itself reports a sub-2s retry
    window), so one retry after a short fixed backoff recovers most of them
    without needing to parse the API's suggested wait out of the error text.
    Any other/repeated failure gives up on just this chunk -- returns None
    rather than raising, so the caller can keep every other chunk's results
    instead of losing the whole batch (see _ensure_embeddings)."""
    for attempt in range(2):
        try:
            return engine.get_embeddings_batch(texts)
        except Exception as e:
            if attempt == 0:
                time.sleep(5)
                continue
            engine.emit(f"[pipeline] embedding chunk failed ({len(texts)} texts), "
                        f"skipping for this run: {e}")
            return None


def _ensure_embeddings(engine, db: Session, rows: list[JobSeen]) -> tuple[int, int]:
    """Assign an embedding to any row missing one, computing via OpenAI only for
    text not already in the global JobEmbedding cache. Each distinct job TEXT is
    embedded exactly once ever, across all profiles -- see the JobEmbedding
    model. Returns (reused, computed) for logging."""
    missing = [r for r in rows if not r.embedding]
    if not missing:
        return 0, 0
    texts = [_embed_text(r) for r in missing]
    hashes = [_embed_text_hash(engine, t) for t in texts]

    # 1. Pull whatever the shared cache already has for this batch's hashes.
    want = set(hashes)
    cache: dict[str, str] = {
        row.text_hash: row.embedding
        for row in db.query(JobEmbedding).filter(JobEmbedding.text_hash.in_(want))
    }

    # 2. Which hashes still need an API call -- unique only, so two rows with the
    # same text (e.g. aggregator reposts) cost one call, not two.
    to_compute: dict[str, str] = {}   # hash -> text
    for h, t in zip(hashes, texts):
        if h not in cache and h not in to_compute:
            to_compute[h] = t

    computed: dict[str, str] = {}     # hash -> base64 embedding
    if to_compute:
        c_hashes = list(to_compute)
        c_texts = [to_compute[h] for h in c_hashes]
        chunk_size = _embed_chunk_size(len(c_hashes))
        hash_chunks = [c_hashes[i:i+chunk_size] for i in range(0, len(c_hashes), chunk_size)]
        chunks = [c_texts[i:i+chunk_size] for i in range(0, len(c_texts), chunk_size)]
        # Each chunk's result is applied independently (rather than the previous
        # all-or-nothing ex.map, where one chunk raising -- e.g. a 429 -- lost every
        # OTHER chunk's already-successful vectors too, including chunks that ran
        # before it). A run that hit the org TPM cap used to re-submit its ENTIRE
        # missing-embedding backlog on every subsequent run (nothing from that run
        # ever got persisted), compounding: more unembedded rows -> a bigger burst
        # next time -> more likely to hit the cap again. Now a failed/rate-limited
        # chunk just leaves its rows unembedded for this run (scored 0.0 and sunk to
        # the bottom by _score_rows, never a crash) while every OTHER chunk's result
        # is still kept and committed, so the backlog shrinks run over run instead of
        # regenerating itself.
        vector_chunks: list[list[list[float]] | None] = [None] * len(chunks)
        if len(chunks) == 1:
            vector_chunks[0] = _embed_chunk_safe(engine, chunks[0])
        else:
            with ThreadPoolExecutor(max_workers=min(EMBED_MAX_WORKERS, len(chunks))) as ex:
                futures = {ex.submit(_embed_chunk_safe, engine, c): i for i, c in enumerate(chunks)}
                for fut in as_completed(futures):
                    vector_chunks[futures[fut]] = fut.result()
        for hashes_slice, vectors in zip(hash_chunks, vector_chunks):
            if vectors is None:
                continue
            for h, v in zip(hashes_slice, vectors):
                computed[h] = _encode_embedding(v)
        # Persist the fresh vectors into the shared cache. Guard the PK against a
        # concurrent search thread having inserted the same hash meanwhile: add
        # each in its own nested transaction so one collision doesn't poison the
        # batch, and fall back to the just-committed value on conflict.
        for h, enc in computed.items():
            try:
                with db.begin_nested():
                    db.add(JobEmbedding(text_hash=h, embedding=enc, model=engine.EMBED_MODEL))
            except IntegrityError:
                existing = db.get(JobEmbedding, h)
                if existing is not None:
                    computed[h] = existing.embedding

    # 3. Assign every missing row its vector (cache hit or freshly computed).
    resolved = {**cache, **computed}
    for r, h in zip(missing, hashes):
        enc = resolved.get(h)
        if enc is not None:
            r.embedding = enc
    db.commit()
    return len(missing) - len(to_compute), len(computed)


def _encode_embedding(vec) -> str:
    """Compact on-disk encoding for an embedding vector: base64 of raw float32
    bytes rather than a JSON list. Measured on this store's real 1536-dim
    OpenAI vectors: ~31,000 JSON chars vs ~8,200 base64 chars per row (~74%
    smaller), and decoding is a zero-copy np.frombuffer reinterpret instead of
    character-by-character JSON number parsing -- the JSON text was the
    dominant cost of the cosine-scoring stage, which re-reads every candidate
    row's embedding on every run. float32 (~7 significant digits) is far more
    precision than this pipeline's ~0.35-0.45 relevance cutoffs use."""
    return base64.b64encode(np.asarray(vec, dtype=np.float32).tobytes()).decode("ascii")


def _decode_embedding(raw: str | None) -> np.ndarray | None:
    """Inverse of _encode_embedding, auto-detecting format so rows still
    holding the old JSON-list encoding (anything not yet touched by
    _ensure_embeddings since the format switch, or not yet migrated by
    migrate_embedding_format.py) keep working. A JSON list always starts with
    '['; the new format never does."""
    if not raw:
        return None
    try:
        if raw[0] == "[":
            return np.asarray(json.loads(raw), dtype=np.float32)
        return np.frombuffer(base64.b64decode(raw), dtype=np.float32)
    except (ValueError, TypeError):
        return None


def _score_rows(rows: list[JobSeen], cluster_embeddings: list[list[float]]) -> list[dict]:
    """Cosine each row's cached embedding against EVERY role-cluster embedding
    (free, local) and keep the best. A job is assigned to whichever cluster it
    matches best (_cluster, an index into cluster_embeddings) so pooling/
    gating/final-eval downstream can treat each role interest independently
    instead of judging every job against one blended average of all of them.
    Returns engine-shaped dicts sorted by score desc with embed_score,
    _cluster, and _identity.

    Vectorized: decodes every row's embedding into one N-D matrix and the
    cluster embeddings into one M-D matrix, then scores the whole set with a
    single normalized matrix multiply instead of a Python loop calling
    cosine_similarity() once per (row, cluster) pair -- same cosine formula,
    just batched, so scores are unchanged (within float32 rounding)."""
    decoded = [_decode_embedding(r.embedding) for r in rows]
    valid_idx = [i for i, v in enumerate(decoded) if v is not None and v.size]

    best_idx_arr = [0] * len(rows)
    best_score_arr = [0.0] * len(rows)
    if valid_idx and cluster_embeddings:
        mat = np.stack([decoded[i] for i in valid_idx]).astype(np.float32)
        cmat = np.asarray(cluster_embeddings, dtype=np.float32)
        mat_norm = mat / np.clip(np.linalg.norm(mat, axis=1, keepdims=True), 1e-12, None)
        cmat_norm = cmat / np.clip(np.linalg.norm(cmat, axis=1, keepdims=True), 1e-12, None)
        sims = mat_norm @ cmat_norm.T  # (len(valid_idx), M)
        row_best_idx = sims.argmax(axis=1)
        row_best_score = sims[np.arange(sims.shape[0]), row_best_idx]
        for pos, i in enumerate(valid_idx):
            best_idx_arr[i] = int(row_best_idx[pos])
            best_score_arr[i] = float(row_best_score[pos])

    scored = sorted(
        zip(best_score_arr, best_idx_arr, rows), key=lambda t: t[0], reverse=True
    )
    out = []
    for score, cluster_idx, r in scored:
        d = _rows_to_dicts([r])[0]
        d["embed_score"] = score
        d["_cluster"] = cluster_idx
        out.append(d)
    return out


def _fair_allocate(by_group: dict[int, list[dict]], total: int) -> list[dict]:
    """Split `total` slots across groups (role clusters) with an equal floor
    share, then roll slots a group didn't need over to groups with more
    candidates than their share -- so a populous group can never crowd a
    sparse-but-real one out of a fixed-size cap. Each group's list must
    already be sorted best-first. Used for pool size, top-N selection, and
    final-picks, so a candidate's several distinct role interests each get a
    fair shot instead of the pipeline judging everything against one blend."""
    groups = [items for items in by_group.values() if items]
    if not groups:
        return []
    if len(groups) == 1:
        return groups[0][:total]
    # Each group's floor is max(1, ...) so more groups than `total` slots would
    # otherwise sum past the cap in the first pass below -- guard the output
    # size explicitly rather than relying on the arithmetic to stay in bounds.
    share = max(1, total // len(groups))
    taken = [min(share, len(items)) for items in groups]
    out: list[dict] = []
    for items, take in zip(groups, taken):
        out.extend(items[:take])
    remaining = total - sum(taken)
    if remaining > 0:
        for i, items in enumerate(groups):
            if remaining <= 0:
                break
            available = len(items) - taken[i]
            if available <= 0:
                continue
            extra = min(available, remaining)
            out.extend(items[taken[i]:taken[i] + extra])
            remaining -= extra
    return out[:total]


def _clusters_without_fresh_terms(eng_profile: dict, role_clusters: list[dict]) -> set[int]:
    """Cluster indices that got none of their own terms in this run's
    TERMS_PER_RUN rotation window (eng_profile['search_terms_batch'], set by
    full_auto.select_sources_for_run as a side effect of gather_jobs). A
    single-cluster profile is never skipped, and this never skips every
    cluster at once (a rotation window that only reaches N-1 clusters still
    leaves at least one live)."""
    if len(role_clusters) <= 1:
        return set()
    batch = {t.strip().lower() for t in (eng_profile.get("search_terms_batch") or []) if t}
    if not batch:
        return set()
    skipped = {
        idx for idx, cluster in enumerate(role_clusters)
        if (roles := {r.strip().lower() for r in (cluster.get("roles") or [])}) and not (roles & batch)
    }
    return skipped if len(skipped) < len(role_clusters) else set()


def _cluster_label(cluster: dict) -> str:
    """The family name the candidate gave this stream, falling back to its first
    role for a cluster built without a family (see snapshot.build_snapshot's
    ungrouped-roles fallback)."""
    return cluster.get("label") or (cluster.get("roles") or ["General"])[0]


def _cluster_candidate_queues(scored: list[dict]) -> tuple[dict[int, list[dict]], bool, dict[int, str]]:
    """Groups already-sorted-desc `scored` candidates by cluster into the full
    ordered queue _gate_rank_refill_cluster pulls batches from -- unlike the
    old single-shot TARGET_POOL-capped pool, nothing is truncated here; each
    cluster's own refill loop decides how much of its queue to actually
    examine (up to its fair-share examine_cap). Returns (queues, harsh, fallback_reasons):
    `harsh` is True if ANY cluster has fewer than MIN_RESULTS candidates
    clearing RELEVANCE_PRIMARY (relying on floor-broadened matches or worse);
    fallback_reasons maps cluster index -> a short tag, only for clusters that
    needed broadening/fallback."""
    by_cluster: dict[int, list[dict]] = defaultdict(list)
    for j in scored:                      # `scored` is already sorted desc
        by_cluster[j.get("_cluster", 0)].append(j)

    queues: dict[int, list[dict]] = {}
    harsh = False
    fallback_reasons: dict[int, str] = {}
    for key, items in by_cluster.items():
        strong = [j for j in items if j["embed_score"] >= RELEVANCE_PRIMARY]
        broadened = [j for j in items if j["embed_score"] >= RELEVANCE_FLOOR]
        if len(strong) < MIN_RESULTS:
            harsh = True
        if broadened:
            if len(strong) < MIN_RESULTS:
                fallback_reasons[key] = "broadened"
            queues[key] = broadened
            continue
        # Nothing clears even the floor. Still surface the best few UNLESS the
        # top score is exactly 0.0 -- that means embedding the store's rows
        # failed for this batch (see _ensure_embeddings), not a genuine niche
        # result, so don't misreport it as "filters too strict". Kept small
        # (MIN_RESULTS) and explicit rather than opening the whole cluster to
        # refill -- this is a last resort, not a normal queue.
        if items and items[0]["embed_score"] > 0.0:
            queues[key] = items[:MIN_RESULTS]
            fallback_reasons[key] = "floor_fallback"
        else:
            queues[key] = []
            if items:
                fallback_reasons[key] = "embedding_failure"

    return queues, harsh, fallback_reasons


def _hard_enforced_axes(engine, cluster_profile: dict) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split screen_gate's soft axes into (still-soft, promoted-to-hard) for this
    run, from the enforcement the candidate chose per constraint
    (snapshot._hard_axes -> eng_profile["hard_axes"]).

    A promoted axis behaves exactly like _hard_gate_ok: a clear failure removes
    the listing outright and it is NOT eligible for the MIN_RESULTS floor
    backfill -- backfilling a role the candidate declared non-negotiable-ly wrong
    would defeat the point of the toggle, same reasoning as the avoid/must-have
    chips. It also stops counting toward the soft-failure threshold, since it can
    no longer contribute to a demotion it has already prevented.

    Intersected with SOFT_GATE_AXES rather than trusted verbatim, so a stale or
    malformed axis name in a snapshot can't silently become a filter no axis
    actually feeds."""
    hard = tuple(a for a in engine.SOFT_GATE_AXES if a in set(cluster_profile.get("hard_axes") or []))
    soft = tuple(a for a in engine.SOFT_GATE_AXES if a not in hard)
    return soft, hard


def _make_progress_reporter(progress_q: "queue.Queue", idx: int):
    """Binds a cluster index to the shared progress queue so
    _gate_rank_refill_cluster can report a snapshot of its own judge_eligible
    list without knowing anything about queues or which cluster it is --
    mirrors _make_cancel_check's callable-injection pattern, just flowing the
    opposite direction (worker thread -> main thread instead of main thread ->
    worker thread). Thread-safe: queue.Queue.put is safe to call from any
    thread with no external locking."""
    def report(snapshot: list[dict]) -> None:
        progress_q.put((idx, snapshot))
    return report


def _gate_rank_refill_cluster(
    queue: list[dict], cluster_profile: dict, engine, cancel_check,
    judge_target: int, examine_cap: int, report=None,
) -> tuple[list[dict], dict]:
    """Iteratively gates then ranks batches of one cluster's embed-score-ordered
    candidate queue (already restricted to >= RELEVANCE_FLOOR, or the small
    last-resort floor_fallback list -- see _cluster_candidate_queues) instead
    of a one-shot TARGET_POOL-capped gate call. A harsher gate (2+, or 1+ when
    the round's dynamic threshold tightens -- see
    full_auto.dynamic_hard_drop_threshold -- soft-axis failures hard-drops)
    or the RANK_REJECT_SCORE_FLOOR
    absolute cutoff can leave a cluster short of its target even though hundreds
    of decent-scoring candidates sit unexamined in the store; this keeps pulling
    batches until `judge_target` rank-floor survivors accumulate, the queue is
    exhausted, or `examine_cap` total candidates have been examined. Both are
    the caller's fair share of the run-wide JUDGE_POOL/examine budget (see the
    call site) -- NOT the flat JUDGE_POOL module constant,
    so a multi-cluster run doesn't have every cluster independently grind
    toward the full JUDGE_POOL target each.

    Returns (judge_eligible_sorted, gate_survivors, stats). judge_eligible is a
    strict subset of gate_survivors (excludes rank-floor rejects, unless
    promoted by the rank-side floor backfill) -- callers that need "everything
    that passed the gate, regardless of rank outcome" (e.g. deciding which
    examined rows should stay 'new' to resurface vs. be marked enriched) want
    gate_survivors, not judge_eligible. stats carries the funnel numbers for
    this cluster's log line (examined/queue_len/gate_survivors/judge_eligible/
    stop_reason), plus "below_rank_floor_jobs" -- the actual rejected candidate
    dicts (not just a count) for the caller's Snapshot-panel sample; `queue[:
    stats['examined']]` recovers exactly the subset of the queue this call
    looked at.

    Runs in a worker thread (one per cluster -- see the call site), so it takes a
    `cancel_check` callable rather than the pipeline's own (thread-unsafe)
    Session/SearchRun pair. Makes no DB writes and touches no shared state; every
    result is merged by the caller, back on the main thread. `report`, if given,
    is called with a full snapshot of `judge_eligible` (a thread-safe one-way
    channel -- see _make_progress_reporter) every time that list grows, so the
    caller can persist provisional rows for early display well before this
    whole cluster's queue is exhausted, instead of only once this function
    returns."""
    report = report or (lambda _: None)
    soft_axes, hard_axes = _hard_enforced_axes(engine, cluster_profile)
    pos = 0
    examined = 0
    gate_survivors: list[dict] = []    # in-sector, below this round's hard-drop threshold (all rounds)
    hard_dropped: list[dict] = []      # in-sector, at/above this round's hard-drop threshold
    off_sector: list[dict] = []        # sector_ok=False
    hard_gate_failed: list[dict] = []  # _hard_gate_ok=False, or a candidate-promoted hard axis failed
    judge_eligible: list[dict] = []    # ranked, >= RANK_REJECT_SCORE_FLOOR
    below_rank_floor: list[dict] = []  # ranked, < RANK_REJECT_SCORE_FLOOR
    stop_reason = "pool_exhausted"

    while pos < len(queue):
        if len(judge_eligible) >= judge_target:
            stop_reason = "target_reached"
            break
        if examined >= examine_cap:
            stop_reason = "absolute_pool_cap"
            break
        cancel_check()
        # Small first round -> earliest possible provisional cards (report() below
        # fires per round), larger rounds after to bound orchestration overhead.
        # dynamic_hard_drop_threshold (below) is now computed per smaller round --
        # more responsive, slightly noisier -- which is fine: it already defaulted
        # to "unsure -> keep" on any axis it isn't confident about.
        round_cap = GATE_FIRST_ROUND if examined == 0 else GATE_ROUND_SIZE
        batch_size = min(len(queue) - pos, round_cap, TARGET_POOL, examine_cap - examined)
        batch = queue[pos:pos + batch_size]
        pos += batch_size
        examined += len(batch)

        # Deterministic, LLM-free hard filter: a listing whose DEFINITE (non-
        # approximate) posted date is confirmed older than the candidate's own
        # "Maximum listing age" preference, when enforced Hard (the default) --
        # see snapshot.build_snapshot's engine_profile["max_listing_age_days"/
        # "_hard"]. Dropped the same way as the candidate's avoid/must-have
        # chips: before any LLM call is even made, since the fact is already
        # known, and excluded from the floor backfill below the same way
        # hard_gate_failed always has been (backfilling a listing this old
        # would defeat the point of the preference). Anything that slips past
        # this (an unknown or merely-approximate date, or the preference left
        # Soft) still reaches screen_gate/rank_gate/the judge, which apply the
        # softer downgrade-only treatment instead -- see full_auto.py's
        # listing_over_max_age/_listing_age_tag.
        max_age_days = cluster_profile.get("max_listing_age_days")
        max_age_hard = cluster_profile.get("max_listing_age_hard", True)
        if max_age_hard and max_age_days:
            too_old, kept = [], []
            for j in batch:
                (too_old if engine.listing_over_max_age(j, max_age_days) else kept).append(j)
            if too_old:
                hard_gate_failed.extend(too_old)
                batch = kept

        annotated = engine.screen_gate(batch, cluster_profile)
        # The candidate's OWN hard filters drop unconditionally, like off-sector.
        # Unlike a soft-axis or sector drop, these are removed from the round
        # entirely and are NOT eligible for the floor backfill below --
        # resurfacing a job the candidate explicitly said to avoid (or that
        # plainly can't meet a stated must-have) would defeat the point. That
        # covers both the avoid/must-have chips (_hard_gate_ok) and any normally-
        # soft axis the candidate marked Hard (hard_axes, see _hard_enforced_axes).
        # _listing_ok (the text clearly isn't one specific job posting -- a
        # board's own search-results/category page or generic aggregator blurb
        # that slipped past discovery-time filtering) drops the same way: there is
        # no real listing underneath to backfill toward, so re-surfacing it via
        # the floor backfill would just show the candidate the same non-job text
        # again.
        def _clears_hard(j: dict) -> bool:
            return (j.get("_hard_gate_ok", True) and j.get("_listing_ok", True)
                    and all(j.get(a, True) for a in hard_axes))

        def _clears_rank_floor(j: dict) -> bool:
            # rank_gate had no real signal for this job (same-model retry and
            # cheap-tier fallback both failed, see full_auto.rank_gate) -- pass it
            # through instead of comparing a fabricated neutral score against
            # RANK_REJECT_SCORE_FLOOR, which would silently guarantee rejection.
            # Still bounded by the judge_target cap in the while loop above.
            return j.get("_rank_gate_failed", False) or j.get("_rank_score", 50.0) >= RANK_REJECT_SCORE_FLOOR

        hard_gate_failed.extend(j for j in annotated if not _clears_hard(j))
        annotated = [j for j in annotated if _clears_hard(j)]
        in_sector = [j for j in annotated if j.get("_sector_ok", True)]
        off_sector.extend(j for j in annotated if not j.get("_sector_ok", True))

        # Dynamic strictness (engine.dynamic_hard_drop_threshold, shared with
        # screen_gate's own diagnostic log so the two can't disagree): drops
        # to a 1-failure hard-drop threshold when most of this round is
        # sailing through every soft axis clean, since that's a sign the
        # round is thin on real mismatches, not that everyone genuinely fits.
        # Only the still-soft axes count here: an axis the candidate promoted to
        # Hard has already removed its failures above, so leaving it in the count
        # would just be summing a column of zeroes.
        soft_fail_counts = [
            sum(1 for axis in soft_axes if not j.get(axis, True))
            for j in in_sector
        ]
        hard_drop_threshold = engine.dynamic_hard_drop_threshold(soft_fail_counts)

        round_survivors = []
        for j, fails in zip(in_sector, soft_fail_counts):
            if fails >= hard_drop_threshold:
                hard_dropped.append(j)
            else:
                gate_survivors.append(j)
                round_survivors.append(j)

        if round_survivors:
            ranked = engine.rank_gate(round_survivors, cluster_profile)
            for j in ranked:
                if _clears_rank_floor(j):
                    judge_eligible.append(j)
                else:
                    below_rank_floor.append(j)
            if judge_eligible:
                report(list(judge_eligible))

    # Gate-side floor backfill: never let a cluster reach rank with fewer than
    # MIN_RESULTS in-sector candidates, mirroring the pre-refill design (a
    # purely hard-drop gate starved clusters once before -- see CLAUDE.md).
    backfilled = False
    if len(gate_survivors) < MIN_RESULTS:
        need = MIN_RESULTS - len(gate_survivors)
        topup = sorted(hard_dropped + off_sector, key=lambda x: x.get("embed_score", 0),
                        reverse=True)[:need]
        if topup:
            backfilled = True
            for j in engine.rank_gate(topup, cluster_profile):
                gate_survivors.append(j)
                if _clears_rank_floor(j):
                    judge_eligible.append(j)
                else:
                    below_rank_floor.append(j)
            if judge_eligible:
                report(list(judge_eligible))

    rank_floor_rejected = len(below_rank_floor)

    # Rank-side floor backfill: never send the judge fewer than MIN_RESULTS
    # for a cluster that has any gate survivors at all -- an absolute score
    # cutoff (unlike the old relative bottom-20% trim) can in principle reject
    # every candidate in a genuinely weak cluster.
    floor_target = min(MIN_RESULTS, len(gate_survivors))
    if len(judge_eligible) < floor_target:
        need = floor_target - len(judge_eligible)
        promoted = sorted(below_rank_floor, key=_selection_score, reverse=True)[:need]
        if promoted:
            backfilled = True
        judge_eligible.extend(promoted)
        below_rank_floor = [j for j in below_rank_floor if j not in promoted]
        rank_floor_rejected -= len(promoted)
        if promoted:
            report(list(judge_eligible))

    judge_eligible.sort(key=_selection_score, reverse=True)
    stats = {
        "examined": examined, "queue_len": len(queue),
        "gate_survivors": len(gate_survivors), "hard_dropped": len(hard_dropped),
        # The actual rank-floor-rejected candidate dicts (carrying rank_gate's
        # _rank_score/_rank_note), not just the count above -- lets the caller
        # sample real borderline drops into the Snapshot panel instead of them
        # just vanishing with only a number left behind.
        "below_rank_floor_jobs": below_rank_floor,
        "off_sector": len(off_sector), "rank_floor_rejected": rank_floor_rejected,
        "hard_gate_dropped": len(hard_gate_failed),
        "judge_eligible": len(judge_eligible), "stop_reason": stop_reason,
        "backfilled": backfilled,
    }
    return judge_eligible, gate_survivors, stats


_TRAINING_COMPANY_CONTAGION = 2  # >= this many flagged listings => whole company is a farm


def _filter_training(engine, jobs: list[dict]) -> tuple[list[dict], int]:
    """Hard-drop paid 'training'/placement schemes masquerading as vacancies
    (see full_auto.looks_like_training_scheme). Two passes: pass 1 phrase-flags
    each listing; a company with >= _TRAINING_COMPANY_CONTAGION flagged listings
    is treated as a training provider, so pass 2 also drops its *un*flagged
    listings (training farms like ITOL Recruit post some ads that individually
    lack the tell-tale phrasing). The >=2 threshold means one false-strong can't
    nuke a genuine employer. Returns (kept, dropped_count)."""
    def _text(j: dict) -> str:
        return j.get("snippet", "") or j.get("full_text", "") or ""

    def _flagged(j: dict) -> bool:
        return engine.looks_like_training_scheme(j.get("title", ""), j.get("company", ""), _text(j))

    flags = [_flagged(j) for j in jobs]
    by_company: dict[str, int] = {}
    for j, f in zip(jobs, flags):
        if f:
            company = (j.get("company") or "").strip().lower()
            if company:
                by_company[company] = by_company.get(company, 0) + 1
    farm_companies = {c for c, n in by_company.items() if n >= _TRAINING_COMPANY_CONTAGION}

    kept, dropped = [], 0
    for j, f in zip(jobs, flags):
        company = (j.get("company") or "").strip().lower()
        if f or (company and company in farm_companies):
            dropped += 1
            continue
        kept.append(j)
    return kept, dropped


# Location-field values that carry no place information at all -- just a
# work-arrangement descriptor. Distinct from a genuine-but-unrecognised place
# name (e.g. "Riga, Latvia"): those must NOT fall back to the snippet (see
# _filter_by_country's docstring), but these carry no location claim to trust
# or distrust in the first place, so they're safe to treat like a blank field.
_LOCATION_SCOPE_DESCRIPTORS = {
    "hybrid", "remote", "distributed", "in-office", "in office", "onsite",
    "on-site", "on site", "flexible", "wfh", "work from home",
}


def _is_location_scope_descriptor(location: str) -> bool:
    return (location or "").strip().lower() in _LOCATION_SCOPE_DESCRIPTORS


# Boards that are inherently scoped to the profile's own country at query time,
# so a listing they return is guaranteed in-country regardless of whether our
# token set can recognise the town it names: Reed is UK-only, and Adzuna is only
# ever queried on the profile's own country endpoint (fetch_adzuna returns [] for
# an unsupported country). Aggregators (careerjet), organic (google_jobs), broad
# APIs (jsearch/remotive) and company ATS boards can all carry cross-border rows,
# so they still get the location check below.
_COUNTRY_SCOPED_BOARDS = {"reed", "adzuna", "usajobs"}


def _filter_by_country(engine, jobs: list[dict], country_codes: list[str]) -> list[dict]:
    """Keep a job UNLESS it positively resolves to a country OTHER than an allowed
    one. Two escape hatches stop this starving a country whose town names the
    worldwide token set can't recognise:

      * Country-scoped boards (_COUNTRY_SCOPED_BOARDS) are kept unconditionally --
        they're guaranteed in-country by construction, so a town the gb token set
        can't name must not drop a Reed/Adzuna row.
      * A location that doesn't positively resolve to ANY country (an unrecognised
        town, a bare work-arrangement descriptor, or blank) is KEPT; only a location
        that resolves to a *different*, non-allowed country is dropped.

    This deliberately loosens the old positively-must-match-or-drop posture, which
    was hard-dropping ~97% of genuinely-UK listings (most UK postings name only a
    town the ~20-city gb token set doesn't carry -> country_of returned None -> the
    row was dropped). The cheap gate's work-arrangement axis and the final judge's
    LOCATION disqualifier remain the real location enforcers for anything kept here.

    The positive check uses engine.country_matches (which tests the allowed codes
    directly) rather than engine.country_of, so an ambiguous city that IS a valid
    allowed-country place -- e.g. "Newcastle", which country_of misroutes to `au`
    because Australia sorts first -- is still recognised as in-country. The blank/
    descriptor -> snippet fallback is preserved. country_codes == [] means Global
    (International scope) -- no filtering."""
    if not country_codes:
        return jobs
    allowed = set(country_codes)
    kept: list[dict] = []
    trusted = matched = unknown = foreign = 0
    for j in jobs:
        board = (j.get("board") or "").split(":")[0].strip().lower()
        if board in _COUNTRY_SCOPED_BOARDS:
            kept.append(j)
            trusted += 1
            continue
        location = j.get("location", "") or ""
        text = location
        if not location.strip() or _is_location_scope_descriptor(location):
            text = j.get("snippet", "") or ""
        if engine.country_matches(text, allowed):
            kept.append(j)
            matched += 1
            continue
        cc = engine.country_of(text)
        if cc is None or cc in allowed:
            kept.append(j)          # unrecognised location -> keep (see docstring)
            unknown += 1
        else:
            foreign += 1            # positively a different country -> drop
    engine.emit(
        f"[pipeline] country filter {sorted(allowed)} kept {len(kept)}/{len(jobs)} "
        f"(scoped-source {trusted}, matched {matched}, unknown-kept {unknown}; "
        f"dropped {foreign} confirmed-foreign)")
    return kept


def _filter_by_local_place(jobs: list[dict], place: str) -> list[dict]:
    """Hard-drop jobs whose location doesn't mention the candidate's city/place.
    Only active when the profile's location_scope is "local" -- a much tighter
    filter than the country check, so it's applied on top of it, not instead of
    it. Unlike _filter_by_country, a blank location is dropped rather than
    falling back to the snippet/description: a substring match against free
    text is far more prone to false positives than the country token match, so
    it needs the stronger signal (an actual location field) to keep a job."""
    if not place:
        return jobs
    needle = place.strip().lower()
    if not needle:
        return jobs
    return [j for j in jobs if needle in (j.get("location", "") or "").lower()]


def _filter_by_salary(jobs: list[dict], salary_floor: int) -> list[dict]:
    """Hard-drop jobs whose stated maximum salary is clearly below the candidate's
    floor. Same posture as the country filter but softer: salary data is sparser
    and unknown salary always passes through (never hard-dropped on missing data).
    Only the *max* is compared, and only when it's a positive number, so a role
    listing a range whose top end is under the floor is dropped while an
    unpriced role survives. Note: magnitudes are compared as-is across
    currencies (GBP/USD/EUR/AUD are close enough for a floor check); this is a
    coarse guard, not a precise salary match. floor <= 0 disables it entirely."""
    if not salary_floor or salary_floor <= 0:
        return jobs
    kept = []
    for j in jobs:
        smax = j.get("salary_max")
        try:
            smax = float(smax) if smax is not None else None
        except (ValueError, TypeError):
            smax = None
        if smax is not None and smax > 0 and smax < salary_floor:
            continue
        kept.append(j)
    return kept


def _log_score_distribution(emit, scored: list[dict]) -> None:
    """Calibration aid (Stage 3): log the embed_score distribution of the WHOLE
    scored set, not just survivors, so RELEVANCE_PRIMARY/FLOOR can eventually be
    set from real data instead of the current fixed guess. Cheap, log-only."""
    if not scored:
        return
    scores = sorted((j.get("embed_score", 0.0) for j in scored), reverse=True)
    n = len(scores)
    def _pct(p: float) -> float:
        return round(scores[min(n - 1, int(p * n))], 3)
    buckets = {"0.4+": 0, "0.35-0.4": 0, "0.3-0.35": 0, "0.2-0.3": 0, "<0.2": 0}
    for s in scores:
        if s >= 0.40:   buckets["0.4+"] += 1
        elif s >= 0.35: buckets["0.35-0.4"] += 1
        elif s >= 0.30: buckets["0.3-0.35"] += 1
        elif s >= 0.20: buckets["0.2-0.3"] += 1
        else:           buckets["<0.2"] += 1
    emit(f"[calibration] score dist n={n} | max={scores[0]:.3f} p25={_pct(0.25)} "
         f"median={_pct(0.5)} p75={_pct(0.75)} min={scores[-1]:.3f} | buckets={buckets}")


class SearchCancelled(Exception):
    """Raised by _check_cancelled when the user has requested cancellation via
    POST /search/cancel. Propagates up through _run_engine_pipeline's
    asyncio.run() call to run_search_task, which catches it distinctly from a
    generic failure -- the cancel endpoint already set status="cancelled" on
    its own session/request, and run_search_task must never overwrite that
    back to "error" or "done".

    Also raised when the PROCESS is shutting down (see _shutdown_requested).
    In that case nothing has set a status yet, and the run is marked by
    request_shutdown_cancel on the way out (or the startup reaper, on a hard
    kill) -- not here."""


# Set by the SIGTERM/SIGINT handler installed in main.py. A signal handler runs
# between arbitrary bytecodes on the main thread, so it must not touch the DB or
# take a lock; assigning a module-level bool is the only safe thing to do there,
# and every cancel checkpoint below reads it for free (no SELECT). This is what
# gets the search to stop *before* the host's kill grace period expires: uvicorn
# waits for background tasks BEFORE firing the lifespan shutdown event, so a
# lifespan hook alone would run only after the search had already finished.
_shutdown_requested = False


def request_process_shutdown() -> None:
    """Signal-handler-safe: flip the flag every cancel checkpoint polls."""
    global _shutdown_requested
    _shutdown_requested = True


def _check_cancelled(db: Session, run: SearchRun) -> None:
    """Cooperative-cancellation checkpoint, called at each major phase
    boundary below. The cancel endpoint commits on its own request-scoped
    session; this session's in-memory `run` won't reflect that commit until
    reloaded, so db.refresh() (a real SELECT) is required here rather than
    trusting the attribute already on the object."""
    if _shutdown_requested:
        raise SearchCancelled()
    db.refresh(run)
    if run.cancel_requested:
        raise SearchCancelled()


def _make_cancel_check(run_id: int, min_interval: float = 3.0):
    """A thread-safe version of _check_cancelled, for the stages that run one
    worker thread per role cluster (the gate+rank refill, and the per-cluster
    scrape+judge). Those threads must not touch the pipeline's own Session --
    SQLAlchemy sessions aren't thread-safe -- so each probe opens and closes its
    own short-lived one instead.

    Throttled to one real SELECT every `min_interval` seconds across ALL callers:
    each cluster calls this per gate batch, so an unthrottled version would turn
    into a steady trickle of concurrent SQLite reads for a flag that changes at
    most once per run. Once a cancel is seen it's latched, so every later call
    raises immediately without another query."""
    lock = threading.Lock()
    state = {"next_check": 0.0, "cancelled": False}

    def check() -> None:
        # Checked before the throttle and without a SELECT: on shutdown the
        # whole point is to stop writing immediately, not up to min_interval
        # seconds later, and the host is already counting down to SIGKILL.
        if _shutdown_requested:
            raise SearchCancelled()
        with lock:
            if state["cancelled"]:
                raise SearchCancelled()
            now = time.monotonic()
            if now < state["next_check"]:
                return
            state["next_check"] = now + min_interval
        probe = SessionLocal()
        try:
            cancelled = bool(probe.execute(
                select(SearchRun.cancel_requested).where(SearchRun.id == run_id)
            ).scalar())
        finally:
            probe.close()
        if cancelled:
            with lock:
                state["cancelled"] = True
            raise SearchCancelled()

    return check


def _progress(db: Session, run: SearchRun, message: str) -> None:
    """Best-effort progress ping shown to the frontend while a phase is
    in-flight. A conditional UPDATE guarded on status still being "running" --
    not before every _progress() call, so without this guard a progress ping
    landing right after a cancel would silently clobber "Search cancelled."
    with a stale phase string like "Reading full job pages...", even though
    the run correctly stops at its next _check_cancelled checkpoint."""
    db.execute(
        update(SearchRun)
        .where(SearchRun.id == run.id, SearchRun.status == "running")
        .values(message=message)
    )
    db.commit()


def _run_cluster_final_eval(
    idx: int, jobs: list[dict], role_clusters: list[dict], cv_text_base: str,
    eng_profile: dict, rank_by_cluster: dict[int, list[dict]], engine,
) -> dict:
    """Per-cluster Phase 6 judging (main call + bounded backfill retry). Pure
    w.r.t. shared state -- makes no DB writes, and touches no shared counter or
    list -- so the caller can run this concurrently across clusters via
    ThreadPoolExecutor. Safe to parallelize here (unlike the gate+rank stage)
    because _fair_allocate has already picked each cluster's `jobs` by the time
    this runs, so there's no cross-cluster fairness decision left to disturb.
    Returns a dict the caller uses, in the main thread, to persist verdicts,
    accumulate funnel counters, extend `to_evaluate`, and run scam-verify."""
    label = _cluster_label(role_clusters[idx])
    cluster_roles = role_clusters[idx].get("roles") or []
    cv_text = cv_text_for_cluster(cv_text_base, cluster_roles) if cluster_roles else cv_text_base
    # Signature includes EXP_MODEL and FINAL_EVAL_PROMPT_VERSION so a judge-model
    # upgrade (e.g. gpt-5.4 -> gpt-5.5) OR a DISQUALIFIERS/schema prompt edit
    # naturally invalidates every previously stored verdict instead
    # of serving a stale reject/strong/backup forever -- same fix as
    # rank_gate's "rank_v2" cache-key bump in full_auto.py, applied here so
    # existing evaluated jobs actually get re-judged by the new model too.
    eval_sig = hashlib.sha1(
        f"{cv_text}|{engine.EXP_MODEL}|{engine.FINAL_EVAL_PROMPT_VERSION}".encode()
    ).hexdigest()[:16]

    # Reuse stored verdicts for jobs already judged under this CV; only send the
    # rest to the expensive model. A prior "reject" under this exact signature
    # is excluded here AND kept out of the deterministic fallback below -- once
    # the expensive AI has judged a job not a fit for the current profile, it
    # must never resurface (the fallback used to pull from the full `jobs`
    # list, which could re-show exactly these rejects as an "inconclusive"
    # placeholder pick -- the bug behind rejected roles reappearing).
    fresh: list[dict] = []
    cached_strong: list[dict] = []
    previously_rejected_ids: set[str] = set()
    for j in jobs:
        if j.get("_eval_signature") == eval_sig and j.get("_eval_verdict"):
            if j["_eval_verdict"] in ("strong", "backup"):
                try:
                    analysis = json.loads(j.get("_eval_analysis") or "{}")
                except (ValueError, TypeError):
                    analysis = {}
                cached_strong.append(dict(j, strong_fit=(j["_eval_verdict"] == "strong"), **analysis))
            else:
                previously_rejected_ids.add(j.get("_identity"))
        else:
            fresh.append(j)

    engine.emit(f"[pipeline] final_evaluation cluster[{idx}] ({label}): {len(fresh)} to judge, "
                f"{len(jobs) - len(fresh)} reused from prior verdict (LLM cap={engine.FINAL_PICKS})")

    strong, backup, disqualified, call_failed = [], [], [], False
    if fresh:
        _cluster_eval_start = time.monotonic()
        strong, backup, disqualified = engine.final_evaluation_split(fresh, eng_profile, cv_text=cv_text)
        _rejected_this_call = len(fresh) - len(strong) - len(backup) if strong is not None else 0
        # `disqualified` now carries BOTH exclusion kinds (see final_evaluation_split):
        # hard DISQUALIFIERS hits and jobs that merely lost out. Report them separately
        # -- the hard count is the diagnostic that says whether the judge is actually
        # rejecting anyone, and folding the out-competed ones in would inflate it.
        _hard = sum(1 for d in (disqualified or []) if d.get("_disqualifier")) if strong is not None else 0
        _reasoned = len(disqualified) if strong is not None else 0
        engine.emit(f"[pipeline] final_evaluation cluster[{idx}] ({label}) took "
                    f"{time.monotonic() - _cluster_eval_start:.1f}s for {len(fresh)} job(s) -> "
                    f"{len(strong) if strong is not None else 0} strong, "
                    f"{len(backup) if strong is not None else 0} backup, {_rejected_this_call} rejected "
                    f"({_hard} on a disqualifier, {_reasoned} of {_rejected_this_call} with a recorded reason)")
        if strong is None:
            # The call itself failed (exception/malformed response) -- nothing
            # was actually judged. Don't persist any verdict, and don't treat
            # this the same as a genuine unanimous rejection below.
            call_failed = True
            strong, backup, disqualified = [], [], []

    # Tiered assembly: strong-tier (fresh this run, then cached) always
    # ranks above backup-tier (cached backup verdicts, then fresh backup),
    # which is only used as filler when a cluster has zero strong picks --
    # previously cached "backup" verdicts were folded unconditionally into
    # the same list as strong picks here, so a stale lenient verdict could
    # ride along as a full peer to a genuine strong match into the
    # embed_score sort below (removed) that decided display order.
    fallback_tags: set[str] = set()
    strong_tier = ([dict(p, strong_fit=True) for p in strong]
                   + [p for p in cached_strong if p.get("strong_fit")])
    backup_tier = ([p for p in cached_strong if not p.get("strong_fit")]
                   + [dict(p, strong_fit=False) for p in backup])

    picks = strong_tier
    if not picks:
        if backup_tier:
            picks = backup_tier
            fallback_tags.add("eval_fallback")
        elif call_failed:
            # Only fall back to an unverified top-N when the AI call itself
            # failed -- never when it succeeded and genuinely rejected
            # everyone, and never resurfacing a job already rejected under
            # this exact profile signature.
            fallback_pool = [j for j in jobs if j.get("_identity") not in previously_rejected_ids]
            picks = [
                dict(j, strong_fit=False, summary="",
                     concerns=["Automated review was inconclusive this run -- showing the "
                               "closest available match unverified."])
                for j in fallback_pool[:MIN_RESULTS]
            ]
            fallback_tags.add("eval_fallback")
        # else: the AI reviewed everyone and rejected them all -- contribute
        # nothing for this cluster rather than resurfacing a rejected job.

    # Bounded single-retry backfill: the judge call genuinely succeeded (not
    # call_failed) but this cluster still came back thin. rank_by_cluster
    # still holds the candidates this cluster lost to the JUDGE_POOL cut --
    # pull the next-highest-ranked of those and judge them too, once. Never
    # loops (no repeat backfill within a run), and never resurfaces a job
    # already rejected under this exact profile signature (same guard as
    # the main pass above).
    extras_fresh: list[dict] = []
    extras_cached: list[dict] = []
    b_strong, b_backup, b_disqualified = [], [], []
    backfill_call_succeeded = False
    if not call_failed and len(picks) < MIN_RESULTS:
        already_ids = {j.get("_identity") for j in jobs}
        extras = [c for c in rank_by_cluster.get(idx, [])
                  if c.get("_identity") not in already_ids][:engine.FINAL_EVAL_MAX_JOBS_PER_CALL]
        if extras:
            for c in extras:
                if c.get("_eval_signature") == eval_sig and c.get("_eval_verdict"):
                    if c["_eval_verdict"] in ("strong", "backup"):
                        try:
                            analysis = json.loads(c.get("_eval_analysis") or "{}")
                        except (ValueError, TypeError):
                            analysis = {}
                        extras_cached.append(dict(c, strong_fit=(c["_eval_verdict"] == "strong"), **analysis))
                    # else: cached "reject" under the current signature -- excluded, never retried
                else:
                    extras_fresh.append(c)

            if extras_fresh:
                b_strong, b_backup, b_disqualified = engine.final_evaluation_split(
                    extras_fresh, eng_profile, cv_text=cv_text)
                if b_strong is None:
                    b_strong, b_backup, b_disqualified = [], [], []  # call failed -- no persist, no backfill picks
                else:
                    backfill_call_succeeded = True

            backfill_picks = (
                [dict(p, strong_fit=True) for p in b_strong]
                + [p for p in extras_cached if p.get("strong_fit")]
                + [p for p in extras_cached if not p.get("strong_fit")]
                + [dict(p, strong_fit=False) for p in b_backup]
            )
            if backfill_picks:
                picks = picks + backfill_picks
                fallback_tags.add("judge_backfill")
                engine.emit(f"[pipeline] final_evaluation cluster[{idx}] ({label}) backfill: "
                            f"retried {len(extras)} next-ranked candidate(s), now {len(picks)} pick(s)")

    for p in picks:
        p["_cluster_label"] = label if len(role_clusters) > 1 else None

    return {
        "idx": idx, "label": label, "eval_sig": eval_sig,
        "fresh": fresh, "strong": strong, "backup": backup, "disqualified": disqualified,
        "call_failed": call_failed, "reused_from_cache": len(jobs) - len(fresh),
        "extras_for_to_evaluate": extras_fresh + extras_cached,
        "extras_fresh": extras_fresh, "b_strong": b_strong, "b_backup": b_backup,
        "b_disqualified": b_disqualified, "backfill_reused_from_cache": len(extras_cached),
        "backfill_call_succeeded": backfill_call_succeeded,
        "picks": picks, "backup_tier": backup_tier, "fallback_tags": fallback_tags,
    }


async def _run_engine_pipeline(engine, eng_profile, weighted_text, cv_text_base, db, profile_id, run: SearchRun):
    emit = engine.emit  # prints to the backend's own console (see run_search_task)
    timings: dict[str, float] = {}
    # Stage-by-stage candidate counts, persisted alongside timings (see
    # run_search_task) so a thin run can be diagnosed from the DB after the
    # fact instead of requiring a live console watch -- emit() only prints,
    # nothing else survives past the run. Built incrementally in-line with the
    # counts each stage already computes; an early return below simply carries
    # whatever keys were reached so far, same as timings already does.
    # `samples` rides inside funnel under the "samples" key so the pipeline's
    # return arity (and its four early returns) stay untouched; run_search_task
    # pops it back out into its own SearchRun column, keeping funnel_counts
    # ints/bools only. See _sample_stage / the Settings > Snapshot panel.
    funnel: dict = {}
    samples: dict[str, list[dict]] = {}
    funnel["samples"] = samples
    t0 = time.monotonic()

    def _lap(phase: str, since: float) -> float:
        timings[phase] = round(time.monotonic() - since, 2)
        return time.monotonic()

    def _snap(stage: str, items) -> None:
        """Record this stage's own total plus a few sample roles. The count is
        stored here rather than cross-referenced out of `funnel` because several
        stages (e.g. post-filter discovery) have no single funnel key of their
        own. Never let diagnostics break a real run."""
        try:
            seq = list(items or [])
            samples[stage] = {"count": len(seq), "samples": _sample_stage(seq)}
        except Exception:
            samples[stage] = {"count": 0, "samples": []}

    _check_cancelled(db, run)

    # The same signature screen_gate/rank_gate key their cache on. Computed once
    # here because it is needed at BOTH ends of the run: to decide which
    # gate-retired rows this profile has since invalidated (pool assembly below)
    # and to stamp the rows this run retires (see _mark/_gate_reopened_rows).
    gate_sig = engine._profile_signature(eng_profile)

    # Role clusters: usually one (today's behavior), sometimes several for a
    # candidate targeting genuinely different fields. Each gets its own
    # embedding so a job matching ONE of the candidate's role interests can
    # score well on its own merits, instead of every job being judged against
    # a single blended average of all of them. fallback_notes accumulates,
    # per cluster, which stages needed to fall back below the normal bar --
    # composed into the user-facing warning at the end.
    role_clusters = eng_profile.get("role_clusters") or [{"roles": [], "weighted_text": weighted_text}]
    cluster_texts = [c.get("weighted_text") or weighted_text for c in role_clusters]
    cluster_embeddings = engine.get_embeddings_batch(cluster_texts)
    fallback_notes: dict[int, set[str]] = defaultdict(set)
    emit(f"[pipeline] role clusters ({len(role_clusters)}): "
         + "; ".join(f"[{i}] {c.get('roles') or ['(none)']}" for i, c in enumerate(role_clusters)))

    # DISCOVERY (tiered, rotation included from first run) -> store. gather_jobs reads the flag.
    eng_profile["first_run"] = _is_first_run(db, profile_id)
    disabled = get_disabled(db)  # per-source toggle (workstream D)
    # The ~40-company ATS rotation batch is the single largest chunk of a run's
    # discovery calls, fetched fresh with no caching today. Skip it (fall back
    # to whatever's already in the store/backlog) when the last fetch for this
    # profile is still within the TTL, so pressing search twice in a row
    # doesn't always re-query every ATS company's board from scratch.
    ats_stale = eng_profile["first_run"] or get_ats_batch_stale(db, profile_id, DISCOVERY_ATS_CACHE_TTL_HOURS)
    if not ats_stale:
        disabled = disabled | ATS_KEYS
    eng_profile["disabled_sources"] = disabled
    emit(f"[pipeline] discovery start (first_run={eng_profile['first_run']}, "
         f"disabled={sorted(eng_profile['disabled_sources'])}, "
         f"ats_batch={'querying fresh' if ats_stale else 'skipped (cached, within TTL)'})")
    _progress(db, run, "Searching job boards…")
    raw_jobs = engine.gather_jobs(eng_profile)
    # search_terms_batch is set as a side effect of gather_jobs (select_sources_for_run
    # mutates the same eng_profile dict) -- log the terms this run actually queried,
    # not the full profile term list, since TERMS_PER_RUN rotates only a window of them.
    emit(f"[pipeline] discovery queried terms this run: {eng_profile.get('search_terms_batch') or []}")
    t0 = _lap("discovery", t0)
    _check_cancelled(db, run)

    # google_jobs tags board category/search-listing pages (e.g. a charityjob
    # "N jobs in X" results page) rather than dropping them -- follow a bounded
    # number of them and extract the individual postings inside, using the same
    # crawler infra as Phase 5. Runs before every other filter below so expanded
    # postings flow through blocklist/training/country/salary/dedupe exactly
    # like any other freshly discovered job.
    category_hits = [j for j in raw_jobs if j.get("_is_category_page")]
    raw_jobs = [j for j in raw_jobs if not j.get("_is_category_page")]
    if category_hits and CATEGORY_EXPAND_ENABLED:
        _progress(db, run, "Expanding job listing pages…")
        browser_config = engine.BrowserConfig(
            headless=True, verbose=False, viewport_width=1280, viewport_height=800,
            user_agent_mode="random",
        )
        async with engine.AsyncWebCrawler(config=browser_config) as crawler:
            expanded = await engine.expand_category_pages(category_hits, crawler)
        raw_jobs.extend(expanded)
        t0 = _lap("category_expand", t0)

    if ats_stale:
        mark_ats_batch_fetched(db, profile_id)
    funnel["raw_discovered"] = len(raw_jobs)
    # How long this profile's discovery store has been accumulating. Gates the
    # age tag's OBSERVATION clause: on a reset store (or a fresh deployment)
    # every row looks newly-discovered, and a per-row threshold alone cannot
    # tell that apart from a genuinely new listing. See
    # full_auto.OBSERVATION_MIN_STORE_DAYS -- below it, the clause is suppressed
    # entirely, which correctly makes the whole signal a no-op until it has had
    # time to mean something.
    store_age_days = _store_age_days(db, profile_id)
    eng_profile["store_age_days"] = store_age_days
    funnel["store_age_days"] = int(store_age_days)
    _snap("discovery", raw_jobs)
    raw_jobs, n_blocked = filter_blocked(raw_jobs, get_blocked_domains(db))
    funnel["blocklist_dropped"] = n_blocked
    if n_blocked:
        emit(f"[pipeline] spam-domain blocklist dropped {n_blocked} listing(s)")
    raw_jobs, n_training = _filter_training(engine, raw_jobs)
    funnel["training_dropped"] = n_training
    if n_training:
        emit(f"[pipeline] training/placement-scheme filter dropped {n_training} listing(s)")
    breakdown = _board_breakdown(raw_jobs)
    emit(f"[pipeline] discovery returned {len(raw_jobs)} raw listings by board: {breakdown}")
    save_last_run_counts(db, counts_from_breakdown(breakdown))  # for the settings screen

    country_codes = eng_profile.get("country_codes") or []
    if country_codes:
        before = len(raw_jobs)
        raw_jobs = _filter_by_country(engine, raw_jobs, country_codes)
        funnel["country_filter_raw_before"] = before
        funnel["country_filter_raw_after"] = len(raw_jobs)
        emit(f"[pipeline] country filter {country_codes}: {before} -> {len(raw_jobs)} listings")

    local_place = eng_profile.get("local_place") or ""
    if eng_profile.get("location_scope") == "local" and local_place:
        before = len(raw_jobs)
        raw_jobs = _filter_by_local_place(raw_jobs, local_place)
        emit(f"[pipeline] local-place filter ({local_place!r}): {before} -> {len(raw_jobs)} listings")

    # Salary hard-filter on fresh discovery only (salary isn't persisted on the
    # JobSeen store, so it can't be re-applied to backlog candidates -- but a
    # clearly-underpaid fresh listing is dropped here before it ever enters the
    # store). Unknown salary passes through.
    salary_floor = int(eng_profile.get("salary_floor") or 0)
    if salary_floor > 0:
        before = len(raw_jobs)
        raw_jobs = _filter_by_salary(raw_jobs, salary_floor)
        funnel["salary_filtered"] = before - len(raw_jobs)
        emit(f"[pipeline] salary filter (floor={salary_floor}): {before} -> {len(raw_jobs)} listings")

    # Scam/CV-farming structural signal (see _company_title_counts): computed once
    # over this run's own fresh discovery batch, before it's merged into the
    # persistent store -- a proxy for "this source posted an unusually templated
    # catalogue of roles this run" without needing to visit the source's own
    # listing page.
    company_title_counts = _company_title_counts(raw_jobs)
    _snap("after_filters", raw_jobs)

    inserted, refreshed, requeued = _upsert_discovered(db, profile_id, raw_jobs)
    funnel["store_inserted"] = inserted
    funnel["store_refreshed"] = refreshed
    funnel["store_requeued"] = requeued
    store_counts = _store_counts(db, profile_id)
    emit(f"[pipeline] store upsert: +{inserted} new, {refreshed} refreshed, "
         f"{requeued} requeued | store totals for this profile: {store_counts}")

    # ASSEMBLE the candidate rows: the whole 'new' store, PLUS previously-seen roles
    # the judge already liked but that were never shown. Without this second part, a
    # good match seen once but out-ranked for a FINAL_PICKS slot stayed frozen in the
    # store forever -- the old top-up only pulled from the backlog when fresh 'new'
    # rows fell below TARGET_POOL, which rarely happens on an active profile, so the
    # backlog was effectively write-only. Resurfacing runs every time now; it's cheap
    # because a resurfaced row's gate/rank/judge results are all served from cache.
    rows = _new_rows(db, profile_id)
    seen_ids = {r.identity_hash for r in rows}
    resurfaced = [r for r in _backlog_rows(db, profile_id, BACKLOG_TOPUP, verdicts=("strong", "backup"))
                  if r.identity_hash not in seen_ids]
    rows = rows + resurfaced
    seen_ids.update(r.identity_hash for r in resurfaced)
    # ...plus rows the cheap gate retired under a profile the candidate has since
    # edited. Unlike the two pulls above this is uncapped: it isn't a "top-up when
    # thin" safety net but a correction to the pool's definition, and capping it at
    # BACKLOG_TOPUP would silently keep most of the invalidated backlog out. They
    # cost one cosine each here and still have to clear RELEVANCE_FLOOR and out-score
    # everything else for an examine slot.
    reopened = [r for r in _gate_reopened_rows(db, profile_id, gate_sig)
                if r.identity_hash not in seen_ids]
    if reopened:
        rows = rows + reopened
        seen_ids.update(r.identity_hash for r in reopened)
        emit(f"[pipeline] re-opened {len(reopened)} row(s) the cheap gate retired under an "
             f"older profile signature (profile has changed since)")
    funnel["pool_gate_reopened"] = len(reopened)
    n_fresh = len(rows) - len(resurfaced) - len(reopened)
    if len(rows) < TARGET_POOL:
        # Thin-run safety net (the original behaviour): still short of a full pool, so
        # top up with any other enriched rows (except known rejects) to avoid an empty
        # screen.
        extra = [r for r in _backlog_rows(db, profile_id, BACKLOG_TOPUP, exclude_rejects=True)
                 if r.identity_hash not in seen_ids]
        rows = rows + extra
        resurfaced = resurfaced + extra
    emit(f"[pipeline] pool assembled: {n_fresh} fresh 'new' + {len(resurfaced)} "
         f"resurfaced backlog + {len(reopened)} gate-reopened row(s) -> {len(rows)} total")
    funnel["pool_rows"] = len(rows)
    funnel["pool_resurfaced"] = len(resurfaced)
    _snap("pool", rows)
    if not rows:
        emit("[pipeline] STOP: nothing to evaluate (store empty and no backlog) -> 0 results")
        return [], False, None, timings, None, funnel

    # FILTER: embed (cached) + cosine-score the whole set against every role
    # cluster, take a fair adaptive pool per cluster.
    _progress(db, run, f"Found {len(rows)} jobs, scoring…")
    n_reused, n_embedded = _ensure_embeddings(engine, db, rows)
    funnel["embedded_new"] = n_embedded
    if n_embedded or n_reused:
        emit(f"[pipeline] embedded {n_embedded} new rows "
             f"({n_reused} reused from shared cache; all cached for future runs)")
    t0 = _lap("embed", t0)
    scored = _score_rows(rows, cluster_embeddings)
    t0 = _lap("score", t0)

    # Re-apply the country filter to the *candidate* set, not just this run's fresh
    # discovery. rows come from the persistent store (_new_rows + backlog), which
    # can hold listings discovered on an earlier run or under different country
    # settings; those bypass the raw_jobs filter above, so an out-of-country role
    # (e.g. "Remote - US" for a GB profile) would otherwise leak into results.
    if country_codes:
        before = len(scored)
        scored = _filter_by_country(engine, scored, country_codes)
        funnel["country_filter_scored_before"] = before
        funnel["country_filter_scored_after"] = len(scored)
        emit(f"[pipeline] country filter on candidates {country_codes}: "
             f"{before} -> {len(scored)} rows")
    if eng_profile.get("location_scope") == "local" and local_place:
        before = len(scored)
        scored = _filter_by_local_place(scored, local_place)
        emit(f"[pipeline] local-place filter on candidates ({local_place!r}): "
             f"{before} -> {len(scored)} rows")
    top_score = scored[0]["embed_score"] if scored else 0.0
    above_primary = sum(1 for j in scored if j["embed_score"] >= RELEVANCE_PRIMARY)
    funnel["scored_total"] = len(scored)
    funnel["above_relevance_primary"] = above_primary
    emit(f"[pipeline] scored {len(scored)} candidates | top_score={top_score:.3f} | "
         f">= RELEVANCE_PRIMARY({RELEVANCE_PRIMARY})={above_primary}")
    _log_score_distribution(emit, scored)
    _snap("scored", scored)

    scored, n_prescreen = _heuristic_prescreen(scored, eng_profile)
    funnel["heuristic_prescreen_dropped"] = n_prescreen
    if n_prescreen:
        emit(f"[pipeline] heuristic prescreen dropped {n_prescreen} obvious seniority mismatch(es) "
             f"before any gate")
    _snap("heuristic_survivors", scored)

    skipped_clusters = _clusters_without_fresh_terms(eng_profile, role_clusters)
    if skipped_clusters:
        before = len(scored)
        scored = [j for j in scored if j.get("_cluster", 0) not in skipped_clusters]
        funnel["skipped_clusters"] = sorted(skipped_clusters)
        for idx in skipped_clusters:
            fallback_notes[idx].add("cluster_skipped")
        emit(f"[pipeline] cluster(s) {sorted(skipped_clusters)} got no fresh search terms "
             f"this run's rotation window -- skipping ({before - len(scored)} candidate(s) "
             f"excluded); full budget goes to the remaining cluster(s), its turn comes on a future run")

    queues, harsh, pool_fallbacks = _cluster_candidate_queues(scored)
    for idx, reason in pool_fallbacks.items():
        fallback_notes[idx].add(reason)
    total_queued = sum(len(q) for q in queues.values())
    funnel["candidate_queue_size"] = total_queued
    funnel["pool_harsh"] = harsh

    # PAINT 1 of 3 (embedding stage). The queues are embed-score-ordered and have
    # already been through the country filter, the free heuristic prescreen and
    # RELEVANCE_FLOOR, so their heads are the best thing known this early -- and
    # this early is seconds in, against ~90s for the first gate+rank round. Pure
    # DB write over dicts already in memory: no LLM, no fetch, nothing added to
    # the critical path. _top_n_across_clusters reuses the same global sort +
    # _dup_key collapse the rank-stage paint uses, so aggregator reposts don't
    # show up as several identical cards here either. Rows land provisional=True,
    # so every existing safety net already covers them -- /my-roles and stats
    # never see them (include_provisional), and a cancelled or failed run reaps
    # them (_cleanup_provisional_roles), preserving "an unfinished run leaves
    # nothing user-visible".
    _upsert_provisional_rows(
        db, profile_id, run,
        _top_n_across_clusters(
            {i: q[:EMBED_PAINT_MAX] for i, q in queues.items()}, EMBED_PAINT_MAX,
            key=lambda j: j.get("embed_score", 0.0),
        ),
        engine, stage=PROVISIONAL_STAGE_EMBED,
    )
    emit(f"[pipeline] candidate queues: {total_queued} total across {len(queues)} cluster(s) "
         f"available to gate (>= RELEVANCE_FLOOR) (harsh/broadened={harsh}) "
         f"fallbacks={dict(pool_fallbacks)}")
    if not any(queues.values()):
        emit(f"[pipeline] STOP: no candidates above RELEVANCE_FLOOR({RELEVANCE_FLOOR}) in any cluster -> 0 results")
        return [], harsh, None, timings, _compose_fallback_warning(role_clusters, fallback_notes), funnel

    # GATE + RANK, combined per cluster with refill: each cluster's own
    # embed-score-ordered queue is fed through screen_gate then rank_gate in
    # batches (see _gate_rank_refill_cluster) until that cluster's fair share
    # of RANK_TARGET_POOL rank-floor survivors accumulate, the queue is
    # exhausted, or its fair share of RANK_EXAMINE_BUDGET has been examined --
    # unlike the old one-shot TARGET_POOL-capped gate call, this keeps pulling
    # from the idle above-floor pool instead of accepting a thin result when a
    # harsher gate or the rank floor (RANK_REJECT_SCORE_FLOOR) leaves a cluster
    # short. Both budgets are run-wide and split evenly across active clusters,
    # so a multi-stream profile costs the same as a single-stream one. sector_ok
    # stays the one unconditional hard drop within screen_gate; 2+ of the 5
    # soft axes failing (seniority/requirements/skills/salary/work-arrangement)
    # is now ALSO a hard drop (see full_auto.screen_gate/SOFT_GATE_AXES), with
    # the cluster-level MIN_RESULTS floor backfill in _gate_rank_refill_cluster
    # as the safety net against starving a cluster to zero -- the same failure
    # mode a purely hard-drop seniority gate caused once before (see CLAUDE.md).
    _progress(db, run, "Screening & ranking candidates…")
    rank_by_cluster: dict[int, list[dict]] = {}
    examined_ids: set[str] = set()
    gate_survivor_ids: set[str] = set()
    below_rank_floor_all: list[dict] = []
    total_examined = total_gate_survivors = total_judge_eligible = 0
    total_hard_gate_dropped = 0
    cluster_diagnostics: dict[int, dict] = {}
    num_active_clusters = sum(1 for q in queues.values() if q)
    # Both budgets are run-wide totals split evenly, so the run's cost is the
    # same whether the candidate has one role family or three -- the shares just
    # get thinner. cluster_judge_target aims at RANK_TARGET_POOL (80), NOT at
    # JUDGE_POOL (40): the mid tier deliberately approves about twice what the
    # judge will use so the expensive stage picks the best of a real pool rather
    # than judging whatever survived. See RANK_TARGET_POOL / RANK_EXAMINE_BUDGET.
    _n = num_active_clusters or 1
    cluster_judge_target = -(-RANK_TARGET_POOL // _n)
    cluster_examine_cap = -(-RANK_EXAMINE_BUDGET // _n)

    # Give the cheap stages something real to read first. Scoped to the head of
    # each cluster's queue -- already embed-score-ordered, so this is the slice
    # the gate reaches first -- rather than the whole store, most of which never
    # reaches a gate. Runs here, on the main thread, before the cluster pool
    # starts, because it commits to the request session; that also puts it
    # squarely in front of time-to-first-card, which is why it's capped at
    # REED_ENRICH_PRE_GATE_CAP / ADZUNA_ENRICH_PRE_GATE_CAP rather than following
    # cluster_examine_cap all the way out to RANK_EXAMINE_BUDGET. Between them these
    # two sources are ~91% of the store's text-starved rows (see the text-supply note
    # in CLAUDE.md). See _enrich_reed_full_text / _enrich_adzuna_full_text.
    enrich_slice = min(cluster_examine_cap, -(-REED_ENRICH_PRE_GATE_CAP // _n))
    to_enrich = [j for queue in queues.values() for j in queue[:enrich_slice]]
    n_enriched = _enrich_reed_full_text(engine, db, profile_id, to_enrich)
    funnel["reed_enriched"] = n_enriched
    # Adzuna runs on its own, narrower slice -- see ADZUNA_ENRICH_PRE_GATE_CAP. It is
    # the same blocking main-thread HTTP sitting in front of first paint that the Reed
    # cap exists to bound, but each response is a ~100KB page from a host that
    # rate-limits, so it cannot ride the same budget.
    adz_slice = min(cluster_examine_cap, -(-ADZUNA_ENRICH_PRE_GATE_CAP // _n))
    to_enrich_adz = [j for queue in queues.values() for j in queue[:adz_slice]]
    n_adz = _enrich_adzuna_full_text(engine, db, profile_id, to_enrich_adz)
    funnel["adzuna_enriched"] = n_adz
    n_enriched += n_adz
    if n_enriched:
        emit(f"[pipeline] enriched {n_enriched} candidate(s) with their full description "
             f"before gating ({funnel['reed_enriched']} Reed, {n_adz} Adzuna; cached for "
             f"future runs; these now skip phase 5)")
    t0 = _lap("enrich", t0)

    # Judge every cluster's queue concurrently. Both budgets above
    # (cluster_judge_target / cluster_examine_cap) are derived from
    # num_active_clusters BEFORE any cluster runs, and each call's results are
    # merged afterwards, so -- exactly as with the per-cluster final eval below
    # -- there is no cross-cluster fairness decision left for concurrency to
    # disturb. Sequentially, this was the single largest phase of a run (a live
    # 3-cluster run spent 83s here, ~28s per cluster back-to-back), because
    # every round is a blocking cheap-model gate call followed by a blocking
    # mid-model rank call.
    active_clusters = [(idx, queue) for idx, queue in queues.items() if queue]
    cancel_check = _make_cancel_check(run.id)
    gate_results: dict[int, tuple[list[dict], list[dict], dict]] = {}
    # Progress reports flow worker-thread -> main-thread over a queue.Queue
    # (the one-way counterpart to `cancel_check`, which flows the other way) so
    # each cluster's judge-eligible candidates can be persisted as provisional
    # "being verified..." rows the moment the very first gate+rank round
    # returns, not just once an entire cluster (or every cluster) finishes --
    # see _gate_rank_refill_cluster's `report` param and _upsert_provisional_rows.
    progress_q: "queue.Queue" = queue.Queue()
    cluster_accum: dict[int, list[dict]] = {idx: [] for idx, _ in active_clusters}
    # Stashed so a JUDGE_POOL_FLOOR shortfall can re-enter the SAME cluster's
    # queue with the same profile/context later, instead of only being able to
    # recycle already-rank-rejected candidates -- see the call site below.
    cluster_profiles: dict[int, dict] = {}
    if active_clusters:
        with ThreadPoolExecutor(max_workers=len(active_clusters)) as pool:
            futures = {}
            for idx, cluster_queue in active_clusters:
                cluster_profile = dict(eng_profile)
                cluster_profile["search_terms"] = (
                    role_clusters[idx].get("roles") or eng_profile.get("search_terms"))
                cluster_profile["_multi_cluster"] = len(role_clusters) > 1
                cluster_profiles[idx] = cluster_profile
                futures[pool.submit(
                    _gate_rank_refill_cluster, cluster_queue, cluster_profile, engine, cancel_check,
                    cluster_judge_target, cluster_examine_cap, _make_progress_reporter(progress_q, idx),
                )] = idx

            pending = set(futures)
            while pending:
                try:
                    p_idx, snapshot = progress_q.get(timeout=0.2)
                    cluster_accum[p_idx] = snapshot  # replace: snapshot is that cluster's full state so far
                    _check_cancelled(db, run)         # DB-aware check; main thread owns `db`
                    _upsert_provisional_rows(
                        db, profile_id, run, _top_n_across_clusters(cluster_accum, PROVISIONAL_MAX), engine)
                except queue.Empty:
                    pass
                done_now = {f for f in pending if f.done()}
                for f in done_now:
                    gate_results[futures[f]] = f.result()
                pending -= done_now
            # report() always completes before _gate_rank_refill_cluster returns, so
            # nothing further will arrive after every future is done -- but the queue
            # may still hold buffered items the loop above hasn't drained yet.
            while True:
                try:
                    p_idx, snapshot = progress_q.get_nowait()
                    cluster_accum[p_idx] = snapshot
                except queue.Empty:
                    break
            if cluster_accum:
                _upsert_provisional_rows(
                    db, profile_id, run, _top_n_across_clusters(cluster_accum, PROVISIONAL_MAX), engine)

    # Merge sequentially, in queue order, so the funnel counters, fallback tags
    # and log lines stay deterministic regardless of which cluster finished first.
    for idx, cluster_queue in active_clusters:
        judge_eligible, gate_survivors, stats = gate_results[idx]
        rank_by_cluster[idx] = judge_eligible
        examined_ids.update(j["_identity"] for j in cluster_queue[:stats["examined"]])
        gate_survivor_ids.update(j["_identity"] for j in gate_survivors)
        below_rank_floor_all.extend(stats["below_rank_floor_jobs"])
        total_examined += stats["examined"]
        total_gate_survivors += stats["gate_survivors"]
        total_judge_eligible += stats["judge_eligible"]
        total_hard_gate_dropped += stats["hard_gate_dropped"]
        if stats["backfilled"]:
            # Same tag/message as the old one-shot design's starvation backfill
            # (see _compose_fallback_warning) -- MIN_RESULTS safety net had to
            # promote a hard-dropped/off-sector/rank-floor-rejected candidate,
            # a genuine "results were thin" signal. Hitting this cluster's
            # examine cap or exhausting the queue without reaching its judge
            # target is NOT tagged here -- that's the normal, expected outcome
            # for a niche cluster with fewer than its target's worth of decent
            # candidates in the store at all.
            fallback_notes[idx].add("gate_fallback")
        emit(f"[gate+rank] cluster[{idx}] ({_cluster_label(role_clusters[idx])}) fed "
             f"{stats['examined']}/{stats['queue_len']} qualifying candidates -> "
             f"{stats['gate_survivors']} gate survivors -> {stats['judge_eligible']} judge-eligible "
             f"(hard_dropped={stats['hard_dropped']}, off_sector={stats['off_sector']}, "
             f"rank_floor_rejected={stats['rank_floor_rejected']}) "
             f"(stopped: {stats['stop_reason']})")
        # Per-cluster funnel, for the Settings "Search run timings" panel. The
        # run-wide funnel_counts can't answer "which track did badly and where" --
        # it sums every cluster together, so a strong stream and a starving one
        # average into numbers that look healthy. Judge-side counts are filled in
        # after Phase 6 below.
        cluster_diagnostics[idx] = {
            "idx": idx, "label": _cluster_label(role_clusters[idx]),
            "queue_len": stats["queue_len"], "examined": stats["examined"],
            "gate_survivors": stats["gate_survivors"], "hard_dropped": stats["hard_dropped"],
            "off_sector": stats["off_sector"], "hard_gate_dropped": stats["hard_gate_dropped"],
            "rank_floor_rejected": stats["rank_floor_rejected"],
            "judge_eligible": stats["judge_eligible"], "stop_reason": stats["stop_reason"],
        }
    t0 = _lap("gate", t0)

    # Judge-pool FLOOR (symmetric to the JUDGE_POOL ceiling): if the gate+rank
    # funnel left fewer than JUDGE_POOL_FLOOR candidates eligible for the expensive
    # judge, run one bounded EXTRA gate+rank round per still-open cluster over its
    # next unexamined candidates, rather than recycling already-rank-rejected jobs
    # (which, by construction, already scored < RANK_REJECT_SCORE_FLOOR and have no
    # better shot at the judge than they already had). Only clusters whose first
    # round stopped on a CAP (target_reached / absolute_pool_cap) -- not a
    # genuinely exhausted queue -- have anything fresh left; a cluster that already
    # burned through its whole queue is simply left short, no reject fallback (a
    # prior version promoted below-floor rejects here instead -- dropped after an
    # investigation found the judge's own scam-suspicion signals, and a downstream
    # scam-verify check, were producing false positives on legitimate high-volume
    # agency listings, making "borderline reject" a much weaker signal of genuine
    # unfitness than assumed). Runs BEFORE dup-suppression/fair-allocate so those
    # stages treat any extra-round survivors uniformly with the rest.
    judge_floor_extra_examined = 0
    if total_judge_eligible < JUDGE_POOL_FLOOR:
        need = JUDGE_POOL_FLOOR - total_judge_eligible
        reopenable = [
            (idx, cluster_queue) for idx, cluster_queue in active_clusters
            if cluster_diagnostics[idx]["stop_reason"] != "pool_exhausted"
            and cluster_diagnostics[idx]["examined"] < len(cluster_queue)
        ]
        if reopenable:
            # Split the shortfall evenly across clusters that can actually supply
            # more candidates -- same fairness principle as cluster_judge_target.
            per_cluster_target = -(-need // len(reopenable))
            with ThreadPoolExecutor(max_workers=len(reopenable)) as pool:
                extra_futures = {
                    pool.submit(
                        _gate_rank_refill_cluster,
                        cluster_queue[cluster_diagnostics[idx]["examined"]:],
                        cluster_profiles[idx], engine, cancel_check,
                        per_cluster_target, JUDGE_POOL_FLOOR_EXTRA_CAP,
                    ): idx
                    for idx, cluster_queue in reopenable
                }
                for f in extra_futures:
                    idx = extra_futures[f]
                    extra_eligible, _extra_survivors, extra_stats = f.result()
                    rank_by_cluster.setdefault(idx, []).extend(extra_eligible)
                    rank_by_cluster[idx].sort(key=_selection_score, reverse=True)
                    below_rank_floor_all.extend(extra_stats["below_rank_floor_jobs"])
                    judge_floor_extra_examined += extra_stats["examined"]
                    total_judge_eligible += extra_stats["judge_eligible"]
                    total_examined += extra_stats["examined"]
                    total_gate_survivors += extra_stats["gate_survivors"]
                    emit(f"[pipeline] judge-pool floor: cluster[{idx}] extra round examined "
                         f"{extra_stats['examined']} more candidate(s) -> "
                         f"{extra_stats['judge_eligible']} newly judge-eligible "
                         f"(total now {total_judge_eligible}, floor={JUDGE_POOL_FLOOR})")
        if total_judge_eligible < JUDGE_POOL_FLOOR:
            emit(f"[pipeline] judge-pool floor: still {total_judge_eligible} judge-eligible after the "
                 f"extra round (floor={JUDGE_POOL_FLOOR}) -- no reject fallback, accepting the shortfall")
    funnel["judge_floor_extra_examined"] = judge_floor_extra_examined
    # Own lap, split out from "rank" below: this block (when it runs at all) is
    # a whole extra screen_gate + rank_gate round -- real LLM calls, not
    # bookkeeping -- and was previously folded into the "rank" timing under the
    # label "Fair-allocate to the judge pool", which made a single-cluster run
    # that had to top up its judge pool look like fair-allocate itself (a pure
    # Python reshuffle of a few dozen dicts) was taking tens of seconds. Zero
    # candidates examined here still records a real (near-zero) lap rather than
    # silently folding into the next one.
    t0 = _lap("judge_floor_topup", t0)

    # Near-duplicate suppression before the expensive judge: same company, same
    # title, near-identical text (a recruiter template re-posted per city) keeps
    # only its top-ranked copy -- see _suppress_judge_duplicates.
    # Plus the CROSS-RUN family check: a role the user already saved/applied to,
    # re-advertised by the same employer under a different city (the GRAYCE
    # case). Keys are read here, on the main thread, once per run.
    decided_keys = _decided_role_keys(db, profile_id)
    n_dupes, n_decided, decided_hits = _suppress_judge_duplicates(rank_by_cluster, decided_keys)
    funnel["judge_dupes_suppressed"] = n_dupes
    funnel["decided_family_keys"] = len(decided_keys)
    funnel["decided_family_suppressed"] = n_decided
    funnel["decided_family_shadow"] = not DECIDED_FAMILY_SUPPRESS_ENABLED
    if n_dupes:
        emit(f"[pipeline] suppressed {n_dupes} near-duplicate posting(s) before the judge pool "
             f"(same company+title+text; top-ranked copy retained)")
    if n_decided:
        mode = "suppressed" if DECIDED_FAMILY_SUPPRESS_ENABLED else "WOULD suppress (shadow mode)"
        emit(f"[pipeline] {mode} {n_decided} listing(s) matching a role already "
             f"saved/applied: " + "; ".join(
                 f"{comp} / {title} x{n}" for (comp, title), n in
                 sorted(decided_hits.items(), key=lambda kv: -kv[1])[:5]))

    selected = _fair_allocate(rank_by_cluster, JUDGE_POOL)
    # Now genuinely just dedup + fair-allocate -- the judge-floor top-up round
    # above is timed separately. Expect this to read near-instant.
    t0 = _lap("rank", t0)
    funnel["gate_survivors_total"] = total_gate_survivors
    funnel["rank_scored"] = total_examined
    funnel["judge_eligible_total"] = total_judge_eligible
    funnel["hard_gate_dropped"] = total_hard_gate_dropped
    funnel["judge_pool_size"] = len(selected)
    # Flatten the per-cluster judge-eligible lists for sampling: the Snapshot
    # panel reports the pipeline stage-by-stage, not cluster-by-cluster.
    _snap("judge_eligible", [j for jl in rank_by_cluster.values() for j in jl])
    _snap("rank_rejected", below_rank_floor_all)
    _snap("judge_pool", selected)
    emit(f"[pipeline] gate+rank across {len(rank_by_cluster)} cluster(s): {total_examined} examined -> "
         f"{total_gate_survivors} gate survivors -> {total_judge_eligible} judge-eligible -> "
         f"top-{len(selected)} sent to full evaluation")
    if not selected:
        # Should be unreachable: every cluster with a non-empty queue gets a
        # guaranteed non-empty fallback in _gate_rank_refill_cluster. Kept as
        # a defensive backstop.
        emit("[pipeline] STOP: gate+rank produced nothing despite non-empty queues -> 0 results")
        return [], True, ([j["_identity"] for j in scored], [], gate_sig), timings, \
            _compose_fallback_warning(role_clusters, fallback_notes), funnel
    _check_cancelled(db, run)

    # Reconcile this run's provisional "being verified..." rows against the
    # real, fair-allocated judge pool -- interim rows may already exist from
    # gate+rank rounds finishing earlier (see the progress_q loop above), so
    # this upserts/reaps rather than blind-inserting. The scrape+judge tail
    # below is still the run's longest phase (~90s live); this just fixes the
    # display's fit_rank/membership up to the true fair-allocated state before
    # that tail starts.
    matched, inserted, removed = _reconcile_provisional_roles(db, profile_id, run, selected, engine)
    funnel["provisional_persisted"] = matched + inserted
    funnel["provisional_reconcile_matched"] = matched
    funnel["provisional_reconcile_inserted"] = inserted
    funnel["provisional_reconcile_removed"] = removed
    emit(f"[pipeline] provisional reconcile: {matched} interim row(s) matched, {inserted} newly "
         f"persisted, {removed} resolved away (deduped/dropped by fair-allocate) -> "
         f"{matched + inserted} provisional role(s) live for early display "
         f"(upgraded/removed at finalization)")

    # EVALUATE (phases 5 + 6), PIPELINED PER CLUSTER. When full-page scraping is
    # enabled (default), each selected job's real page is read first so the final
    # LLM judges fit against the actual posting text (seniority/experience/
    # location) instead of a short snippet -- but only for jobs whose snippet
    # doesn't already have enough to judge from (see _needs_full_scrape); skipping
    # the rest is most of the win there, since it's the largest source of both run
    # time and anti-bot blocking.
    #
    # These used to be two strictly sequential whole-run phases: scrape ALL 40
    # selected jobs, then judge, cluster by cluster in a thread pool. Since
    # _fair_allocate has already fixed each cluster's share by this point, nothing
    # about a cluster's judging depends on any other cluster's pages -- so each
    # cluster now scrapes and then judges on its own task, and one cluster's
    # (expensive, blocking) judge call overlaps the others' page fetching. On a
    # live 3-cluster run those two phases cost 48s + 53s back-to-back.
    #
    # Judging itself is unchanged: still ONE expensive call per cluster (given a
    # cv_text scoped to just that cluster's roles -- see cv_text_for_cluster) so
    # the judge weighs fit against ONE coherent role identity, returning both a
    # strict "strong" list and a lenient disqualifier-only "backup" list. Jobs
    # already judged under this exact CV are served from their stored verdict.
    # Second chance at real text, for the judge pool only. The pre-gate enrichers
    # above are capped (REED_ENRICH_PRE_GATE_CAP / ADZUNA_ENRICH_PRE_GATE_CAP) and
    # ordered by embed score, because they sit in front of time-to-first-card -- so
    # a candidate that climbs into the judge pool from outside that head slice
    # reaches the expensive model still holding its ~500-char teaser.
    #
    # For Reed that merely wastes a phase-5 page fetch. For Adzuna it is terminal:
    # the API's /jobs/land/ad/ URL is a JS interstitial, so _needs_full_scrape skips
    # it and the judge grades the posting on a company blurb. A live run rejected an
    # Adzuna "BI Analyst" with "the available description does not provide enough
    # role requirements or seniority detail to establish a genuine fit" -- while the
    # posting's own detail page carried a full responsibilities-and-Power-BI
    # requirements section the pipeline never fetched.
    #
    # Neither cap's reason applies here: this runs AFTER the provisional cards are
    # on screen (nothing is waiting on it), it is plain HTTP with no LLM and no
    # browser, and it is bounded by JUDGE_POOL(40) rather than by an examine budget.
    # It also SHRINKS phase 5, since anything enriched now skips the scrape.
    n_judge_enriched = (_enrich_reed_full_text(engine, db, profile_id, selected)
                        + _enrich_adzuna_full_text(engine, db, profile_id, selected))
    funnel["judge_pool_enriched"] = n_judge_enriched
    if n_judge_enriched:
        emit(f"[pipeline] enriched {n_judge_enriched} judge-pool candidate(s) with their full "
             f"description before final review (missed by the pre-gate caps)")

    scrape_enabled = get_full_scrape_enabled(db)
    if not scrape_enabled:
        emit("[pipeline] full-page scraping disabled in settings; evaluating on snippets")
    blocked_domains = set(get_blocked_domains(db))
    scrape_country = eng_profile.get("adzuna_country_code", "gb")

    selected_by_cluster: dict[int, list[dict]] = defaultdict(list)
    for j in selected:
        company = (j.get("company") or "").strip().lower()
        n_titles = len(company_title_counts.get(company, ()))
        if n_titles >= TEMPLATE_FACTORY_TITLE_THRESHOLD:
            j["_posting_volume_hint"] = f"{n_titles} differently-titled roles from this source this run"
        selected_by_cluster[j.get("_cluster", 0)].append(j)
    cluster_items = list(selected_by_cluster.items())

    # Shared across the concurrent per-cluster scrapes so the crawler still uses
    # ONE MAX_CONCURRENT-wide lane and ONE alt-source lookup budget for the whole
    # run -- without these, N clusters would mean N independent lanes/budgets (see
    # full_auto.scrape_full_details' `sem`/`alt_budget` params).
    scrape_sem = asyncio.Semaphore(engine.MAX_CONCURRENT)
    scrape_alt_budget = [engine.ALT_SOURCE_LOOKUP_MAX_PER_RUN]
    scraped_all: list[dict] = []   # every freshly-fetched job, for the main-thread persist
    dead_all: list[dict] = []
    scrape_counts = {"needed": 0, "already_ready": 0}

    async def _scrape_cluster(idx: int, jobs: list[dict], crawler) -> list[dict]:
        """Phase 5 for ONE cluster. Returns the jobs that should go on to its
        judge (snippet-sufficient ones plus successfully-scraped live ones);
        confirmed-dead listings are dropped here and collected for the caller to
        persist. Runs on the event loop, so appending to the shared lists below
        needs no lock."""
        if not scrape_enabled or crawler is None:
            return jobs
        needs_scrape, already_ready = [], []
        for j in jobs:
            if _needs_full_scrape(j):
                needs_scrape.append(j)
            else:
                j["full_text"] = j.get("snippet", "")
                already_ready.append(j)
        scrape_counts["needed"] += len(needs_scrape)
        scrape_counts["already_ready"] += len(already_ready)
        emit(f"[pipeline] phase 5 cluster[{idx}]: {len(needs_scrape)}/{len(jobs)} candidates need a "
             f"full-page scrape ({len(already_ready)} already have enough detail from their source)")
        if not needs_scrape:
            return already_ready
        scraped = await engine.scrape_full_details(
            needs_scrape, crawler, blocked_domains=blocked_domains,
            country_code=scrape_country, sem=scrape_sem, alt_budget=scrape_alt_budget,
        )
        scraped_all.extend(scraped)
        dead = [j for j in scraped if j.get("_dead_reason")]
        dead_all.extend(dead)
        if dead:
            emit(f"[pipeline] phase 5 cluster[{idx}]: {len(dead)} listing(s) confirmed dead/expired "
                 f"(no alt-source recovery) -- excluded before final judge")
        return already_ready + [j for j in scraped if not j.get("_dead_reason")]

    async def _scrape_then_judge(idx: int, jobs: list[dict], crawler) -> dict:
        ready = await _scrape_cluster(idx, jobs, crawler)
        # _run_cluster_final_eval is blocking (expensive-model calls) but makes no
        # DB writes and touches no shared state, so it's safe in a worker thread --
        # which is what lets the OTHER clusters keep scraping while it runs.
        result = await asyncio.to_thread(
            _run_cluster_final_eval, idx, ready, role_clusters, cv_text_base,
            eng_profile, rank_by_cluster, engine,
        )
        result["_evaluated"] = ready
        return result

    final_by_cluster: dict[int, list[dict]] = {}
    final_fresh_judged = final_reused_from_cache = 0
    final_strong = final_backup = final_disqualified = final_reject_reasoned = 0
    final_scam_verified_dropped = 0

    def _hard_dq(entries) -> int:
        """Judge exclusions that fired a DISQUALIFIERS rule, as opposed to jobs that
        merely lost out. Both kinds ride in the same list now (see
        full_auto.final_evaluation_split) so that every reject carries a reason; this
        keeps `final_disqualified` meaning what it meant before -- "did the judge
        actually hard-reject anyone" -- rather than silently becoming "how many
        weren't picked", which is nearly all of them and diagnoses nothing."""
        return sum(1 for d in (entries or []) if d.get("_disqualifier"))
    scam_verify_budget = [SCAM_VERIFY_MAX_PER_RUN]

    _progress(db, run, "Reading job pages & final AI review…")
    _check_cancelled(db, run)

    async def _run_all(crawler) -> list[dict]:
        return list(await asyncio.gather(
            *[_scrape_then_judge(idx, jobs, crawler) for idx, jobs in cluster_items]
        ))

    # Only pay for a browser launch when something actually needs fetching.
    if scrape_enabled and any(_needs_full_scrape(j) for j in selected):
        browser_config = engine.BrowserConfig(
            headless=True, verbose=False, viewport_width=1280, viewport_height=800,
            user_agent_mode="random",
        )
        async with engine.AsyncWebCrawler(config=browser_config) as crawler:
            results = await _run_all(crawler)
    else:
        results = await _run_all(None)

    eval_results: dict[int, dict] = {r["idx"]: r for r in results}
    to_evaluate = [j for r in results for j in r["_evaluated"]]

    # Scrape persistence stays on the main thread (these touch the request
    # session): remember the page text so a resurfacing job isn't re-scraped, and
    # record confirmed-dead listings so no future run considers them at all.
    if scraped_all:
        _persist_scrape(db, profile_id, scraped_all)
        _persist_dead_scrapes(db, profile_id, dead_all)
    if scrape_enabled:
        funnel["scrape_needed"] = scrape_counts["needed"]
        funnel["scrape_already_ready"] = scrape_counts["already_ready"]
        funnel["dead_dropped"] = len(dead_all)
    _snap("scraped", to_evaluate)

    for idx, jobs in cluster_items:
        r = eval_results[idx]
        final_fresh_judged += len(r["fresh"])
        final_reused_from_cache += r["reused_from_cache"]
        if r["fresh"] and not r["call_failed"]:
            _persist_verdicts(db, profile_id, r["fresh"], r["strong"], r["backup"], r["disqualified"], r["eval_sig"])
        final_strong += len(r["strong"])
        final_backup += len(r["backup"])
        final_disqualified += _hard_dq(r["disqualified"])
        final_reject_reasoned += len(r["disqualified"])

        if r["extras_for_to_evaluate"]:
            # So judged_ids (computed from to_evaluate after this loop, used to
            # mark JobSeen rows "enriched") picks these up same as any other
            # judged job -- otherwise a backfilled-and-judged extra would keep
            # reappearing in next run's "new, unprocessed" pool despite already
            # carrying a persisted verdict.
            to_evaluate.extend(r["extras_for_to_evaluate"])
            final_reused_from_cache += r["backfill_reused_from_cache"]
        if r["backfill_call_succeeded"]:
            _persist_verdicts(db, profile_id, r["extras_fresh"], r["b_strong"], r["b_backup"],
                               r["b_disqualified"], r["eval_sig"])
            final_fresh_judged += len(r["extras_fresh"])
            final_strong += len(r["b_strong"])
            final_backup += len(r["b_backup"])
            final_disqualified += _hard_dq(r["b_disqualified"])
            final_reject_reasoned += len(r["b_disqualified"])

        fallback_notes[idx].update(r["fallback_tags"])

        # Cross-site duplicate-content corroboration for judge-flagged scam_suspect
        # picks (see full_auto.verify_not_duplicated) -- gated to only picks about to
        # be shown this run and a small shared budget, since it spends a real search
        # call per check. A corroborated pick is pulled from output and its
        # persisted verdict overridden to reject so it never resurfaces. Sequential
        # across clusters (not part of the concurrent judging above) since it
        # decrements one shared per-run budget.
        picks = r["picks"]
        if scam_verify_budget[0] > 0:
            had_picks = bool(picks)
            verified_picks = []
            for p in picks:
                if (scam_verify_budget[0] > 0 and p.get("scam_suspect") and p.get("_identity")):
                    scam_verify_budget[0] -= 1
                    dup_reason = engine.verify_not_duplicated(
                        p, eng_profile.get("adzuna_country_code", "gb"))
                    if dup_reason:
                        _persist_scam_override(db, profile_id, p["_identity"], dup_reason, r["eval_sig"])
                        final_scam_verified_dropped += 1
                        emit(f"[pipeline] scam-verify: dropped {p.get('title')} @ "
                             f"{p.get('company')} -- {dup_reason}")
                        continue
                verified_picks.append(p)
            picks = verified_picks
            if had_picks and not picks and r["backup_tier"]:
                # The only strong/cached pick(s) got corroborated as scam and removed
                # -- fall back to this cluster's own backup tier (computed
                # earlier, discarded above because strong was non-empty) rather than
                # silently contributing nothing, same posture as the original
                # empty-picks fallback. Not itself re-verified for scam_suspect --
                # bounds cost/complexity for what should be a rare double-fallback.
                picks = r["backup_tier"]
                fallback_notes[idx].add("eval_fallback")

        final_by_cluster[idx] = picks
        # Judge-side half of this cluster's diagnostics row (the gate stage filled
        # in the other half). A cluster that reaches the judge with a healthy pool
        # and still returns nothing strong is the shape a run-wide funnel can't
        # show -- see the Settings "Search run timings" panel.
        diag = cluster_diagnostics.get(idx)
        if diag is not None:
            diag.update({
                "judged": len(r["fresh"]) + len(r["extras_fresh"]),
                "judge_reused_from_cache": r["reused_from_cache"] + r["backfill_reused_from_cache"],
                "judge_strong": len(r["strong"]) + len(r["b_strong"]),
                "judge_backup": len(r["backup"]) + len(r["b_backup"]),
                "judge_disqualified": _hard_dq(r["disqualified"]) + _hard_dq(r["b_disqualified"]),
                "judge_reject_reasoned": len(r["disqualified"]) + len(r["b_disqualified"]),
                "picks": len(picks),
                "fallbacks": sorted(fallback_notes[idx]),
            })

    funnel["final_fresh_judged"] = final_fresh_judged
    funnel["final_reused_from_cache"] = final_reused_from_cache
    funnel["final_strong"] = final_strong
    funnel["final_backup"] = final_backup
    funnel["final_disqualified"] = final_disqualified
    # Every judged job that didn't make a list should now carry the AI's own reason.
    # Watching this against final_fresh_judged - strong - backup is how a regression
    # in the judge honouring "account for every job_number" stays visible.
    funnel["final_reject_reasoned"] = final_reject_reasoned
    funnel["final_scam_verified_dropped"] = final_scam_verified_dropped

    # Same fair-allocation logic as pooling/top-N: total output stays capped at
    # FINAL_PICKS, redistributed across clusters rather than added per cluster.
    # Allocated one VERDICT GRADE at a time rather than one pass over each
    # cluster's already tier-ordered list -- a single pass takes each cluster's
    # WHOLE share in cluster order, so a cluster with zero strong picks could
    # contribute its backup-tier filler ahead of a later cluster's genuine strong
    # picks. fit_rank (below) is assigned purely by position in `final`, and
    # nothing downstream re-sorts by verdict, so any ordering slip here rides all
    # the way to the UI.
    #
    # This used to split on the `strong_fit` BOOLEAN only (which list the judge
    # put the pick in), which guaranteed strong-before-backup but left the finer
    # fit_level grade -- the very thing the card's badge shows -- doing nothing at
    # all: a live run ranked four "Strong fit" picks above two "Very strong fit"
    # ones purely because they came from the cluster that happened to be first in
    # `final_by_cluster`. Splitting per _VERDICT_GRADES instead makes the badge
    # order and the rank order agree (every Very strong fit above every Strong
    # fit, and so on down), while each grade's own fair-allocate pass still
    # distributes that grade's slots across clusters fairly. Within one cluster's
    # grade bucket the judge's own ordering is preserved.
    graded_by_cluster: dict[str, dict[int, list[dict]]] = {
        grade: {idx: [p for p in picks if _verdict_of(p) == grade]
                for idx, picks in final_by_cluster.items()}
        for grade in _VERDICT_GRADES
    }
    # A pick the judge graded with something we don't recognise (or didn't grade
    # at all -- e.g. an inconclusive-call fallback) still has to land somewhere:
    # keep it behind every graded pick rather than dropping it.
    ungraded_by_cluster = {idx: [p for p in picks if _verdict_of(p) not in _VERDICT_GRADES]
                           for idx, picks in final_by_cluster.items()}
    final: list[dict] = []
    for by_cluster in (*(graded_by_cluster[g] for g in _VERDICT_GRADES), ungraded_by_cluster):
        if len(final) >= engine.FINAL_PICKS:
            break
        final += _fair_allocate(by_cluster, engine.FINAL_PICKS - len(final))
    # One lap, not the old separate "scrape" + "final_eval": phases 5 and 6 now
    # overlap per cluster (see _scrape_then_judge), so there is no longer a
    # wall-clock boundary between them to measure.
    t0 = _lap("scrape+judge", t0)
    funnel["final_picks"] = len(final)
    _snap("final_picks", final)
    # Per-cluster funnel, carried out alongside the stage samples (see _snap's
    # note on `samples` riding inside `funnel`). run_search_task writes it into
    # SearchRun.snapshot_samples, whose payload is free-form JSON -- the Snapshot
    # endpoint iterates a fixed stage list and ignores this key, so it needs no
    # schema change and breaks no existing panel.
    samples["_clusters"] = [cluster_diagnostics[i] for i in sorted(cluster_diagnostics)]
    _progress(db, run, "Writing up top picks…")
    emit(f"[pipeline] final_evaluation returned {len(final)} picks across "
         f"{sum(1 for v in final_by_cluster.values() if v)} cluster(s)"
         + ("" if final else " -- nothing survived evaluation"))
    emit(f"[pipeline] phase timings (s): "
         + ", ".join(f"{phase}={secs}" for phase, secs in timings.items()))

    # Mark 'enriched' only the rows this run actually resolved: those that reached the
    # judge (they now carry a cached verdict), plus off-sector/hard-dropped gate drops
    # (a genuine quality rejection). On-sector gate survivors that never reached the
    # judge -- trimmed by the rank floor or by fair-allocate's judge budget -- stay
    # 'new' so they re-compete and reach the judge on a later run with spare capacity,
    # instead of being frozen after one look. This is what the rank stage's own "free
    # to resurface on a future run" comment promises, but which marking the whole
    # examined batch enriched would silently break. Off-sector/hard-dropped rows are
    # NOT kept 'new' (they'd otherwise re-fill the queue by relevance every run while
    # always failing the same gate axes -- exactly the wildcard rows we suppress).
    # gate_survivor_ids/examined_ids were accumulated per cluster during the gate+rank
    # refill loop above (gate_survivors -- every cluster's <2-soft-axis-failure
    # in-sector candidates across all rounds -- and the exact examined subset of each
    # cluster's queue, respectively).
    judged_ids = {j["_identity"] for j in to_evaluate}
    processed_ids = [
        identity for identity in examined_ids
        if identity in judged_ids or identity not in gate_survivor_ids
    ]
    shown_ids = [f.get("_identity") for f in final if f.get("_identity")]
    warning = _compose_fallback_warning(role_clusters, fallback_notes)
    return final, harsh or bool(fallback_notes), (processed_ids, shown_ids, gate_sig), timings, warning, funnel


# Shared by the startup reaper and the shutdown handler below, so the two halves
# of "this process died with a search in flight" can't drift apart in wording.
RESTART_INTERRUPT_MESSAGE = (
    "Search was interrupted by a server restart. Please run a new search."
)

# Run ids whose run_search_task is still executing in this process. Used only by
# request_shutdown_cancel, to tell "the workers have unwound" from "the settle
# timeout expired". Deliberately not derived from thread names: the task runs on
# anyio's shared BackgroundTasks pool, whose threads are generic workers and
# outlive any one task. set.add/discard are atomic under the GIL, and the only
# read is a truthiness test, so this needs no lock.
_active_search_runs: set[int] = set()


def reap_stale_search_runs(db: Session) -> int:
    """Called once at process startup (see main.py). Any SearchRun still
    status="running" was orphaned by the PREVIOUS process lifetime -- crash,
    `uvicorn --reload` restart, manual kill, etc. -- since run_search_task's
    worker thread died with that process and nothing else will ever revisit
    the row. Left alone it permanently occupies a daily search-cap slot
    and /search/status serves a run stuck at "running" forever. Returns
    the number of rows reaped.

    This is the backstop, not the only path: request_shutdown_cancel() below
    normally reaches these rows first, on the way down. It stays because a hard
    kill (SIGKILL, OOM, host loss) never runs a shutdown handler at all."""
    stale = db.execute(select(SearchRun).where(SearchRun.status == "running")).scalars().all()
    if not stale:
        return 0
    now = datetime.utcnow()
    for run in stale:
        run.status = "error"
        run.message = RESTART_INTERRUPT_MESSAGE
        run.finished_at = now
    db.commit()
    # A run killed after its gate+rank phase left provisional Role rows behind
    # -- nothing else will ever revisit them, so resolve them here too.
    for run in stale:
        _cleanup_provisional_roles(db, run.id)
    return len(stale)


def request_shutdown_cancel(db: Session, settle_timeout: float = 4.0) -> int:
    """Called on process shutdown (see main.py). Marks every in-flight run the
    same way reap_stale_search_runs would at the next boot, but *before* the
    process goes away -- the point being the `cancel_requested` flag, which the
    pipeline's worker threads poll (_make_cancel_check, one SELECT per ~3s).
    Setting it makes them raise SearchCancelled at their next checkpoint and
    unwind through the normal cancel path instead of being killed mid-write.

    Why it matters on a container host: the search worker holds the SQLite file
    open on a mounted volume, so a worker still writing when the supervisor
    tears the machine down leaves the volume busy and the database unmounted
    uncleanly (observed on Fly as repeated `error umounting /data: EBUSY`
    followed by `recovering journal` on the next boot). Journal recovery is
    doing its job there, but it's a crash-consistency path being exercised on
    every single restart, which is not a thing to rely on routinely.

    Waits up to `settle_timeout` seconds for the workers to notice. Sized
    against the 3s cancel-check interval and kept well inside the host's
    kill grace period -- this runs while the supervisor is already counting
    down to SIGKILL, so it must never block indefinitely.

    Returns the number of runs marked."""
    running = db.execute(select(SearchRun).where(SearchRun.status == "running")).scalars().all()
    if not running:
        return 0
    now = datetime.utcnow()
    for run in running:
        # cancel_requested is what the worker threads actually poll; status is
        # set here too so /search/status is already truthful the moment the
        # process comes back, rather than depending on the startup reaper.
        run.cancel_requested = True
        run.status = "error"
        run.message = RESTART_INTERRUPT_MESSAGE
        run.finished_at = now
    db.commit()

    # Give the workers a moment to observe the flag and stop writing. They clean
    # up their own provisional rows on the way out (the SearchCancelled path in
    # run_search_task); the startup reaper covers whatever didn't make it.
    deadline = time.monotonic() + settle_timeout
    while _active_search_runs and time.monotonic() < deadline:
        time.sleep(0.25)
    return len(running)


def _prune_previous_roles(db: Session, profile_id: int) -> None:
    """Second-search semantics: only 'crossed' roles age out to deleted, so the
    "passed this session" list resets on each fresh run. 'new' (inbox) roles are
    never auto-pruned -- an unreviewed role must persist until the user acts on
    it (tick/cross/delete), not disappear just because another search ran.
    saved/applied are left untouched either way."""
    db.query(Role).filter(
        Role.profile_id == profile_id, Role.status == "crossed"
    ).update({Role.status: "deleted"}, synchronize_session=False)
    db.commit()


def _expire_stale_roles(db: Session, profile_id: int) -> None:
    """Nothing else ever revisits an already-shown 'new' role to check
    whether the listing has since closed (Phase 5 only scrapes a given job
    once -- see _persist_dead_scrapes for the other, signal-based half of
    this). As a cheap fallback for the common case where the posting simply
    goes stale without ever being re-scraped, auto-move 'new' roles older
    than ROLE_STALE_DAYS to 'ignored' -- reversible via the Ignored tab's
    re-save, unlike 'deleted'."""
    cutoff = datetime.utcnow() - timedelta(days=ROLE_STALE_DAYS)
    db.query(Role).filter(
        Role.profile_id == profile_id, Role.status == "new", Role.created_at < cutoff,
    ).update({Role.status: "ignored"}, synchronize_session=False)
    db.commit()


def _already_decided_ids(db: Session, profile_id: int, identities: list[str]) -> set[str]:
    """External ids among `identities` that already have a saved/applied Role for
    this profile from an earlier run. A Keep/Save is a completed decision (see
    the Role lifecycle notes in CLAUDE.md) -- these must never get a second,
    duplicate 'new' Role persisted alongside the existing one just because the
    same job resurfaced in a later run's candidate pool."""
    identities = [i for i in identities if i]
    if not identities:
        return set()
    return {
        row[0] for row in db.execute(
            select(Role.external_id).where(
                Role.profile_id == profile_id,
                Role.external_id.in_(identities),
                Role.status.in_(("saved", "applied")),
            )
        ).all()
    }


# Cross-run family suppression (the GRAYCE case). _already_decided_ids above
# matches an exact identity_hash, so the SAME grad scheme advertised in a second
# city -- different URL, therefore different hash -- sailed straight past it and
# was surfaced again after the user had already applied. Discovery deliberately
# keeps those as separate JobSeen rows (_find_soft_duplicate REQUIRES a shared
# location token), and _suppress_judge_duplicates only ever sees one run, so
# nothing anywhere connected the two.
#
# Scope is deliberately narrow, on one criterion: suppress only on states that
# are explicit, visible, user-initiated and individually reversible from
# /my-roles. That admits 'saved' and 'applied' and excludes:
#   * 'crossed'  -- _prune_previous_roles flips these to 'deleted' at the start of
#                   every run, so cross-run there is nothing left to match anyway;
#                   and "a cross resets each session" is a deliberate property,
#                   which this would silently convert into a permanent blocklist.
#   * 'ignored'  -- written automatically by _expire_stale_roles after
#                   ROLE_STALE_DAYS with NO user involvement, so including it
#                   would let the mere passage of time blocklist a whole family.
#   * 'deleted'  -- terminal state of a cross and of a leftover-provisional reap.
#
# Nothing is persisted by the suppression itself: the JobSeen row keeps its
# state, rank score and (absent) verdict, so un-saving the role brings the family
# straight back on the next run.
_DECIDED_FAMILY_STATUSES = ("saved", "applied")

# Per-family cap, so a template employer advertising one title across 40 cities
# (a real shape in the live store) cannot have an unbounded number of rows
# suppressed by a single decision without it being obvious in the console.
DECIDED_FAMILY_SUPPRESS_MAX = 3

# Shadow mode. False = compute the keys and count what WOULD be suppressed, but
# suppress nothing. Lets one ordinary run report the real blast radius, by name,
# before any result is withheld from the user. Flipped to True after a shadow
# run (2026-08-03, run 15) confirmed the matcher against real data -- it
# correctly keyed "Pimlico Enterprises / Graduate Data Analyst" across its
# Bolton/Doncaster/GB reposts to the saved Bolton copy's family -- and reported
# 0 suppressions only because none of that run's live candidates happened to be
# in a decided family (see funnel_counts.decided_family_*).
DECIDED_FAMILY_SUPPRESS_ENABLED = True


def _family_key(company: str, title: str, location: str = "") -> tuple[str, str] | None:
    """(normalized company, normalized title family), or None when there is no
    company to anchor on. A blank company is excluded for the same reason
    _same_vacancy excludes it: aggregator rows arrive company-less and would
    collapse unrelated postings that merely share a common title."""
    comp = _norm_company(company or "")
    if not comp:
        return None
    key = _norm_title_key(title or "", location or "")
    return (comp, key) if key else None


def _decided_role_keys(db: Session, profile_id: int) -> set[tuple[str, str]]:
    """Family keys for roles this profile has already saved or applied to. Cheap
    (a live profile holds tens of roles), computed once per run and threaded
    through rather than re-queried per call site."""
    rows = db.execute(
        select(Role.company, Role.title, Role.location).where(
            Role.profile_id == profile_id,
            Role.status.in_(_DECIDED_FAMILY_STATUSES),
        )
    ).all()
    keys = {_family_key(c, t, l) for c, t, l in rows}
    keys.discard(None)
    return keys  # type: ignore[return-value]


def _top_n_across_clusters(cluster_accum: dict[int, list[dict]], n: int, key=None) -> list[dict]:
    """Flattens every cluster's currently-known snapshot and returns the top `n`
    by `key` (default _selection_score). A simple global sort, NOT fairness-
    balanced across clusters the way _fair_allocate is -- that's only possible
    once every cluster's gate+rank has finished. A cluster with generally
    higher-scoring candidates can dominate this interim view early in a run;
    accepted tradeoff for responsiveness, corrected once _reconcile_provisional_roles
    runs on the real fair-allocated pool.

    The embedding-stage paint passes an embed_score key: nothing has a rank score
    that early, and the default would fall back to a flat 50.0 whose only
    remaining variation is _selection_score's rich-text bonus -- i.e. it would
    order the very first cards the user sees by which ones happen to carry a long
    snippet, not by how well they match."""
    everything = [j for jl in cluster_accum.values() for j in jl]
    everything.sort(key=key or _selection_score, reverse=True)
    # Dedup the interim view with the same key the judge pool uses
    # (_suppress_judge_duplicates), keeping the highest-ranked copy -- otherwise
    # aggregator reposts (blank-company careerjet/jobviewtrack listings) show as
    # several identical "Verifying..." cards until finalization finally collapses
    # them. Keep the top-ranked copy of each; a job with no reliable key
    # (key is None) is always kept.
    out: list[dict] = []
    seen: set[tuple] = set()
    for j in everything:
        key = _dup_key(j)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        out.append(j)
        if len(out) >= n:
            break
    return out


def _upsert_provisional_rows(db: Session, profile_id: int, run: SearchRun,
                              top: list[dict], engine, *, stage: str = "rank") -> tuple[int, int]:
    """Insert-or-update this run's provisional Role rows to mirror `top`
    (already sorted/capped by the caller) -- never deletes. Interim calls
    during gate+rank only ever add or refresh rows so a row's fit_rank can
    shift as better candidates arrive; only _reconcile_provisional_roles,
    once the real fair-allocated pool is known, resolves genuinely-gone
    leftovers. Safe to call repeatedly with a growing/reshuffling `top`
    across the run -- see the call sites in _run_engine_pipeline.

    `stage` ("embed" | "rank") records which progressive-paint section the row
    belongs to. A row already at a LATER stage is never demoted back: the
    embed-stage paint runs once, up front, over candidates that mostly go on to
    be examined, so re-writing stage="embed" onto a row the gate has since
    promoted would bounce it back up the page. Promotion embed -> rank happens
    here, in place (same row id, status untouched), which is what makes a job
    move between sections instead of appearing in both -- the de-duplication the
    three-stage view depends on falls out of the upsert rather than needing its
    own pass."""
    existing = {
        r.external_id: r
        for r in db.query(Role).filter(Role.search_run_id == run.id, Role.provisional.is_(True))
        if r.external_id
    }
    # _already_decided_ids exists to stop a SECOND, duplicate Role being created
    # for a job the candidate already saved/applied. It must not also block
    # UPDATES to a row this run already owns: its query isn't scoped to earlier
    # runs, so a card the user Kept mid-run matches it too, and skipping that row
    # froze it at whatever stage/rank it was first painted at. Latent before the
    # three-stage paint (a stale fit_rank), load-bearing now -- a Kept
    # embedding-stage card would never be promoted out of the "not yet reviewed"
    # section even once the gate and rank stage had cleared it.
    decided = _already_decided_ids(db, profile_id, [j.get("_identity") for j in top])
    # Family-level twin of the same rule, so a different-city copy of an
    # already-decided role doesn't flash as a "Verifying..." card for the whole
    # run before vanishing at finalization. Same `in existing` escape for the same
    # reason: a row this run already owns must keep being UPDATED.
    decided_fams = (_decided_role_keys(db, profile_id)
                    if DECIDED_FAMILY_SUPPRESS_ENABLED else set())
    top = [
        j for j in top
        if j.get("_identity") in existing
        or (j.get("_identity") not in decided
            and _family_key(j.get("company", ""), j.get("title", ""),
                            j.get("location", "")) not in decided_fams)
    ]
    matched = inserted = 0
    for pos, j in enumerate(top, start=1):
        ident = j.get("_identity") or _external_id(engine, j)
        fields = dict(
            title=j.get("title", "Untitled role"),
            company=j.get("company"),
            location=j.get("location"),
            url=j.get("url"),
            salary_text=_salary_text(j),
            source=j.get("board"),
            fit_rank=pos,
            provisional_stage=stage,
        )
        # No rank_score at the embedding stage -- nothing has scored this job yet,
        # and writing the 50.0 default would render a fabricated "Fit estimate
        # 50/100" chip on a card whose whole point is that no AI has seen it.
        if stage != "embed":
            fields["rank_score"] = int(round(j.get("_rank_score", 50.0)))
        row = existing.get(ident)
        if row is not None:
            if stage == "embed" and row.provisional_stage in _PROVISIONAL_STAGES_AFTER_EMBED:
                continue
            for k, v in fields.items():
                setattr(row, k, v)   # upgrade in place: same row id, status untouched
            matched += 1
        else:
            db.add(Role(profile_id=profile_id, search_run_id=run.id, external_id=ident,
                        provisional=True, status="new", **fields))
            inserted += 1
    db.commit()
    return matched, inserted


def _reconcile_provisional_roles(db: Session, profile_id: int, run: SearchRun,
                                  selected: list[dict], engine) -> tuple[int, int, int]:
    """Final provisional reconcile, once every cluster's gate+rank has
    finished and _fair_allocate has produced the real, fair, cross-cluster
    `selected` pool (~JUDGE_POOL candidates). Some/all of this run's
    provisional rows may already exist -- interim _upsert_provisional_rows
    calls fired as each gate/rank round returned (see _run_engine_pipeline) --
    so this upserts/reaps rather than blind-inserting.

    `selected` is the full judge pool; `top` here is only the top
    PROVISIONAL_MAX of it for *display* -- the rest are still headed to
    scrape+judge this run. A row a user Kept mid-run that's ranked outside
    the top-PROVISIONAL_MAX but still in `selected` must be left
    provisional=True and untouched: resolving it here would permanently file
    it before the judge ever actually saw it, and finalization's own later
    pass (which only looks at provisional=True rows) would never find it
    again to upgrade it with the real verdict. Only a row whose job has
    genuinely fallen out of `selected` entirely -- deduped by
    _suppress_judge_duplicates, or cut by _fair_allocate's cross-cluster
    budget -- gets resolved (retained-if-kept-or-applied / removed) here."""
    selected_ids = {j.get("_identity") for j in selected}
    top = sorted(selected, key=_selection_score, reverse=True)[:PROVISIONAL_MAX]
    matched, inserted = _upsert_provisional_rows(db, profile_id, run, top, engine)

    still_provisional = {
        r.external_id: r
        for r in db.query(Role).filter(Role.search_run_id == run.id, Role.provisional.is_(True))
        if r.external_id
    }
    top_ids = {j.get("_identity") for j in top}
    removed = 0
    for ident, row in still_provisional.items():
        if ident in top_ids:
            continue
        # Embedding-stage rows are NOT reaped here. This runs at the end of
        # gate+rank, which is exactly the point where the embedding section is
        # supposed to still be on screen underneath the rank-stage results (paint
        # 2 of 3) -- resolving them would blank the bottom of the page mid-run.
        # They are resolved at finalization like any other leftover.
        if row.provisional_stage == PROVISIONAL_STAGE_EMBED:
            continue
        if ident not in selected_ids:
            _resolve_leftover_provisional(db, row)
            removed += 1
    db.commit()
    return matched, inserted, removed


# Honest markers for a provisional row the user chose to Keep but that the
# final judge didn't put in this run's picks. Rendered by RoleCard's notes
# bucket (a leading ⚠ line before any headline). Keyed by the job's stored
# JobSeen.eval_verdict at finalization time. Worded for a role that may now
# be back in the Inbox (see _resolve_leftover_provisional's routing below),
# not just a still-"saved" one.
_RETAINED_MARKER_BY_VERDICT = {
    "reject": "⚠ You kept this during review, but the full AI review later rated it below the bar — take another look.",
    "strong": "⚠ Verified as a reasonable match, but it didn't make this run's final cut.",
    "backup": "⚠ Verified as a reasonable match, but it didn't make this run's final cut.",
}
_RETAINED_MARKER_UNJUDGED = (
    "⚠ You kept this during review, but the AI couldn't complete its full review of this role.")
_RETAINED_MARKER_INTERRUPTED = (
    "⚠ You kept this during review, but the search ended before the AI finished verifying this role.")


# How many rank-stage leftovers stay on screen after the run finishes, under
# their own "quick-scored only" heading (paint 3 of 3). Ranked by the cheap
# stage's own estimate, so these are the best of what the expensive judge ran out
# of budget to look at.
UNREVIEWED_RETAIN_MAX = 8
_UNREVIEWED_MARKER = (
    "⚠ Only quick-scored — the full AI review ran out of room before reaching this one, "
    "so there's no detailed verdict here yet.")


def _retain_unreviewed_provisional(db: Session, row: Role) -> None:
    """Keep a rank-stage leftover on screen after the run, as an honestly-labelled
    "we didn't get to this" card rather than deleting it.

    Stays `provisional_stage="rank"` while `provisional` goes False: that pair is
    what /search buckets on to render the trailing section, and it keeps the
    persisted rank_score renderable. fit_rank is cleared so the row sorts after
    every real pick (NULLS LAST), same as a retained Keep. Caller commits."""
    row.provisional = False
    row.fit_rank = None
    row.ai_analysis = (_UNREVIEWED_MARKER if not row.ai_analysis
                       else f"{_UNREVIEWED_MARKER}\n{row.ai_analysis}")


_INTERRUPTED_LEFTOVER_MARKER = (
    "⚠ The search was stopped before this role could be reviewed further — showing "
    "what was found so far.")


def _retain_interrupted_provisional(db: Session, row: Role) -> None:
    """Pin an untouched ('new') provisional row on screen after its run was
    cancelled, crashed, or orphaned by a server restart, instead of deleting it
    -- see _cleanup_provisional_roles. Whichever stage it was painted at
    ('embed' or 'rank') is left as-is: RoleCard's corner label already renders
    both correctly regardless of the `provisional` flag, so no relabelling is
    needed here, just the honest "search stopped" note. fit_rank is cleared so
    it sorts after any later run's real picks (NULLS LAST), same convention as
    _retain_unreviewed_provisional. Caller commits."""
    row.provisional = False
    row.fit_rank = None
    row.ai_analysis = (_INTERRUPTED_LEFTOVER_MARKER if not row.ai_analysis
                       else f"{_INTERRUPTED_LEFTOVER_MARKER}\n{row.ai_analysis}")


def _resolve_leftover_provisional(db: Session, row: Role, marker: str | None = None) -> None:
    """Apply the leftover rules to ONE provisional row that did NOT land in the
    final picks (or whose run ended early). Never-acted 'new' rows are
    hard-deleted (no FeedbackLog referent exists); crossed/deleted rows
    soft-delete so their feedback audit rows keep a valid referent (same
    posture as routers/search.delete_role).

    'applied' rows are retained as applied unconditionally -- a real action
    already taken, never reverted by a later AI opinion. 'saved' (Keep during
    verification) rows are retained too, but a Keep is a tentative preference,
    not a firm decision the way an ordinary Save is -- if the full judge
    rated it strong/backup (a reasonable match, just short of this run's
    numeric FINAL_PICKS cut), that's the judge agreeing, so it stays saved;
    otherwise (rejected outright, never got a verdict at all, or the run was
    interrupted before judging) it goes back to the Inbox (status="new") for
    a real decision, with the ⚠ marker carried over so the context isn't
    lost. Caller commits."""
    if row.status in ("saved", "applied"):
        job = db.execute(
            select(JobSeen).where(
                JobSeen.profile_id == row.profile_id,
                JobSeen.identity_hash == (row.external_id or ""),
            )
        ).scalar_one_or_none()
        verdict = job.eval_verdict if job else None
        interrupted = marker is not None  # caller forced a marker: cancel/failure/restart path
        if marker is None:
            marker = _RETAINED_MARKER_BY_VERDICT.get(verdict or "", _RETAINED_MARKER_UNJUDGED)
        # Only `final` entries get JobSeen.state bumped to "shown" (see
        # run_search_task) -- a provisional row the judge never picked otherwise
        # stays "enriched", which _backlog_rows treats as fair game to resurface
        # on a later run. A kept/applied row is a permanent decision, same as a
        # Role saved from `final`, so it needs the same protection regardless of
        # which status it ends up with below, or a later run can persist a
        # second, duplicate Role for the exact job already handled here (see
        # _already_decided_ids for the belt-and-braces guard at the two
        # Role-creation sites).
        if job is not None and job.state != "shown":
            job.state = "shown"
        row.provisional = False
        # A retained Keep/Applied carries its own ⚠ marker and is a user decision,
        # not an interim display bucket -- clear the stage so /search renders it as
        # an ordinary row rather than filing it under "quick-scored only".
        row.provisional_stage = None
        row.ai_analysis = marker if not row.ai_analysis else f"{marker}\n{row.ai_analysis}"
        if row.status == "saved" and (interrupted or verdict not in ("strong", "backup")):
            row.status = "new"
        row.fit_rank = None  # sorts after the ranked picks (NULLS LAST); no rank context applies here
    elif row.status == "new":
        db.delete(row)
    else:  # crossed (possibly already flipped to deleted by _prune_previous_roles)
        row.status = "deleted"
        row.provisional = False
        row.provisional_stage = None


def _cleanup_provisional_roles(db: Session, run_id: int) -> None:
    """Resolve the provisional rows of a run that ended without finalizing
    (cancel, failure, server restart).

    An untouched 'new' row -- whatever the run had managed to paint on screen,
    embed- or rank-stage -- is now PINNED as a non-provisional leftover
    (_retain_interrupted_provisional) rather than deleted: the whole point of
    the progressive paint is to avoid a blank screen, and wiping it the moment
    a run is interrupted defeated that. It stays visible, honestly marked, until
    the next search run's own results replace it (same "an unreviewed role
    persists until the user acts on it" precedent as _prune_previous_roles).
    A row the user explicitly acted on (saved/applied) keeps the pre-existing
    interrupted-marker retain path; 'crossed' still soft-deletes -- the user
    already dismissed it."""
    rows = db.query(Role).filter(
        Role.search_run_id == run_id, Role.provisional.is_(True)
    ).all()
    for row in rows:
        if row.status == "new":
            _retain_interrupted_provisional(db, row)
        else:
            _resolve_leftover_provisional(db, row, marker=_RETAINED_MARKER_INTERRUPTED)
    if rows:
        db.commit()


def _safe_print(msg: str) -> None:
    """Mirrors full_auto.emit()'s fallback: some consoles (cp1252, seen on this
    repo's own venv under some Windows launch paths) can't encode the
    box-drawing dashes these pipeline logs use, and raise UnicodeEncodeError.
    That's fatal for the FIRST call below -- it happens before
    run_search_task's own try/except is entered, so an uncaught crash there
    permanently orphans the SearchRun at status="running" with nothing ever
    marking it "error"."""
    try:
        print(msg)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "ascii"
        print(msg.encode(enc, errors="backslashreplace").decode(enc, errors="replace"))


def run_search_task(profile_id: int, run_id: int) -> None:
    """Background entry point. Owns its own DB session (runs off-request).
    Progress is logged with print()/emit(), which lands in the same console
    that's running `uvicorn app.main:app` (the backend terminal/window)."""
    db = SessionLocal()
    run = db.get(SearchRun, run_id)
    _active_search_runs.add(run_id)  # see request_shutdown_cancel
    _safe_print(f"\n[pipeline] ── search run {run_id} for profile {profile_id} starting ──")
    try:
        import full_auto as engine  # lazy: pulls in crawl4ai only now
        engine.init_db()  # ensures gate_cache/jobs/profile_cache tables exist
        # Per-run token accounting (see full_auto.llm / _record_llm_usage). Reset
        # here rather than in the pipeline so the profile-intel calls above are
        # excluded -- those are cached and usually don't fire, and folding them in
        # would make the per-stage hit rates read differently on a run that
        # happened to regenerate intel.
        engine.reset_llm_usage()

        # Cached: only actually calls the LLM when the profile's inputs changed
        # since the last run (or never ran). regenerate_roles=False: this
        # automatic top-up must only ever refresh the header/intent-draft, never
        # the target_role rows themselves -- letting it also do so used to wipe
        # every role family's roles (and could reintroduce a just-deleted
        # cluster) as a side effect of any unrelated signature change, e.g.
        # deleting one family. See profile_intel.ensure_profile_intel.
        ensure_profile_intel(db, profile_id, regenerate_roles=False)
        # Belt-and-braces: slot any genuinely-ungrouped role into an EXISTING
        # family (a cheap no-op in the normal case now that the call above never
        # creates ungrouped roles) rather than leaving snapshot._role_groups to
        # fall back to its own from-scratch, family-unaware clustering.
        ensure_families(db, profile_id)

        snap = build_snapshot(db, profile_id)

        # Engine's expensive-AI step reads the CV from a file; hand it our synthesis.
        with open(engine.CV_PATH, "w", encoding="utf-8") as f:
            f.write(snap["cv_text"])

        final, harsh, marks, timings, warning, funnel = asyncio.run(
            _run_engine_pipeline(
                engine, snap["engine_profile"], snap["weighted_text"], snap["cv_text_base"],
                db, profile_id, run
            )
        )

        # A checkpoint inside the pipeline may not have caught a cancel that
        # landed after the last one ran (e.g. mid-scrape, or between the
        # pipeline's return and this line). Re-check right before persisting
        # anything, so a late-finishing run can't clobber the "cancelled"
        # status the endpoint already set, or dump results the user no longer
        # expects to see.
        db.refresh(run)
        if run.cancel_requested:
            _cleanup_provisional_roles(db, run_id)
            _safe_print(f"[pipeline] ── search run {run_id} cancelled (caught before persisting results) ──\n")
            return

        # Prune only after the pipeline has succeeded, so a failed run leaves the
        # previous "crossed" roles intact instead of wiping them with nothing
        # to replace them. (Prune may flip a mid-run-crossed provisional row to
        # 'deleted' -- fine: it lands in the leftover bucket below either way.)
        _prune_previous_roles(db, profile_id)
        _expire_stale_roles(db, profile_id)

        # This run's provisional rows (persisted after gate+rank for early
        # display): each is upgraded in place by the matching final pick, or
        # resolved by the leftover rules (retain if the user kept it, else
        # remove). Matching by external_id, scoped to this run.
        provisional_by_id = {
            r.external_id: r
            for r in db.query(Role).filter(
                Role.search_run_id == run.id, Role.provisional.is_(True)
            )
            if r.external_id
        }

        # A job the candidate already saved/applied to in an earlier run can
        # still legitimately reach `final` again (e.g. a backlog-resurfaced or
        # requeued JobSeen row the judge re-confirms) -- see _already_decided_ids.
        # Belt-and-braces alongside the JobSeen "shown" fix in
        # _resolve_leftover_provisional: don't persist a second, duplicate 'new'
        # Role for it.
        decided_ids = _already_decided_ids(
            db, profile_id,
            [entry.get("_identity") or _external_id(engine, entry) for entry in final],
        )
        # Family-level guard. NOT merely belt-and-braces: a job whose stored
        # verdict is still valid under the current eval_signature is served from
        # cache (see cached_strong in _run_cluster_final_eval) and therefore never
        # passes through _suppress_judge_duplicates at all, so this is the only
        # site that catches a cache-served member of an already-decided family.
        decided_fams = (_decided_role_keys(db, profile_id)
                        if DECIDED_FAMILY_SUPPRESS_ENABLED else set())
        n_fam_persist = 0

        for rank, entry in enumerate(final, start=1):
            ident = entry.get("_identity") or _external_id(engine, entry)
            fields = dict(
                title=entry.get("title", "Untitled role"),
                company=entry.get("company"),
                location=entry.get("location"),
                url=entry.get("url"),
                tags=_derive_tags(entry, snap["skills"], snap["seniority_label"]),
                # The judge read the salary off the full JD; the regex only ever
                # saw whatever text was to hand. Prefer the judge, fall back to
                # the regex for a pick it had nothing to say about.
                salary_text=(entry.get("role_salary") or "").strip() or _salary_text(entry),
                source=entry.get("board"),
                fit_rank=rank,
                ai_analysis=_compose_analysis(entry),
                verdict=_verdict_of(entry),
                work_style=(entry.get("work_style") or "").strip() or None,
                seniority_level=(entry.get("role_seniority") or "").strip() or None,
                deadline_text=(entry.get("deadline") or "").strip() or None,
            )
            row = provisional_by_id.pop(ident, None)
            if row is not None:
                # Upgrade in place: same row id, so the frontend card swaps
                # content rather than remounting. Deliberately does NOT touch
                # `status` -- a mid-run save/cross must survive the upgrade.
                for k, v in fields.items():
                    setattr(row, k, v)
                row.provisional = False
                # A real judged pick belongs to no interim stage. Must be cleared
                # explicitly: the row may have been painted at the embedding or
                # rank stage, and a stale value here would file a genuine pick
                # into the trailing "quick-scored only" section on /search.
                row.provisional_stage = None
            elif ident in decided_ids:
                pass  # already saved/applied from an earlier run -- see decided_ids.
            elif _family_key(entry.get("company", ""), entry.get("title", ""),
                             entry.get("location", "")) in decided_fams:
                # Same role family as something already saved/applied, reached via
                # the cached-verdict path. See decided_fams above.
                n_fam_persist += 1
            else:
                db.add(Role(
                    profile_id=profile_id,
                    search_run_id=run.id,
                    external_id=ident,
                    status="new",
                    **fields,
                ))
        if n_fam_persist:
            _safe_print(f"[pipeline] skipped persisting {n_fam_persist} pick(s) in a role "
                        f"family already saved/applied to")

        # Provisional rows the judge did NOT pick. Three outcomes, in priority
        # order, all riding the same commit as the picks above so the whole
        # transition is atomic with status="done":
        #
        #  1. saved/applied  -> _resolve_leftover_provisional (unchanged). A real
        #     user action always outranks the display rules below.
        #  2. rank-stage, top UNREVIEWED_RETAIN_MAX by rank_score, and NOT
        #     explicitly rejected by the judge -> retained on screen under the
        #     "quick-scored only" heading (paint 3 of 3).
        #  3. everything else (embedding-stage leftovers, and rank-stage rows
        #     past the retain cap) -> _resolve_leftover_provisional, i.e. deleted
        #     if never acted on.
        #
        # The judge-rejected exclusion in (2) is deliberate and is the one place
        # the three-stage display can't be purely additive: a job the judge
        # actually looked at and rejected must not come back as an "unreviewed"
        # card, both because the label would be a lie and because "a job with a
        # stored reject verdict under the current signature is never resurfaced"
        # is an invariant the rest of the pipeline (both judge fallbacks, the
        # backlog top-up) already enforces.
        leftovers = list(provisional_by_id.values())
        rejected_ids = {
            r.identity_hash for r in db.execute(
                select(JobSeen).where(
                    JobSeen.profile_id == profile_id,
                    JobSeen.identity_hash.in_([r.external_id for r in leftovers if r.external_id]),
                    JobSeen.eval_verdict == "reject",
                )
            ).scalars().all()
        }
        retainable = sorted(
            (r for r in leftovers
             if r.status not in ("saved", "applied")
             and r.provisional_stage == PROVISIONAL_STAGE_RANK
             and (r.external_id or "") not in rejected_ids),
            key=lambda r: (r.rank_score if r.rank_score is not None else 0),
            reverse=True,
        )[:UNREVIEWED_RETAIN_MAX]
        retain_ids = {id(r) for r in retainable}
        for row in leftovers:
            if row.status not in ("saved", "applied") and id(row) in retain_ids:
                _retain_unreviewed_provisional(db, row)
            else:
                _resolve_leftover_provisional(db, row)

        if marks:
            processed_ids, shown_ids, gate_sig = marks
            # gate_sig stamps WHICH profile this retirement was decided under, so a
            # later profile edit re-opens exactly the rows it invalidated (see
            # _gate_reopened_rows). "shown" carries none: it isn't a gate verdict.
            _mark(db, profile_id, processed_ids, "enriched", gate_sig)  # everything we evaluated
            _mark(db, profile_id, shown_ids, "shown")         # the up-to-10 displayed

        run.result_count = len(final)
        run.status = "done"
        run.finished_at = datetime.utcnow()
        run.phase_timings = json.dumps(timings)
        # Samples ride inside `funnel` purely to keep the pipeline's return arity
        # (and its early returns) unchanged -- split back out here so
        # funnel_counts stays ints/bools only, as get_run_funnel expects.
        run.snapshot_samples = json.dumps(funnel.pop("samples", {}))
        # Token accounting, flattened into funnel_counts' ints-only contract as
        # tokens_{stage}_{prompt,cached,completion,calls}. Kept here rather than in
        # the pipeline because the judge's backfill retry and the scam-verify call
        # both land after _run_engine_pipeline builds `funnel`, and a rollup that
        # misses those would understate the run's most expensive stage.
        for stage, row in engine.emit_llm_usage_summary().items():
            for k in ("calls", "prompt_tokens", "cached_tokens", "completion_tokens"):
                funnel[f"tokens_{stage}_{k}"] = int(row.get(k, 0))
        run.funnel_counts = json.dumps(funnel)
        if warning:
            run.warning = warning
        run.message = (
            "No new roles found. Try widening your profile or location." if not final else None
        )
        db.commit()
        _safe_print(f"[pipeline] ── search run {run_id} done: {len(final)} results "
                    f"(harsh={harsh}) ──\n")
    except SearchCancelled:
        # The cancel endpoint already set status="cancelled"/finished_at/message
        # on its own session -- don't touch `run` here, just stop cleanly.
        db.rollback()
        _cleanup_provisional_roles(db, run_id)
        _safe_print(f"[pipeline] ── search run {run_id} cancelled mid-run ──\n")
    except Exception as e:  # never let the worker thread die silently
        import traceback
        traceback.print_exc()  # full stack trace to the backend console
        db.rollback()
        _cleanup_provisional_roles(db, run_id)
        if run:
            run.status = "error"
            run.message = f"Search failed: {e!r}" if str(e) else f"Search failed: {type(e).__name__} (see backend console for traceback)"
            run.finished_at = datetime.utcnow()
            db.commit()
        _safe_print(f"[pipeline] ── search run {run_id} FAILED, see traceback above ──\n")
    finally:
        _active_search_runs.discard(run_id)
        db.close()
