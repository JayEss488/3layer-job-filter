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
import hashlib
import json
import random
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from ..config import CATEGORY_EXPAND_ENABLED, DISCOVERY_ATS_CACHE_TTL_HOURS, ROLE_STALE_DAYS
from ..database import SessionLocal
from ..models import Role, SearchRun, JobSeen
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
# SINGLE_CLUSTER_EXAMINE_CAP/MULTI_CLUSTER_EXAMINE_CAP below) has been examined
# -- unlike a one-shot capped batch, this keeps pulling from the idle
# above-RELEVANCE_FLOOR pool instead of leaving hundreds of unexamined,
# fair-scoring candidates on the table every run. Survivors -> expensive
# full-text judge -> engine.FINAL_PICKS capped final results.
TARGET_POOL       = 90     # gate-survivor checkpoint per cluster per refill round
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
# Cheap numeric-ranking stage (rank_gate), between the sector/seniority gate and
# the expensive full-text judge: an extra cheap-model pass that scores gate
# survivors 0-100 on fit instead of a boolean pass/fail, so the expensive judge
# only ever sees a curated top slice instead of every gate survivor.
JUDGE_POOL = 40               # top-ranked candidates sent on to scrape + judge
# Per-cluster ceiling on how many candidates screen_gate/rank_gate examine in one
# run (see _gate_rank_refill_cluster's judge_target/examine_cap params below).
# Replaces a flat cap applied per cluster regardless of cluster
# count, which let every cluster in a multi-stream profile independently grind
# toward the FULL JUDGE_POOL target each -- a live 2-cluster run produced
# "450 examined -> 282 gate survivors -> 203 judge-eligible -> top-40 sent to
# full evaluation" this way, since only JUDGE_POOL=40 total is ever used by the
# final cross-cluster fair-allocate regardless of how many more each cluster
# found. Smaller when there's more than one cluster, since each only needs its
# fair share (JUDGE_POOL / active cluster count) rather than the full 40.
SINGLE_CLUSTER_EXAMINE_CAP = 80
MULTI_CLUSTER_EXAMINE_CAP = 40
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
RANK_REJECT_SCORE_FLOOR = 55
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


def _external_id(engine, job: dict) -> str:
    return engine.make_job_id(job.get("board", ""), job.get("url", ""))


# Adzuna's /jobs/land/ad/... click-tracking redirect resolves 200 OK but is just a
# "you're being redirected" interstitial, not the posting -- full_auto's
# _looks_like_redirect_stub already catches this by content, but only after paying
# for a full fetch (with retries). The URL pattern itself is a free, pre-fetch tell.
_KNOWN_DEAD_END_URL_RE = re.compile(r"/jobs/land/ad/")


def _needs_full_scrape(job: dict) -> bool:
    """Whether phase 5 should bother reading this job's real page before final
    evaluation. ATS-sourced snippets (greenhouse/lever/ashby/workable/
    recruitee/personio) already carry the full posting description -- they
    never need it. Everything else (Reed/Adzuna/Google Jobs/etc.) only needs
    it when its snippet is too short to judge seniority/requirements from,
    which is the actual cost driver: most of a run's full-page fetches (and
    the anti-bot blocking they trigger) buy nothing over what the API already
    handed us."""
    if job.get("_has_full_text"):
        return False  # a real page was scraped and persisted on a prior run
    if canonical_key(job.get("board")) in ATS_KEYS:
        return False
    if _KNOWN_DEAD_END_URL_RE.search(job.get("url") or ""):
        return False  # known-dead redirect stub -- skip straight to snippet fallback
    return len((job.get("snippet") or "").strip()) < SNIPPET_SUFFICIENT_CHARS


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

    Always visible (no marker, or the always-rendered `§role-type`): the
    cluster-label/closest-match notes, the plain-language `summary` headline,
    and `role_type` classifying the day-to-day work. Behind the "Show more"
    toggle: `§qualification` (a direct qualified-or-not verdict, then a
    concern count and its bullets) and `§ai-reasoning` (one synthesized
    narrative paragraph).

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

    if entry.get("summary"):
        parts.append(entry["summary"].strip())

    if entry.get("role_type"):
        parts.append("§role-type")
        parts.append(entry["role_type"].strip())

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

    if entry.get("top_match_reason"):
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
            )
            db.add(row)
            seen_this_batch[h] = row
            batch_rows.append(row)
            inserted += 1
        else:
            existing.last_seen = now
            # A soft-duplicate match means a different source described the same
            # posting -- prefer whichever source's snippet is more complete rather
            # than freezing on whichever was seen first (a mangled/truncated
            # snippet from one source shouldn't outlive a cleaner one from another).
            new_snippet = job.get("snippet", "") or ""
            if len(new_snippet) > len(existing.snippet or ""):
                existing.snippet = new_snippet
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
        "_eval_verdict": r.eval_verdict,
        "_eval_signature": r.eval_signature,
        "_eval_analysis": r.eval_analysis,
    } for r in rows]


def _mark(db: Session, profile_id: int, identities: list[str], state: str) -> None:
    if not identities:
        return
    db.query(JobSeen).filter(
        JobSeen.profile_id == profile_id,
        JobSeen.identity_hash.in_(identities),
        JobSeen.state != "shown",   # never downgrade a shown row
    ).update({JobSeen.state: state}, synchronize_session=False)
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
                      strong: list[dict], backup: list[dict], disqualified: list[dict],
                      eval_sig: str) -> None:
    """Store the final-AI verdict per freshly-judged job so an unchanged profile never
    re-pays the expensive model for it. Jobs the AI omitted are recorded as 'reject';
    ones it flagged as hard-disqualified carry the AI's own reason in eval_analysis
    (see the "disqualified" list in the final-eval schema) instead of the blank
    analysis a plain reject used to get -- jobs simply not chosen among the best
    options (passed disqualifiers but weren't picked) still record no reasoning."""
    if not judged:
        return
    strong_ids = {s.get("_identity") for s in strong if s.get("_identity")}
    backup_by = {b.get("_identity"): b for b in backup if b.get("_identity")}
    disqualified_by = {d.get("_identity"): d.get("reason", "") for d in disqualified if d.get("_identity")}
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
        elif ident in disqualified_by:
            verdict, src = "reject", {"concerns": [disqualified_by[ident]]} if disqualified_by[ident] else {}
        else:
            verdict, src = "reject", j
        analysis = json.dumps({
            "summary": src.get("summary", ""),
            "role_type": src.get("role_type", ""),
            # The judge's explicit reasoning split (see full_auto's
            # _FINAL_EVAL_REASONING): can-do-fit is judged separately from
            # want-fit, and top_match_reason synthesizes both into one
            # candidate-facing narrative. Persisted alongside the rest so a
            # cache-served verdict renders identically to a freshly-judged one.
            "can_do_fit": src.get("can_do_fit", ""),
            "top_match_reason": src.get("top_match_reason", ""),
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
    same as any other reject."""
    row = db.execute(
        select(JobSeen).where(JobSeen.profile_id == profile_id, JobSeen.identity_hash == identity)
    ).scalar_one_or_none()
    if row is None:
        return
    row.eval_verdict = "reject"
    row.eval_analysis = json.dumps({"summary": "", "concerns": [reason]})
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

def _ensure_embeddings(engine, db: Session, rows: list[JobSeen]) -> int:
    """Compute and cache an embedding for any row missing one. Each job is
    embedded exactly once ever; later runs only pay for newly-discovered rows.
    Returns how many were embedded this call (for logging)."""
    missing = [r for r in rows if not r.embedding]
    if not missing:
        return 0
    texts = [f"{r.title} {r.company or ''} {(r.snippet or '')[:2000]}" for r in missing]
    embeddings = []
    for i in range(0, len(texts), 100):
        embeddings.extend(engine.get_embeddings_batch(texts[i:i+100]))
    for r, emb in zip(missing, embeddings):
        r.embedding = json.dumps(emb)
    db.commit()
    return len(missing)


def _score_rows(engine, rows: list[JobSeen], cluster_embeddings: list[list[float]]) -> list[dict]:
    """Cosine each row's cached embedding against EVERY role-cluster embedding
    (free, local) and keep the best. A job is assigned to whichever cluster it
    matches best (_cluster, an index into cluster_embeddings) so pooling/
    gating/final-eval downstream can treat each role interest independently
    instead of judging every job against one blended average of all of them.
    Returns engine-shaped dicts sorted by score desc with embed_score,
    _cluster, and _identity."""
    scored: list[tuple[float, int, JobSeen]] = []
    for r in rows:
        try:
            emb = json.loads(r.embedding) if r.embedding else None
        except (ValueError, TypeError):
            emb = None
        if emb:
            per_cluster = [engine.cosine_similarity(ce, emb) for ce in cluster_embeddings]
            best_idx = max(range(len(per_cluster)), key=lambda i: per_cluster[i])
            best_score = per_cluster[best_idx]
        else:
            best_idx, best_score = 0, 0.0
        scored.append((best_score, best_idx, r))
    scored.sort(key=lambda t: t[0], reverse=True)
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


def _gate_rank_refill_cluster(
    queue: list[dict], cluster_profile: dict, engine, db: Session, run: SearchRun,
    judge_target: int, examine_cap: int,
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
    looked at."""
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
        _check_cancelled(db, run)
        batch_size = min(len(queue) - pos, TARGET_POOL, examine_cap - examined)
        batch = queue[pos:pos + batch_size]
        pos += batch_size
        examined += len(batch)

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

    rank_floor_rejected = len(below_rank_floor)

    # Rank-side floor backfill: never send the judge fewer than MIN_RESULTS
    # for a cluster that has any gate survivors at all -- an absolute score
    # cutoff (unlike the old relative bottom-20% trim) can in principle reject
    # every candidate in a genuinely weak cluster.
    floor_target = min(MIN_RESULTS, len(gate_survivors))
    if len(judge_eligible) < floor_target:
        need = floor_target - len(judge_eligible)
        promoted = sorted(below_rank_floor, key=lambda x: x.get("_rank_score", 0), reverse=True)[:need]
        if promoted:
            backfilled = True
        judge_eligible.extend(promoted)
        below_rank_floor = [j for j in below_rank_floor if j not in promoted]
        rank_floor_rejected -= len(promoted)

    judge_eligible.sort(key=lambda j: j.get("_rank_score", 50.0), reverse=True)
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


def _filter_by_country(engine, jobs: list[dict], country_codes: list[str]) -> list[dict]:
    """Hard-drop jobs whose location isn't the selected country. A job is only
    kept if it positively matches an allowed country. The snippet is only
    consulted when location is inconclusive -- either genuinely blank, or a
    bare work-arrangement descriptor ("Hybrid"/"Distributed"/"In-Office") that
    carries no place information at all. A known-but-unrecognised REAL place
    name (e.g. "Riga, Latvia" when our token list doesn't cover Latvia) must
    NOT fall back to scanning the description, since a global company's
    boilerplate ("headquartered in London...") will false-positive-match the
    HQ's country for a role based somewhere else entirely. country_codes == []
    means "Global" -- no filtering. Anything we can't positively confirm is
    dropped: that's the point of a hard filter, not a reason to wave it through."""
    if not country_codes:
        return jobs
    allowed = set(country_codes)
    kept = []
    for j in jobs:
        location = j.get("location", "") or ""
        cc = engine.country_of(location)
        if cc is None and (not location.strip() or _is_location_scope_descriptor(location)):
            cc = engine.country_of(j.get("snippet", "") or "")
        if cc in allowed:
            kept.append(j)
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
    back to "error" or "done"."""


def _check_cancelled(db: Session, run: SearchRun) -> None:
    """Cooperative-cancellation checkpoint, called at each major phase
    boundary below. The cancel endpoint commits on its own request-scoped
    session; this session's in-memory `run` won't reflect that commit until
    reloaded, so db.refresh() (a real SELECT) is required here rather than
    trusting the attribute already on the object."""
    db.refresh(run)
    if run.cancel_requested:
        raise SearchCancelled()


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
        engine.emit(f"[pipeline] final_evaluation cluster[{idx}] ({label}) took "
                    f"{time.monotonic() - _cluster_eval_start:.1f}s for {len(fresh)} job(s) -> "
                    f"{len(strong) if strong is not None else 0} strong, "
                    f"{len(backup) if strong is not None else 0} backup, {_rejected_this_call} rejected "
                    f"({len(disqualified) if strong is not None else 0} with a disqualifier reason)")
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

    # DISCOVERY (cheap, tiered on first run) -> store. gather_jobs reads the flag.
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
    n_fresh = len(rows) - len(resurfaced)
    if len(rows) < TARGET_POOL:
        # Thin-run safety net (the original behaviour): still short of a full pool, so
        # top up with any other enriched rows (except known rejects) to avoid an empty
        # screen.
        extra = [r for r in _backlog_rows(db, profile_id, BACKLOG_TOPUP, exclude_rejects=True)
                 if r.identity_hash not in seen_ids]
        rows = rows + extra
        resurfaced = resurfaced + extra
    emit(f"[pipeline] pool assembled: {n_fresh} fresh 'new' + {len(resurfaced)} "
         f"resurfaced backlog row(s) -> {len(rows)} total")
    funnel["pool_rows"] = len(rows)
    funnel["pool_resurfaced"] = len(resurfaced)
    _snap("pool", rows)
    if not rows:
        emit("[pipeline] STOP: nothing to evaluate (store empty and no backlog) -> 0 results")
        return [], False, None, timings, None, funnel

    # FILTER: embed (cached) + cosine-score the whole set against every role
    # cluster, take a fair adaptive pool per cluster.
    _progress(db, run, f"Found {len(rows)} jobs, scoring…")
    n_embedded = _ensure_embeddings(engine, db, rows)
    funnel["embedded_new"] = n_embedded
    if n_embedded:
        emit(f"[pipeline] embedded {n_embedded} new rows (cached for future runs)")
    t0 = _lap("embed", t0)
    scored = _score_rows(engine, rows, cluster_embeddings)
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
    emit(f"[pipeline] candidate queues: {total_queued} total across {len(queues)} cluster(s) "
         f"available to gate (>= RELEVANCE_FLOOR) (harsh/broadened={harsh}) "
         f"fallbacks={dict(pool_fallbacks)}")
    if not any(queues.values()):
        emit(f"[pipeline] STOP: no candidates above RELEVANCE_FLOOR({RELEVANCE_FLOOR}) in any cluster -> 0 results")
        return [], harsh, None, timings, _compose_fallback_warning(role_clusters, fallback_notes), funnel

    # GATE + RANK, combined per cluster with refill: each cluster's own
    # embed-score-ordered queue is fed through screen_gate then rank_gate in
    # batches (see _gate_rank_refill_cluster) until that cluster's fair share
    # of JUDGE_POOL rank-floor survivors accumulate, the queue is exhausted, or
    # its fair share of the examine budget has been examined -- unlike the old
    # one-shot TARGET_POOL-capped gate call, this keeps pulling from the idle
    # above-floor pool instead of accepting a thin result when a harsher gate
    # or the rank floor (RANK_REJECT_SCORE_FLOOR) leaves a cluster short.
    # Each cluster targets JUDGE_POOL / active-cluster-count rather than the
    # full JUDGE_POOL, and is capped at SINGLE_CLUSTER_EXAMINE_CAP (1 cluster)
    # or MULTI_CLUSTER_EXAMINE_CAP (2+) total examined -- otherwise every
    # cluster in a multi-stream profile independently grinds toward the full
    # JUDGE_POOL target each, producing far more judge-eligible candidates than
    # the final cross-cluster fair-allocate below will ever use. sector_ok
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
    num_active_clusters = sum(1 for q in queues.values() if q)
    cluster_judge_target = -(-JUDGE_POOL // num_active_clusters) if num_active_clusters else JUDGE_POOL
    cluster_examine_cap = SINGLE_CLUSTER_EXAMINE_CAP if num_active_clusters <= 1 else MULTI_CLUSTER_EXAMINE_CAP
    for idx, queue in queues.items():
        if not queue:
            continue
        cluster_profile = dict(eng_profile)
        cluster_profile["search_terms"] = role_clusters[idx].get("roles") or eng_profile.get("search_terms")
        cluster_profile["_multi_cluster"] = len(role_clusters) > 1
        judge_eligible, gate_survivors, stats = _gate_rank_refill_cluster(
            queue, cluster_profile, engine, db, run, cluster_judge_target, cluster_examine_cap
        )
        rank_by_cluster[idx] = judge_eligible
        examined_ids.update(j["_identity"] for j in queue[:stats["examined"]])
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
    t0 = _lap("gate", t0)

    selected = _fair_allocate(rank_by_cluster, JUDGE_POOL)
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
        return [], True, ([j["_identity"] for j in scored], []), timings, \
            _compose_fallback_warning(role_clusters, fallback_notes), funnel
    _check_cancelled(db, run)

    # EVALUATE. When full-page scraping is enabled (default), read each selected
    # job's real page first, so the final LLM judges fit against the actual
    # posting text (seniority/experience/location) instead of a short snippet
    # -- but only for jobs whose snippet doesn't already have enough to judge
    # from (see _needs_full_scrape); skipping the rest is most of the win here,
    # since it's the largest source of both run time and anti-bot blocking.
    to_evaluate = selected
    if get_full_scrape_enabled(db):
        needs_scrape: list[dict] = []
        already_ready: list[dict] = []
        for j in selected:
            if _needs_full_scrape(j):
                needs_scrape.append(j)
            else:
                j["full_text"] = j.get("snippet", "")
                already_ready.append(j)
        funnel["scrape_needed"] = len(needs_scrape)
        funnel["scrape_already_ready"] = len(already_ready)
        emit(f"[pipeline] phase 5: {len(needs_scrape)}/{len(selected)} candidates need a full-page "
             f"scrape ({len(already_ready)} already have enough detail from their source)")
        if needs_scrape:
            _progress(db, run, "Reading full job pages…")
            browser_config = engine.BrowserConfig(
                headless=True, verbose=False, viewport_width=1280, viewport_height=800,
                user_agent_mode="random",
            )
            async with engine.AsyncWebCrawler(config=browser_config) as crawler:
                scraped = await engine.scrape_full_details(
                    needs_scrape, crawler, blocked_domains=set(get_blocked_domains(db)),
                    country_code=eng_profile.get("adzuna_country_code", "gb"),
                )
            _persist_scrape(db, profile_id, scraped)  # reuse the page next run, no re-scrape
            scraped_dead = [j for j in scraped if j.get("_dead_reason")]
            scraped_live = [j for j in scraped if not j.get("_dead_reason")]
            _persist_dead_scrapes(db, profile_id, scraped_dead)
            funnel["dead_dropped"] = len(scraped_dead)
            if scraped_dead:
                emit(f"[pipeline] phase 5: {len(scraped_dead)} listing(s) confirmed dead/expired "
                     f"(no alt-source recovery) -- excluded before final judge")
            to_evaluate = already_ready + scraped_live
        else:
            to_evaluate = already_ready
        t0 = _lap("scrape", t0)
    else:
        emit("[pipeline] full-page scraping disabled in settings; evaluating on snippets")
    _snap("scraped", to_evaluate)

    # Final judgment runs once PER CLUSTER, each given a cv_text scoped to just
    # that cluster's roles (see cv_text_for_cluster) -- so the judge weighs fit
    # against ONE coherent role identity instead of every field the candidate
    # has ever listed. A SINGLE expensive call per cluster returns both a strict
    # "strong" list and a lenient disqualifier-only "backup" list, so a cluster
    # with no strong fits no longer costs a second full call. Any job already
    # judged under this exact CV (unchanged profile) is served from its stored
    # verdict and never re-sent to the expensive model. If nothing is strong and
    # there's no backup, a deterministic no-LLM fallback surfaces the top-scoring
    # candidates rather than silently contributing nothing.
    to_evaluate_by_cluster: dict[int, list[dict]] = defaultdict(list)
    for j in to_evaluate:
        company = (j.get("company") or "").strip().lower()
        n_titles = len(company_title_counts.get(company, ()))
        if n_titles >= TEMPLATE_FACTORY_TITLE_THRESHOLD:
            j["_posting_volume_hint"] = f"{n_titles} differently-titled roles from this source this run"
        to_evaluate_by_cluster[j.get("_cluster", 0)].append(j)

    final_by_cluster: dict[int, list[dict]] = {}
    final_fresh_judged = final_reused_from_cache = 0
    final_strong = final_backup = final_disqualified = 0
    final_scam_verified_dropped = 0
    scam_verify_budget = [SCAM_VERIFY_MAX_PER_RUN]

    _progress(db, run, "Final AI review…")
    _check_cancelled(db, run)
    # Judge every cluster concurrently -- _fair_allocate has already picked each
    # cluster's `to_evaluate_by_cluster[idx]` by this point, so unlike the
    # gate+rank stage there's no cross-cluster fairness left for concurrency to
    # disturb; running these sequentially (one blocking expensive-model call per
    # cluster, plus its own backfill/scam-verify) was most of what made "final
    # matching" feel slow. _run_cluster_final_eval makes no DB writes and touches
    # no shared state, so persistence, funnel counters, and scam-verify (which
    # spends a shared per-run budget) all happen below, sequentially, once every
    # cluster's judging has returned.
    cluster_items = list(to_evaluate_by_cluster.items())
    eval_results: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(cluster_items))) as pool:
        futures = {
            pool.submit(_run_cluster_final_eval, idx, jobs, role_clusters, cv_text_base,
                        eng_profile, rank_by_cluster, engine): idx
            for idx, jobs in cluster_items
        }
        for fut in as_completed(futures):
            eval_results[futures[fut]] = fut.result()

    for idx, jobs in cluster_items:
        r = eval_results[idx]
        final_fresh_judged += len(r["fresh"])
        final_reused_from_cache += r["reused_from_cache"]
        if r["fresh"] and not r["call_failed"]:
            _persist_verdicts(db, profile_id, r["fresh"], r["strong"], r["backup"], r["disqualified"], r["eval_sig"])
        final_strong += len(r["strong"])
        final_backup += len(r["backup"])
        final_disqualified += len(r["disqualified"])

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
            final_disqualified += len(r["b_disqualified"])

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

    funnel["final_fresh_judged"] = final_fresh_judged
    funnel["final_reused_from_cache"] = final_reused_from_cache
    funnel["final_strong"] = final_strong
    funnel["final_backup"] = final_backup
    funnel["final_disqualified"] = final_disqualified
    funnel["final_scam_verified_dropped"] = final_scam_verified_dropped

    # Same fair-allocation logic as pooling/top-N: total output stays capped at
    # FINAL_PICKS, redistributed across clusters rather than added per cluster.
    # Allocated in two tiers (strong_fit, already set on every pick above) rather
    # than one pass over each cluster's already tier-ordered list -- a single pass
    # takes each cluster's WHOLE share in cluster order, so a cluster with zero
    # strong picks could contribute its backup-tier filler ahead of a later
    # cluster's genuine strong picks. fit_rank (below) is assigned purely by
    # position in `final`, and nothing downstream re-sorts by verdict, so that
    # ordering bug rode all the way to the UI. Splitting by tier first guarantees
    # every strong pick outranks every backup pick globally, while each tier's own
    # fair-allocate pass still distributes slots across clusters fairly.
    strong_by_cluster = {idx: [p for p in picks if p.get("strong_fit")]
                          for idx, picks in final_by_cluster.items()}
    backup_by_cluster = {idx: [p for p in picks if not p.get("strong_fit")]
                          for idx, picks in final_by_cluster.items()}
    final = _fair_allocate(strong_by_cluster, engine.FINAL_PICKS)
    if len(final) < engine.FINAL_PICKS:
        final += _fair_allocate(backup_by_cluster, engine.FINAL_PICKS - len(final))
    t0 = _lap("final_eval", t0)
    funnel["final_picks"] = len(final)
    _snap("final_picks", final)
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
    return final, harsh or bool(fallback_notes), (processed_ids, shown_ids), timings, warning, funnel


def reap_stale_search_runs(db: Session) -> int:
    """Called once at process startup (see main.py). Any SearchRun still
    status="running" was orphaned by the PREVIOUS process lifetime -- crash,
    `uvicorn --reload` restart, manual kill, etc. -- since run_search_task's
    worker thread died with that process and nothing else will ever revisit
    the row. Left alone it permanently occupies a daily search-cap slot
    and /search/status serves a run stuck at "running" forever. Returns
    the number of rows reaped."""
    stale = db.execute(select(SearchRun).where(SearchRun.status == "running")).scalars().all()
    if not stale:
        return 0
    now = datetime.utcnow()
    for run in stale:
        run.status = "error"
        run.message = "Search was interrupted by a server restart. Please run a new search."
        run.finished_at = now
    db.commit()
    return len(stale)


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
    _safe_print(f"\n[pipeline] ── search run {run_id} for profile {profile_id} starting ──")
    try:
        import full_auto as engine  # lazy: pulls in crawl4ai only now
        engine.init_db()  # ensures gate_cache/jobs/profile_cache tables exist

        # Cached: only actually calls the LLM when the profile's inputs changed
        # since the last run (or never ran). See profile_intel.py.
        ensure_profile_intel(db, profile_id)

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
            _safe_print(f"[pipeline] ── search run {run_id} cancelled (caught before persisting results) ──\n")
            return

        # Prune only after the pipeline has succeeded, so a failed run leaves the
        # previous "crossed" roles intact instead of wiping them with nothing
        # to replace them.
        _prune_previous_roles(db, profile_id)
        _expire_stale_roles(db, profile_id)

        for rank, entry in enumerate(final, start=1):
            db.add(Role(
                profile_id=profile_id,
                search_run_id=run.id,
                external_id=entry.get("_identity") or _external_id(engine, entry),
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
                status="new",
            ))

        if marks:
            processed_ids, shown_ids = marks
            _mark(db, profile_id, processed_ids, "enriched")  # everything we evaluated
            _mark(db, profile_id, shown_ids, "shown")         # the up-to-10 displayed

        run.result_count = len(final)
        run.status = "done"
        run.finished_at = datetime.utcnow()
        run.phase_timings = json.dumps(timings)
        # Samples ride inside `funnel` purely to keep the pipeline's return arity
        # (and its early returns) unchanged -- split back out here so
        # funnel_counts stays ints/bools only, as get_run_funnel expects.
        run.snapshot_samples = json.dumps(funnel.pop("samples", {}))
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
        _safe_print(f"[pipeline] ── search run {run_id} cancelled mid-run ──\n")
    except Exception as e:  # never let the worker thread die silently
        import traceback
        traceback.print_exc()  # full stack trace to the backend console
        db.rollback()
        if run:
            run.status = "error"
            run.message = f"Search failed: {e!r}" if str(e) else f"Search failed: {type(e).__name__} (see backend console for traceback)"
            run.finished_at = datetime.utcnow()
            db.commit()
        _safe_print(f"[pipeline] ── search run {run_id} FAILED, see traceback above ──\n")
    finally:
        db.close()
