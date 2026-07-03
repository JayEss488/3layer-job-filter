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
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models import Role, SearchRun, JobSeen
from .snapshot import build_snapshot
from .moderation import filter_blocked, get_blocked_domains
from .sources import (
    counts_from_breakdown,
    get_disabled,
    get_full_scrape_enabled,
    save_last_run_counts,
)

# ── Adaptive funnel tuning ───────────────────────────────────────────────────
TARGET_POOL       = 25     # candidates fed to the expensive evaluator
MIN_RESULTS       = 3      # below this many strong matches, broaden the threshold
RELEVANCE_PRIMARY = 0.35   # strict strong-fit threshold
RELEVANCE_FLOOR    = 0.20  # never include anything weaker than this
BACKLOG_TOPUP     = 40     # enriched rows pulled in when fresh discovery is thin
STORE_SCORE_CAP   = 6000   # max 'new' rows relevance-scored per run (whole store)


def _external_id(engine, job: dict) -> str:
    return engine.make_job_id(job.get("board", ""), job.get("url", ""))


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
    parts = [entry.get("summary", "").strip()]
    for r in entry.get("match_reasons", []) or []:
        parts.append(f"✓ {r}")
    for c in entry.get("concerns", []) or []:
        parts.append(f"⚠ {c}")
    return "\n".join(p for p in parts if p)


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
    """JobSeen -> the dict shape the engine's filter/eval expect. snippet doubles
    as full_text so the evaluator judges on text we already have (no scrape)."""
    return [{
        "board": r.source, "title": r.title, "company": r.company or "",
        "location": r.location or "", "url": r.url or "",
        "snippet": r.snippet or "", "full_text": r.snippet or "",
        "_identity": r.identity_hash,
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


def _score_rows(engine, rows: list[JobSeen], profile_embedding) -> list[dict]:
    """Cosine each row's cached embedding against the profile (free, local), then
    return engine-shaped dicts sorted by score desc with embed_score + _identity."""
    scored: list[tuple[float, JobSeen]] = []
    for r in rows:
        try:
            emb = json.loads(r.embedding) if r.embedding else None
        except (ValueError, TypeError):
            emb = None
        score = engine.cosine_similarity(profile_embedding, emb) if emb else 0.0
        scored.append((score, r))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for score, r in scored:
        d = _rows_to_dicts([r])[0]
        d["embed_score"] = score
        out.append(d)
    return out


def _adaptive_pool(scored: list[dict]) -> tuple[list[dict], bool]:
    """Strict when matches are plentiful, broaden only when sparse.
    Returns (pool, harsh) where harsh means we had to drop below the strict
    threshold to find enough."""
    strong = [j for j in scored if j["embed_score"] >= RELEVANCE_PRIMARY]
    if len(strong) >= TARGET_POOL:
        return strong[:TARGET_POOL], False          # plenty; keep it strict
    if len(strong) >= MIN_RESULTS:
        return strong, False                         # fewer than 25 but enough to choose from
    broadened = [j for j in scored if j["embed_score"] >= RELEVANCE_FLOOR]
    return broadened[:TARGET_POOL], True             # sparse: broaden + flag


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


def _progress(db: Session, run: SearchRun, message: str) -> None:
    run.message = message
    db.commit()


async def _run_engine_pipeline(engine, eng_profile, weighted_text, db, profile_id, run: SearchRun):
    emit = engine.emit  # prints to the backend's own console (see run_search_task)
    timings: dict[str, float] = {}
    t0 = time.monotonic()

    def _lap(phase: str, since: float) -> float:
        timings[phase] = round(time.monotonic() - since, 2)
        return time.monotonic()

    profile_embedding = engine.get_embeddings_batch([weighted_text])[0]

    # DISCOVERY (cheap, tiered on first run) -> store. gather_jobs reads the flag.
    eng_profile["first_run"] = _is_first_run(db, profile_id)
    eng_profile["disabled_sources"] = get_disabled(db)  # per-source toggle (workstream D)
    emit(f"[pipeline] discovery start (first_run={eng_profile['first_run']}, "
         f"disabled={sorted(eng_profile['disabled_sources'])}, "
         f"terms={eng_profile.get('search_terms', [])[:5]})")
    _progress(db, run, "Searching job boards…")
    raw_jobs = engine.gather_jobs(eng_profile)
    t0 = _lap("discovery", t0)
    raw_jobs, n_blocked = filter_blocked(raw_jobs, get_blocked_domains(db))
    if n_blocked:
        emit(f"[pipeline] spam-domain blocklist dropped {n_blocked} listing(s)")
    breakdown = _board_breakdown(raw_jobs)
    emit(f"[pipeline] discovery returned {len(raw_jobs)} raw listings by board: {breakdown}")
    save_last_run_counts(db, counts_from_breakdown(breakdown))  # for the settings screen

    country_codes = eng_profile.get("country_codes") or []
    if country_codes:
        before = len(raw_jobs)
        raw_jobs = _filter_by_country(engine, raw_jobs, country_codes)
        emit(f"[pipeline] country filter {country_codes}: {before} -> {len(raw_jobs)} listings")

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
        return [], False, None, timings

    # FILTER: embed (cached) + cosine-score the whole set, take an adaptive pool.
    _progress(db, run, f"Found {len(rows)} jobs, scoring…")
    n_embedded = _ensure_embeddings(engine, db, rows)
    if n_embedded:
        emit(f"[pipeline] embedded {n_embedded} new rows (cached for future runs)")
    t0 = _lap("embed", t0)
    scored = _score_rows(engine, rows, profile_embedding)
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
    top_score = scored[0]["embed_score"] if scored else 0.0
    above_primary = sum(1 for j in scored if j["embed_score"] >= RELEVANCE_PRIMARY)
    emit(f"[pipeline] scored {len(scored)} candidates | top_score={top_score:.3f} | "
         f">= RELEVANCE_PRIMARY({RELEVANCE_PRIMARY})={above_primary}")

    pool, harsh = _adaptive_pool(scored)
    emit(f"[pipeline] adaptive pool size={len(pool)} (harsh/broadened={harsh})")
    if not pool:
        emit(f"[pipeline] STOP: no candidates above RELEVANCE_FLOOR({RELEVANCE_FLOOR}) -> 0 results")
        return [], harsh, None, timings

    # EVALUATE. When full-page scraping is enabled (default), rank the pool down
    # to TOP_CANDIDATES and read each job's real page first, so the final LLM
    # judges fit against the actual posting text (seniority/experience
    # requirements included) instead of a short API snippet.
    to_evaluate = pool
    if get_full_scrape_enabled(db):
        _progress(db, run, "Ranking best matches…")
        ranked = engine.rank_candidates(pool, eng_profile)
        t0 = _lap("rank", t0)
        _progress(db, run, "Reading full job pages…")
        browser_config = engine.BrowserConfig(
            headless=True, verbose=False, viewport_width=1280, viewport_height=800,
            user_agent_mode="random",
        )
        async with engine.AsyncWebCrawler(config=browser_config) as crawler:
            to_evaluate = await engine.scrape_full_details(ranked, crawler)
        t0 = _lap("scrape", t0)
    else:
        emit("[pipeline] full-page scraping disabled in settings; evaluating on snippets")

    _progress(db, run, "Final AI review…")
    emit(f"[pipeline] sending {len(to_evaluate)} candidates to final_evaluation "
         f"(LLM, cap={engine.FINAL_PICKS})")
    final = engine.final_evaluation(to_evaluate, eng_profile)   # up to FINAL_PICKS
    t0 = _lap("final_eval", t0)
    _progress(db, run, "Writing up top picks…")
    emit(f"[pipeline] final_evaluation returned {len(final)} picks"
         + ("" if final else " -- LLM judged none as a genuinely strong fit"))

    processed_ids = [j["_identity"] for j in pool]
    shown_ids = [f.get("_identity") for f in final if f.get("_identity")]
    return final, harsh, (processed_ids, shown_ids), timings


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

        snap = build_snapshot(db, profile_id)

        # Engine's expensive-AI step reads the CV from a file; hand it our synthesis.
        with open(engine.CV_PATH, "w", encoding="utf-8") as f:
            f.write(snap["cv_text"])

        _prune_previous_roles(db, profile_id)

        final, harsh, marks, timings = asyncio.run(
            _run_engine_pipeline(
                engine, snap["engine_profile"], snap["weighted_text"], db, profile_id, run
            )
        )

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
        if harsh:
            run.warning = (
                "Your filters look strict — few roles matched. Showing the "
                "best available anyway; loosen salary/location for more."
            )
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
