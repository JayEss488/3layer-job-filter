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
import re
import time
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import DISCOVERY_ATS_CACHE_TTL_HOURS
from ..database import SessionLocal
from ..models import Role, SearchRun, JobSeen
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
# Funnel: TARGET_POOL candidates survive embedding+gate -> cheap numeric rank_gate
# scores all of them -> bottom RANK_AUTOREJECT_FRACTION dropped -> top JUDGE_POOL
# go to the expensive full-text judge -> engine.FINAL_PICKS capped final results.
TARGET_POOL       = 90     # candidates fed to the cheap gate + cheap rank stage
MIN_RESULTS       = 3      # below this many strong matches, broaden the threshold
# Below this many characters, a job's discovery-time snippet is assumed too
# thin (e.g. a short Google-organic blurb) to judge fit against without
# reading the real page. ATS-sourced snippets (greenhouse/lever/etc.) already
# carry the full posting description and skip this check entirely. Adzuna's
# API teaser is truncated at exactly 500 chars -- keep this strictly above
# that or every Adzuna snippet waves through as "sufficient" purely because
# its truncation length happens to land on the threshold.
SNIPPET_SUFFICIENT_CHARS = 600
# Highest-relevance candidates per cluster (by embed_score) that skip the cheap
# gate's seniority veto and go straight to the expensive AI (the real judge),
# using the score we already computed rather than risking loss at the gate.
AUTO_PASS_TOP = 2
RELEVANCE_PRIMARY = 0.35   # strict strong-fit threshold
RELEVANCE_FLOOR    = 0.20  # never include anything weaker than this
BACKLOG_TOPUP     = 40     # enriched rows pulled in when fresh discovery is thin
STORE_SCORE_CAP   = 6000   # max 'new' rows relevance-scored per run (whole store)
# Cheap numeric-ranking stage (rank_gate), between the sector/seniority gate and
# the expensive full-text judge: an extra cheap-model pass that scores gate
# survivors 0-100 on fit instead of a boolean pass/fail, so the expensive judge
# only ever sees a curated top slice instead of every gate survivor.
JUDGE_POOL = 40               # top-ranked candidates sent on to scrape + judge
RANK_AUTOREJECT_FRACTION = 0.20  # bottom fraction of the cheap ranking dropped first


def _external_id(engine, job: dict) -> str:
    return engine.make_job_id(job.get("board", ""), job.get("url", ""))


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
    return len((job.get("snippet") or "").strip()) < SNIPPET_SUFFICIENT_CHARS


# Free, high-confidence seniority pre-reject: a junior/graduate candidate will never
# get a Director/VP role and a senior candidate won't take an internship. Matched
# against the job TITLE only, so it never fires on a stray body-text mention.
_SENIOR_TITLE_RE = re.compile(
    r"\b(director|vice[- ]president|vp|head of|principal|chief|c[tefo]o|partner)\b", re.I)
_JUNIOR_TITLE_RE = re.compile(
    r"\b(intern(ship)?|graduate|placement|apprentice(ship)?|trainee|entry[- ]level)\b", re.I)
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


def _compose_analysis(entry: dict) -> str:
    parts = []
    if entry.get("_cluster_label"):
        parts.append(f"Matched via: {entry['_cluster_label']} track")
    if entry.get("strong_fit") is False:
        parts.append("⚠ Closest available match — no role fully met the bar this run.")
    parts.append(entry.get("summary", "").strip())
    for r in entry.get("match_reasons", []) or []:
        parts.append(f"✓ {r}")
    for c in entry.get("concerns", []) or []:
        parts.append(f"⚠ {c}")
    return "\n".join(p for p in parts if p)


def _compose_fallback_warning(role_clusters: list[dict], fallback_notes: dict[int, set[str]]) -> str | None:
    """Turn per-cluster fallback tags (set by _adaptive_pool_by_cluster's
    "broadened"/"floor_fallback" and _run_engine_pipeline's "gate_fallback"/
    "eval_fallback") into a user-facing message. Names the specific role when
    only one cluster needed a fallback; generic wording otherwise."""
    affected = [idx for idx, tags in fallback_notes.items() if tags]
    if not affected:
        return None
    if len(role_clusters) <= 1 or len(affected) > 1:
        return ("Your profile or filters look strict — showing the best available "
                "matches anyway; some may be a stretch.")
    idx = affected[0]
    label = _cluster_label(role_clusters[idx])
    tags = fallback_notes[idx]
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


def identity_hash(job: dict) -> str:
    canon = _canonical_url(job.get("url", ""))
    basis = canon or f"{_norm(job.get('company',''))}|{_norm(job.get('title',''))}|{_norm(job.get('location',''))}"
    return hashlib.sha1(basis.encode()).hexdigest()


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
            row = JobSeen(
                profile_id=profile_id, identity_hash=h, source=job.get("board", ""),
                title=title, company=job.get("company"), location=job.get("location"),
                url=job.get("url"), snippet=job.get("snippet", ""),
                state="new", source_updated_at=upd_dt, first_seen=now, last_seen=now,
            )
            db.add(row)
            seen_this_batch[h] = row
            inserted += 1
        else:
            existing.last_seen = now
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
    db.commit()
    return inserted, refreshed, requeued


def _new_rows(db: Session, profile_id: int, limit: int = STORE_SCORE_CAP) -> list[JobSeen]:
    """Fresh, unprocessed rows. Freshest first. The limit is the whole-store cap:
    relevance scoring runs over all of them (cheap, since embeddings are cached),
    so a genuinely good role can't be excluded by an arbitrary small slice."""
    return db.execute(
        select(JobSeen)
        .where(JobSeen.profile_id == profile_id, JobSeen.state == "new")
        .order_by(JobSeen.first_seen.desc())
        .limit(limit)
    ).scalars().all()


def _backlog_rows(db: Session, profile_id: int, limit: int) -> list[JobSeen]:
    """Enriched-but-unshown rows. Used to top up a thin run (surfacing the
    backlog) so the user never sees an empty screen. Already-processed, so
    re-considering them is free apart from one shared LLM call."""
    return db.execute(
        select(JobSeen)
        .where(JobSeen.profile_id == profile_id, JobSeen.state == "enriched")
        .order_by(JobSeen.last_seen.desc())
        .limit(limit)
    ).scalars().all()


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
            "match_reasons": src.get("match_reasons", []),
            "concerns": src.get("concerns", []),
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


def _cluster_label(cluster: dict) -> str:
    roles = cluster.get("roles") or []
    return roles[0] if roles else "General"


def _adaptive_pool_by_cluster(scored: list[dict]) -> tuple[list[dict], bool, dict[int, str]]:
    """Per-cluster version of the strict/broaden pool: strict when a cluster's
    own matches are plentiful, broadened when sparse, and -- unlike before --
    never contributes zero for a cluster that has ANY scored candidates at
    all (falls back to its top few by score rather than silently vanishing).
    Slots are then fairly split across clusters (_fair_allocate) so one
    cluster's abundant supply can't crowd out another's sparse-but-real one.
    Returns (pool, harsh, fallback_reasons); fallback_reasons maps cluster
    index -> a short tag, only for clusters that needed broadening/fallback."""
    by_cluster: dict[int, list[dict]] = defaultdict(list)
    for j in scored:                      # `scored` is already sorted desc
        by_cluster[j.get("_cluster", 0)].append(j)

    cluster_lists: dict[int, list[dict]] = {}
    harsh = False
    fallback_reasons: dict[int, str] = {}
    for key, items in by_cluster.items():
        strong = [j for j in items if j["embed_score"] >= RELEVANCE_PRIMARY]
        if len(strong) >= TARGET_POOL:
            cluster_lists[key] = strong[:TARGET_POOL]
            continue
        if len(strong) >= MIN_RESULTS:
            cluster_lists[key] = strong
            continue
        broadened = [j for j in items if j["embed_score"] >= RELEVANCE_FLOOR]
        if broadened:
            cluster_lists[key] = broadened[:TARGET_POOL]
            harsh = True
            fallback_reasons[key] = "broadened"
            continue
        # Nothing clears even the floor. Still surface the best few UNLESS the
        # top score is exactly 0.0 -- that means embedding the store's rows
        # failed for this batch (see _ensure_embeddings), not a genuine niche
        # result, so don't misreport it as "filters too strict".
        if items and items[0]["embed_score"] > 0.0:
            cluster_lists[key] = items[:MIN_RESULTS]
            harsh = True
            fallback_reasons[key] = "floor_fallback"
        else:
            cluster_lists[key] = []
            if items:
                fallback_reasons[key] = "embedding_failure"

    pool = _fair_allocate(cluster_lists, TARGET_POOL)
    return pool, harsh, fallback_reasons


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


def _filter_by_country(engine, jobs: list[dict], country_codes: list[str]) -> list[dict]:
    """Hard-drop jobs whose location isn't the selected country. A job is only
    kept if it positively matches an allowed country. The snippet is only
    consulted when location is genuinely blank -- a known-but-unrecognised
    location (e.g. "Riga, Latvia" when our token list doesn't cover Latvia)
    must NOT fall back to scanning the description, since a global company's
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
        cc = engine.country_of(location) if location.strip() else engine.country_of(j.get("snippet", "") or "")
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


def _progress(db: Session, run: SearchRun, message: str) -> None:
    run.message = message
    db.commit()


async def _run_engine_pipeline(engine, eng_profile, weighted_text, cv_text_base, db, profile_id, run: SearchRun):
    emit = engine.emit  # prints to the backend's own console (see run_search_task)
    timings: dict[str, float] = {}
    t0 = time.monotonic()

    def _lap(phase: str, since: float) -> float:
        timings[phase] = round(time.monotonic() - since, 2)
        return time.monotonic()

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
         f"ats_batch={'querying fresh' if ats_stale else 'skipped (cached, within TTL)'}, "
         f"terms={eng_profile.get('search_terms', [])[:5]})")
    _progress(db, run, "Searching job boards…")
    raw_jobs = engine.gather_jobs(eng_profile)
    t0 = _lap("discovery", t0)
    if ats_stale:
        mark_ats_batch_fetched(db, profile_id)
    raw_jobs, n_blocked = filter_blocked(raw_jobs, get_blocked_domains(db))
    if n_blocked:
        emit(f"[pipeline] spam-domain blocklist dropped {n_blocked} listing(s)")
    raw_jobs, n_training = _filter_training(engine, raw_jobs)
    if n_training:
        emit(f"[pipeline] training/placement-scheme filter dropped {n_training} listing(s)")
    breakdown = _board_breakdown(raw_jobs)
    emit(f"[pipeline] discovery returned {len(raw_jobs)} raw listings by board: {breakdown}")
    save_last_run_counts(db, counts_from_breakdown(breakdown))  # for the settings screen

    country_codes = eng_profile.get("country_codes") or []
    if country_codes:
        before = len(raw_jobs)
        raw_jobs = _filter_by_country(engine, raw_jobs, country_codes)
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
        emit(f"[pipeline] salary filter (floor={salary_floor}): {before} -> {len(raw_jobs)} listings")

    inserted, refreshed, requeued = _upsert_discovered(db, profile_id, raw_jobs)
    store_counts = _store_counts(db, profile_id)
    emit(f"[pipeline] store upsert: +{inserted} new, {refreshed} refreshed, "
         f"{requeued} requeued | store totals for this profile: {store_counts}")

    # ASSEMBLE the candidate rows: the whole 'new' store, topped up from the
    # enriched backlog when discovery was thin (this is "surfacing the backlog").
    rows = _new_rows(db, profile_id)
    if len(rows) < TARGET_POOL:
        backlog = _backlog_rows(db, profile_id, BACKLOG_TOPUP)
        rows = rows + backlog
        emit(f"[pipeline] 'new' rows={len(rows) - len(backlog)} (< TARGET_POOL={TARGET_POOL}); "
             f"topped up with {len(backlog)} backlog rows -> {len(rows)} total")
    else:
        emit(f"[pipeline] scoring whole 'new' store: {len(rows)} rows")
    if not rows:
        emit("[pipeline] STOP: nothing to evaluate (store empty and no backlog) -> 0 results")
        return [], False, None, timings, None

    # FILTER: embed (cached) + cosine-score the whole set against every role
    # cluster, take a fair adaptive pool per cluster.
    _progress(db, run, f"Found {len(rows)} jobs, scoring…")
    n_embedded = _ensure_embeddings(engine, db, rows)
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
        emit(f"[pipeline] country filter on candidates {country_codes}: "
             f"{before} -> {len(scored)} rows")
    if eng_profile.get("location_scope") == "local" and local_place:
        before = len(scored)
        scored = _filter_by_local_place(scored, local_place)
        emit(f"[pipeline] local-place filter on candidates ({local_place!r}): "
             f"{before} -> {len(scored)} rows")
    top_score = scored[0]["embed_score"] if scored else 0.0
    above_primary = sum(1 for j in scored if j["embed_score"] >= RELEVANCE_PRIMARY)
    emit(f"[pipeline] scored {len(scored)} candidates | top_score={top_score:.3f} | "
         f">= RELEVANCE_PRIMARY({RELEVANCE_PRIMARY})={above_primary}")
    _log_score_distribution(emit, scored)

    scored, n_prescreen = _heuristic_prescreen(scored, eng_profile)
    if n_prescreen:
        emit(f"[pipeline] heuristic prescreen dropped {n_prescreen} obvious seniority mismatch(es) "
             f"before any gate")

    pool, harsh, pool_fallbacks = _adaptive_pool_by_cluster(scored)
    for idx, reason in pool_fallbacks.items():
        fallback_notes[idx].add(reason)
    emit(f"[pipeline] adaptive pool size={len(pool)} (harsh/broadened={harsh}) "
         f"fallbacks={dict(pool_fallbacks)}")
    if not pool:
        emit(f"[pipeline] STOP: no candidates above RELEVANCE_FLOOR({RELEVANCE_FLOOR}) in any cluster -> 0 results")
        return [], harsh, None, timings, _compose_fallback_warning(role_clusters, fallback_notes)

    # GATE: sector fit is judged per role-cluster (each cluster's own roles as
    # the "candidate target roles" signal) so the call isn't confused by a
    # candidate's OTHER, unrelated target roles; seniority is profile-wide, so
    # it runs once on the combined sector-gate survivors. Discarded roles stay
    # in the store (marked enriched below) and can resurface via backlog, but
    # won't be re-judged while the profile signature is unchanged (gate cache)
    # -- each cluster's own search_terms naturally land in separate cache rows.
    pool_by_cluster: dict[int, list[dict]] = defaultdict(list)
    for j in pool:
        pool_by_cluster[j.get("_cluster", 0)].append(j)

    # GATE: one merged sector+seniority screen per cluster (each cluster's own roles
    # as the sector signal). The screen ANNOTATES rather than drops; we then hard-drop
    # only off-sector jobs, DEMOTE (never drop) seniority failures, and guarantee a
    # per-cluster floor -- so the cheap gate can't starve a cluster to zero, and the
    # full-text final AI stays the authoritative seniority judge. Discarded roles stay
    # in the store (marked enriched below) and won't be re-judged while the profile
    # signature is unchanged (gate cache) -- each cluster's own search_terms naturally
    # land in separate cache rows.
    _progress(db, run, "Screening candidates…")
    survivors_by_cluster: dict[int, list[dict]] = {}
    for idx, cluster_pool in pool_by_cluster.items():
        cluster_profile = dict(eng_profile)
        cluster_profile["search_terms"] = role_clusters[idx].get("roles") or eng_profile.get("search_terms")
        annotated = engine.screen_gate(cluster_pool, cluster_profile)
        annotated.sort(key=lambda x: x.get("embed_score", 0), reverse=True)

        in_sector = [j for j in annotated if j.get("_sector_ok", True)]
        # Auto-admit the top few by relevance regardless of the seniority veto -- send
        # the highest-cosine candidates straight to the expensive AI (the real judge).
        auto_ids = {id(j) for j in in_sector[:AUTO_PASS_TOP]}
        primary = [j for j in in_sector if j.get("_seniority_ok", True) or id(j) in auto_ids]

        if len(primary) >= MIN_RESULTS:
            survivors = primary
        elif in_sector:
            # Keep floor: backfill from demoted (in-sector, seniority-failed) jobs by
            # score so a cluster with a real pool never reaches final eval starved.
            primary_ids = {id(j) for j in primary}
            demoted = [j for j in in_sector if id(j) not in primary_ids]
            survivors = primary + demoted[:max(0, MIN_RESULTS - len(primary))]
            if len(survivors) > len(primary):
                fallback_notes[idx].add("gate_fallback")
        else:
            # Sector wiped everything (genuinely wrong field): fall back to this
            # cluster's own top pool slice rather than contributing nothing.
            survivors = sorted(cluster_pool, key=lambda x: x.get("embed_score", 0),
                               reverse=True)[:MIN_RESULTS]
            if survivors:
                fallback_notes[idx].add("gate_fallback")
        survivors_by_cluster[idx] = survivors
    t0 = _lap("gate", t0)

    selected_by_cluster = {
        idx: sorted(items, key=lambda x: x.get("embed_score", 0), reverse=True)
        for idx, items in survivors_by_cluster.items() if items
    }
    selected = _fair_allocate(selected_by_cluster, TARGET_POOL)
    emit(f"[pipeline] gates: pool={len(pool)} -> survivors={sum(len(v) for v in survivors_by_cluster.values())} "
         f"-> selected top-{len(selected)} across {len(selected_by_cluster)} cluster(s) for cheap ranking")
    if not selected:
        # Should be unreachable: every cluster in pool_by_cluster gets a
        # guaranteed non-empty fallback above. Kept as a defensive backstop.
        emit("[pipeline] STOP: screen produced nothing despite a non-empty pool -> 0 results")
        return [], True, ([j["_identity"] for j in pool], []), timings, \
            _compose_fallback_warning(role_clusters, fallback_notes)

    # RANK: one more cheap-model call per cluster over that cluster's own gate
    # survivors, estimating a numeric 0-100 fit score per job instead of a
    # boolean pass/fail, so the expensive full-text judge only ever sees a
    # curated top slice (JUDGE_POOL) instead of every gate survivor (up to
    # TARGET_POOL). Scoped per cluster (own cluster_profile, mirroring the gate
    # loop above) rather than once across the whole cross-cluster pool -- a
    # profile-wide call was scoring a minority cluster's jobs against a
    # candidate description dominated by the OTHER cluster's target roles,
    # which measurably tanked its scores. The bottom RANK_AUTOREJECT_FRACTION is
    # dropped PER CLUSTER (not off the merged pool) so a cluster that happens to
    # score lower can't lose more than its own share before fair-allocate ever
    # gets a chance to protect it. This is a per-run funnel decision, not a
    # permanent verdict -- unlike an expensive-AI reject, a cheap-rank exclusion
    # doesn't get persisted, so a job scored out here is still free to
    # resurface (and be re-ranked) on a future run.
    _progress(db, run, "Ranking candidates…")
    selected_for_rank: dict[int, list[dict]] = defaultdict(list)
    for j in selected:
        selected_for_rank[j.get("_cluster", 0)].append(j)

    rank_by_cluster: dict[int, list[dict]] = {}
    n_scored = n_autorejected = 0
    for idx, cluster_items in selected_for_rank.items():
        cluster_profile = dict(eng_profile)
        cluster_profile["search_terms"] = role_clusters[idx].get("roles") or eng_profile.get("search_terms")
        cluster_profile["_multi_cluster"] = len(role_clusters) > 1
        ranked = engine.rank_gate(cluster_items, cluster_profile)
        ranked_sorted = sorted(ranked, key=lambda j: j.get("_rank_score", 50.0), reverse=True)
        keep_n = max(1, int(len(ranked_sorted) * (1 - RANK_AUTOREJECT_FRACTION)))
        rank_by_cluster[idx] = ranked_sorted[:keep_n]
        n_scored += len(ranked_sorted)
        n_autorejected += len(ranked_sorted) - keep_n

    selected = _fair_allocate(rank_by_cluster, JUDGE_POOL)
    t0 = _lap("rank", t0)
    emit(f"[pipeline] cheap rank: {n_scored} scored across {len(rank_by_cluster)} cluster(s) -> "
         f"{n_autorejected} auto-dropped (bottom {RANK_AUTOREJECT_FRACTION:.0%} per cluster, kept "
         f"for a future run) -> top-{len(selected)} sent to full evaluation")

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
                    needs_scrape, crawler, blocked_domains=set(get_blocked_domains(db))
                )
            _persist_scrape(db, profile_id, scraped)  # reuse the page next run, no re-scrape
            to_evaluate = already_ready + scraped
        else:
            to_evaluate = already_ready
        t0 = _lap("scrape", t0)
    else:
        emit("[pipeline] full-page scraping disabled in settings; evaluating on snippets")

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
        to_evaluate_by_cluster[j.get("_cluster", 0)].append(j)

    final_by_cluster: dict[int, list[dict]] = {}
    for idx, jobs in to_evaluate_by_cluster.items():
        label = _cluster_label(role_clusters[idx])
        cluster_roles = role_clusters[idx].get("roles") or []
        cv_text = cv_text_for_cluster(cv_text_base, cluster_roles) if cluster_roles else cv_text_base
        eval_sig = hashlib.sha1(cv_text.encode()).hexdigest()[:16]

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

        _progress(db, run, "Final AI review…")
        emit(f"[pipeline] final_evaluation cluster[{idx}] ({label}): {len(fresh)} to judge, "
             f"{len(jobs) - len(fresh)} reused from prior verdict (LLM cap={engine.FINAL_PICKS})")

        strong, backup, disqualified, call_failed = [], [], [], False
        if fresh:
            strong, backup, disqualified = engine.final_evaluation_split(fresh, eng_profile, cv_text=cv_text)
            if strong is None:
                # The call itself failed (exception/malformed response) -- nothing
                # was actually judged. Don't persist any verdict, and don't treat
                # this the same as a genuine unanimous rejection below.
                call_failed = True
                strong, backup, disqualified = [], [], []
            else:
                _persist_verdicts(db, profile_id, fresh, strong, backup, disqualified, eval_sig)

        picks = [dict(p, strong_fit=True) for p in strong] + cached_strong
        if not picks:
            if backup:
                picks = [dict(p, strong_fit=False) for p in backup]
                fallback_notes[idx].add("eval_fallback")
            elif call_failed:
                # Only fall back to an unverified top-N when the AI call itself
                # failed -- never when it succeeded and genuinely rejected
                # everyone, and never resurfacing a job already rejected under
                # this exact profile signature.
                fallback_pool = [j for j in jobs if j.get("_identity") not in previously_rejected_ids]
                picks = [
                    dict(j, strong_fit=False, summary="", match_reasons=[],
                         concerns=["Automated review was inconclusive this run -- showing the "
                                   "closest available match unverified."])
                    for j in fallback_pool[:MIN_RESULTS]
                ]
                fallback_notes[idx].add("eval_fallback")
            # else: the AI reviewed everyone and rejected them all -- contribute
            # nothing for this cluster rather than resurfacing a rejected job.
        picks.sort(key=lambda p: p.get("embed_score", 0), reverse=True)
        for p in picks:
            p["_cluster_label"] = label if len(role_clusters) > 1 else None
        final_by_cluster[idx] = picks

    # Same fair-allocation logic as pooling/top-N: total output stays capped at
    # FINAL_PICKS, redistributed across clusters rather than added per cluster.
    final = _fair_allocate(final_by_cluster, engine.FINAL_PICKS)
    t0 = _lap("final_eval", t0)
    _progress(db, run, "Writing up top picks…")
    emit(f"[pipeline] final_evaluation returned {len(final)} picks across "
         f"{sum(1 for v in final_by_cluster.values() if v)} cluster(s)"
         + ("" if final else " -- nothing survived evaluation"))

    processed_ids = [j["_identity"] for j in pool]
    shown_ids = [f.get("_identity") for f in final if f.get("_identity")]
    warning = _compose_fallback_warning(role_clusters, fallback_notes)
    return final, harsh or bool(fallback_notes), (processed_ids, shown_ids), timings, warning


def _prune_previous_roles(db: Session, profile_id: int) -> None:
    """Second-search semantics: crossed and leftover unactioned 'new' (inbox)
    roles both age out to deleted. saved/applied are left untouched."""
    db.query(Role).filter(
        Role.profile_id == profile_id, Role.status.in_(["crossed", "new"])
    ).update({Role.status: "deleted"}, synchronize_session=False)
    db.commit()


def run_search_task(profile_id: int, run_id: int) -> None:
    """Background entry point. Owns its own DB session (runs off-request).
    Progress is logged with print()/emit(), which lands in the same console
    that's running `uvicorn app.main:app` (the backend terminal/window)."""
    db = SessionLocal()
    run = db.get(SearchRun, run_id)
    print(f"\n[pipeline] ── search run {run_id} for profile {profile_id} starting ──")
    try:
        import full_auto as engine  # lazy: pulls in crawl4ai only now
        engine.init_db()  # ensures gate_cache/jobs/profile_cache tables exist

        snap = build_snapshot(db, profile_id)

        # Engine's expensive-AI step reads the CV from a file; hand it our synthesis.
        with open(engine.CV_PATH, "w", encoding="utf-8") as f:
            f.write(snap["cv_text"])

        final, harsh, marks, timings, warning = asyncio.run(
            _run_engine_pipeline(
                engine, snap["engine_profile"], snap["weighted_text"], snap["cv_text_base"],
                db, profile_id, run
            )
        )

        # Prune only after the pipeline has succeeded, so a failed run leaves the
        # previous "new"/"crossed" roles intact instead of wiping them with nothing
        # to replace them.
        _prune_previous_roles(db, profile_id)

        for rank, entry in enumerate(final, start=1):
            db.add(Role(
                profile_id=profile_id,
                external_id=entry.get("_identity") or _external_id(engine, entry),
                title=entry.get("title", "Untitled role"),
                company=entry.get("company"),
                location=entry.get("location"),
                url=entry.get("url"),
                tags=_derive_tags(entry, snap["skills"], snap["seniority_label"]),
                salary_text=_salary_text(entry),
                source=entry.get("board"),
                fit_rank=rank,
                ai_analysis=_compose_analysis(entry),
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
        if warning:
            run.warning = warning
        run.message = (
            "No new roles found. Try widening your profile or location." if not final else None
        )
        db.commit()
        print(f"[pipeline] ── search run {run_id} done: {len(final)} results "
              f"(harsh={harsh}) ──\n")
    except Exception as e:  # never let the worker thread die silently
        import traceback
        traceback.print_exc()  # full stack trace to the backend console
        db.rollback()
        if run:
            run.status = "error"
            run.message = f"Search failed: {e!r}" if str(e) else f"Search failed: {type(e).__name__} (see backend console for traceback)"
            run.finished_at = datetime.utcnow()
            db.commit()
        print(f"[pipeline] ── search run {run_id} FAILED, see traceback above ──\n")
    finally:
        db.close()
