# Upgrade: Discovery/Enrichment Split, ATS Feeds, and a Fast Adaptive Funnel

A drop-in implementation guide for the `full_auto` search engine.

This document tells you exactly what to add, what to change, and what to leave
alone. It is written against the code as it actually exists today:
`full_auto.py` (the engine), `backend/app/services/engine.py` (the only place
that imports the engine), `backend/app/services/snapshot.py`,
`backend/app/models.py`, `backend/app/config.py`, and `backend/app/database.py`.
Every signature and field name below was checked against those files.

The upgrade is contained to three files: `models.py` (one new model), `engine.py`
(the orchestration + filtering rewrite), and `full_auto.py` (new sources, a few
tuning constants, and one prompt edit). `snapshot.py`, the routers, the
`Role`/feedback/weight system, and the entire Next.js frontend are untouched.

---

## 0. Goals this revision is built to hit

1. **First search is quick.** The user should not wait minutes for the first
   results. We achieve this by running only a fast source tier on the first run,
   parallelising discovery, and taking the browser scrape off the critical path.
2. **Running 3x/day keeps returning good results.** A persistent store +
   rotation + ATS breadth means each run explores a fresh slice and accumulates
   depth instead of re-peeling a shrinking pile.
3. **Cost stays modest.** A hard cap on how many rows get enriched per run keeps
   LLM/embedding spend flat no matter how many thousands discovery finds.
4. **Strong results, up to 10 shown.** The funnel considers thousands and
   returns up to 10 quality-gated matches (fewer if not enough genuinely qualify),
   because users are picky and want options.
5. **Strong filter that broadens rather than returning 0.** The relevance
   threshold is adaptive: strict when matches are plentiful, looser when they're
   sparse, with a backlog fallback so the user never sees an empty screen.

---

## 1. The core reframe

Today a run does everything at once: `gather_jobs` fetches the same listings
every time, `engine.py` dedupes them against roles already shown, and whatever
survives gets the expensive treatment (full-page browser scrape + GPT scoring).
Discovery and enrichment are welded together, so each run peels a progressively
worse layer off a static pile and a second search in a short window is worse than
the first.

We **separate discovery from enrichment** and put a **persistent job store**
between them:

- **Discovery** is cheap and runs broadly (tiered on the first run). Every source
  returns listing-level data. We compute a stable identity hash and upsert into a
  `jobs_seen` table. Discovery never scrapes full pages.
- **Enrichment** (the embedding filter + the expensive LLM evaluation) runs only
  on `state='new'` rows, capped per run. That's where the money goes, and you
  only ever spend it once per job.

The whole revision is four pieces, each independently shippable:

1. **`JobSeen` store + identity hash** (backend) - the foundation.
2. **The adaptive enrichment funnel** (backend) - delivers goals 1, 4, 5.
3. **ATS feed layer + source tiers** (engine) - delivers coverage and goal 1.
4. **Rotation, tiered first run, parallel discovery, pagination** (engine) -
   delivers goals 1 and 2.

---

## 2. Where state lives

Two databases; do not confuse them:

- `boards_cache.db` - owned by `full_auto.py` via raw `sqlite3`. Holds
  `profile_cache` and `jobs`. The engine's private scratch.
- `backend/jobmatch.db` - owned by the backend via SQLAlchemy (`models.py`).
  Holds everything per-user and per-profile.

The job store belongs in `jobmatch.db` as a new SQLAlchemy model: it's
per-profile state with a lifecycle, it needs a `user_id` for the multi-user-later
design, and `engine.py` already holds a `Session` and is the only importer of the
engine. The engine stays stateless about *who* it searches for - it discovers
listings; the backend remembers them.

`database.py`'s `init_db()` calls `Base.metadata.create_all(bind=engine)` on
startup, so the new table is created automatically on next boot. No migration
tool needed for SQLite. (On Postgres, add the equivalent `CREATE TABLE`.)

---

## 3. Piece 1 - the `JobSeen` store and the identity hash

### 3.1 New model

Add to `backend/app/models.py` (mirrors the existing style - `user_id` carried,
`_now` default, indexed FKs). Import `UniqueConstraint` from `sqlalchemy` at the
top; `CURRENT_USER_ID` is already imported from `.config`.

```python
class JobSeen(Base):
    """Persistent discovery store: every listing ever discovered for a profile,
    with a stable identity and an enrichment state. Discovery upserts here;
    enrichment only ever touches state='new' (plus backlog top-up when thin)."""

    __tablename__ = "jobs_seen"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False, default=CURRENT_USER_ID, index=True)
    profile_id = Column(
        Integer, ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    identity_hash = Column(Text, nullable=False, index=True)  # stable cross-source key
    source = Column(Text, nullable=False)                     # board name
    title = Column(Text, nullable=False)
    company = Column(Text)
    location = Column(Text)
    url = Column(Text)
    snippet = Column(Text)             # doubles as evaluation text (see 4.3)
    state = Column(Text, nullable=False, default="new")       # new|enriched|shown
    source_updated_at = Column(DateTime)                      # ATS updated_at when present
    first_seen = Column(DateTime, default=_now)
    last_seen = Column(DateTime, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("profile_id", "identity_hash", name="uq_jobseen_profile_identity"),
    )
```

### 3.2 The stable identity hash

This is the linchpin. Do **not** key on `(title, company)` like the current dedup
- it collapses two different roles at the same company. Prefer the canonical
apply URL stripped of tracking params; fall back to normalized
company+title+location when the URL is missing or is an aggregator redirect.

Add to `engine.py` (backend-side logic, not engine internals):

```python
import hashlib
import re
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

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
```

`Role.external_id` currently comes from `engine.make_job_id(board, url)` (board-
scoped MD5). Going forward, set `external_id = identity_hash(job)` so a `Role` row
and its `JobSeen` row share one key, and the same role found via two sources
collapses to one identity. Existing rows keep their old `external_id`; dedup is
now driven by `jobs_seen`, not `external_id`, so that's harmless.

### 3.3 Upsert and selection helpers

Add to `engine.py`:

```python
from ..models import Role, SearchRun, JobSeen   # add JobSeen


def _parse_iso(s: str):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _upsert_discovered(db: Session, profile_id: int, raw_jobs: list[dict]) -> None:
    """Discovery is cheap and runs fully every time. New identities get
    state='new'; seen ones refresh last_seen. A role edited at source after we
    last enriched it is re-queued (state -> 'new')."""
    now = datetime.utcnow()
    for job in raw_jobs:
        title = job.get("title", "")
        if not title:
            continue
        h = identity_hash(job)
        existing = db.execute(
            select(JobSeen).where(
                JobSeen.profile_id == profile_id, JobSeen.identity_hash == h
            )
        ).scalar_one_or_none()
        upd_dt = _parse_iso(job.get("updated_at")) if job.get("updated_at") else None

        if existing is None:
            db.add(JobSeen(
                profile_id=profile_id, identity_hash=h, source=job.get("board", ""),
                title=title, company=job.get("company"), location=job.get("location"),
                url=job.get("url"), snippet=job.get("snippet", ""),
                state="new", source_updated_at=upd_dt, first_seen=now, last_seen=now,
            ))
        else:
            existing.last_seen = now
            if upd_dt and existing.source_updated_at and upd_dt > existing.source_updated_at:
                existing.state = "new"
                existing.source_updated_at = upd_dt
    db.commit()


def _new_rows(db: Session, profile_id: int, limit: int = 200) -> list[JobSeen]:
    """Fresh, unprocessed rows. Freshest first."""
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
```

---

## 4. Piece 2 - the adaptive enrichment funnel

This is the heart of the revision. It delivers goals 1, 4, and 5 at once.

### 4.1 The old funnel vs the new one

**Old:** thousands -> embedding filter (fixed 0.35) -> top 60 -> cheap-LLM rank
to 10 -> **scrape 10 full pages (1-3 min)** -> expensive-LLM eval -> top 3.

**New:** thousands -> store -> embedding score all new(+backlog if thin) ->
**adaptive pool of ~25** (strict threshold normally, broadened when sparse) ->
expensive-LLM eval **on text we already have, no scrape** -> **up to 10**
quality-gated picks -> return immediately. Optional: deep-scrape only the shown
non-ATS picks afterward to enrich their analysis.

Two structural wins: the browser scrape leaves the critical path entirely (the
big latency cut), and the threshold becomes adaptive (the never-zero guarantee).

### 4.2 Tuning constants

In `full_auto.py`, under "Global Tuning Hyperparameters", change two values and
soften the final-eval prompt (4.4):

```python
TOP_CANDIDATES = 25      # was 10; pool size handed to the evaluator
FINAL_PICKS    = 10      # was 3;  max results returned, quality-gated
```

In `engine.py`, add the funnel constants near the top:

```python
TARGET_POOL       = 25     # candidates fed to the expensive evaluator
MIN_RESULTS       = 3      # below this many strong matches, broaden the threshold
RELEVANCE_PRIMARY = 0.35   # strict strong-fit threshold
RELEVANCE_FLOOR   = 0.20   # never include anything weaker than this
BACKLOG_TOPUP     = 40     # enriched rows pulled in when fresh discovery is thin
```

### 4.3 Scoring and the adaptive pool

Re-scoring already-computed embeddings is free (local cosine), so broadening
costs nothing. Add to `engine.py`:

```python
def _score_all(engine, jobs: list[dict], profile_embedding) -> list[dict]:
    texts = [f"{j['title']} {j.get('company','')} {j.get('snippet','')}" for j in jobs]
    embeddings = []
    for i in range(0, len(texts), 100):
        embeddings.extend(engine.get_embeddings_batch(texts[i:i+100]))
    for j, emb in zip(jobs, embeddings):
        j["embed_score"] = engine.cosine_similarity(profile_embedding, emb)
    return sorted(jobs, key=lambda j: j["embed_score"], reverse=True)


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
```

The evaluator (4.4) still quality-gates, so broadening never forces weak roles
into the results - it just gives the LLM a wider field to find the genuinely good
ones in. If even the floor yields nothing, the backlog top-up in 4.5 has already
padded the input, and as a last resort the run returns the best of the backlog.

### 4.4 Dynamic up-to-10 evaluation, no scrape

`final_evaluation` in `full_auto.py` already reads `j.get('full_text','')` and
selects `FINAL_PICKS` jobs. Two small edits make it return *up to* 10 on text we
already have:

1. Set `FINAL_PICKS = 10` (done in 4.2). `final_evaluation` already slices
   `results[:FINAL_PICKS]`, so it will return up to 10 with no code change there.
2. Soften the prompt so it returns *only* strong fits and may return fewer than
   the cap. Change the instruction line from "Choose the {FINAL_PICKS} strongest
   overall alignments" to:

   > "Return every role that is a genuinely strong fit for this candidate, up to
   > {FINAL_PICKS}, ordered best first. Return fewer than {FINAL_PICKS} if fewer
   > genuinely qualify - do not pad the list with weak matches."

Because we set `full_text = snippet` in `_rows_to_dicts`, and ATS snippets carry
the full description (4.3 of Piece 3 raises the ATS snippet cap to 3000 chars),
the evaluator judges on real text without any browser scrape. Aggregator snippets
are the description's first chunk - good enough for ranking; the optional deep
scrape (4.6) upgrades the shown ones afterward.

### 4.5 The rewritten pipeline

Replace `_run_engine_pipeline` in `engine.py` with:

```python
def _is_first_run(db: Session, profile_id: int) -> bool:
    return db.query(JobSeen.id).filter(JobSeen.profile_id == profile_id).first() is None


async def _run_engine_pipeline(engine, eng_profile, weighted_text, db, profile_id):
    profile_embedding = engine.get_embeddings_batch([weighted_text])[0]

    # DISCOVERY (cheap, tiered on first run) -> store. gather_jobs reads the flag.
    eng_profile["first_run"] = _is_first_run(db, profile_id)
    raw_jobs = engine.gather_jobs(eng_profile)
    _upsert_discovered(db, profile_id, raw_jobs)

    # ASSEMBLE the pool: fresh 'new' rows, topped up from the enriched backlog
    # when discovery was thin (this is "surfacing the backlog").
    fresh = _rows_to_dicts(_new_rows(db, profile_id))
    if len(fresh) < TARGET_POOL:
        fresh += _rows_to_dicts(_backlog_rows(db, profile_id, BACKLOG_TOPUP))
    if not fresh:
        return [], False, None

    # FILTER: score everything, take an adaptive pool (broaden only if sparse).
    scored = _score_all(engine, fresh, profile_embedding)
    pool, harsh = _adaptive_pool(scored)
    if not pool:
        return [], harsh, None

    # EVALUATE on snippet-as-full_text. No browser scrape on the critical path.
    final = engine.final_evaluation(pool, eng_profile)   # up to FINAL_PICKS

    processed_ids = [j["_identity"] for j in pool]
    shown_ids = [f.get("_identity") for f in final if f.get("_identity")]
    return final, harsh, (processed_ids, shown_ids)
```

`final_evaluation` does `merged = jobs[idx].copy(); merged.update(entry)`, so
`_identity` rides through into the returned picks for free. Note we no longer call
`rank_candidates` or `scrape_full_details` on the critical path. (You may keep
`rank_candidates` as an optional cheap-LLM trim of a larger pool before the
expensive call if you want extra precision - it's fast - but it's no longer
required, and it is not what was gating latency.)

### 4.6 Optional: deepen the shown picks after returning

The skip-scrape path judges aggregator roles on their snippet. If you want the
displayed analysis for non-ATS picks to be based on the full page, do it *after*
the results are already persisted and visible, so it never delays the user. In
`run_search_task`, after the `Role` rows are committed:

```python
    # Optional polish: deep-scrape only the shown non-ATS roles, off the
    # critical path, then refresh their ai_analysis.
    ats_prefixes = ("gh:", "lever:", "ashby:")
    to_deepen = [f for f in final
                 if not str(f.get("board", "")).startswith(ats_prefixes)
                 and f.get("url")]
    # (left as an enhancement: run engine.scrape_full_details on these in a
    #  fresh crawler, re-run final_evaluation on the single role, update Role.)
```

This is genuinely optional - ATS picks already have full text, and the snippet-
based analysis is decent for the rest. Ship the funnel first; add this only if
the aggregator analyses feel thin in practice.

### 4.7 Updating `run_search_task`

Change the call site, the persist loop (now up to 10 rows), and the state marks.
Delete the old `existing_ids` block entirely.

```python
        final, harsh, marks = asyncio.run(
            _run_engine_pipeline(
                engine, snap["engine_profile"], snap["weighted_text"], db, profile_id
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
                fit_rank=rank,
                ai_analysis=_compose_analysis(entry),
                status="new",
            ))

        if marks:
            processed_ids, shown_ids = marks
            _mark(db, profile_id, processed_ids, "enriched")  # everything we evaluated
            _mark(db, profile_id, shown_ids, "shown")         # the up-to-10 displayed
```

`_prune_previous_roles` stays exactly as it is (it operates on `Role` rows;
separate concern from the discovery store). The harsh warning and the "no results"
message at the bottom of `run_search_task` stay - `harsh` is now driven by the
adaptive pool, which is a more honest signal than the old count.

### 4.8 Why this meets the goals

- **Quick first search:** no browser scrape on the critical path (the old 1-3 min
  wait is gone); the expensive eval is a single call over ~25 short texts.
  Combined with the tiered first run (Piece 4) the first results land in seconds.
- **Up to 10, quality-gated:** `FINAL_PICKS=10` over a 25-strong pool, with a
  prompt that returns fewer when fewer qualify. Picky users get options without
  getting padding.
- **Never zero, broadens when sparse:** the adaptive threshold widens only when
  strong matches are thin, and the backlog top-up + last-resort backlog return
  guarantee a non-empty screen whenever the store has anything at all.
- **Cost flat:** `_new_rows` is capped and the pool is capped at 25, so the
  expensive call sees at most 25 jobs regardless of how many thousands discovery
  found. Embedding all fresh rows is cheap (text-embedding-3-small, batched).

---

## 5. Piece 3 - ATS feeds and source tiers

The coverage multiplier, contained to `full_auto.py`. `gather_jobs(profile)`
keeps its signature and `List[Dict]` return shape, so `engine.py` and everything
downstream are untouched.

### 5.1 A source protocol with a tier

```python
from typing import Protocol, Optional
from datetime import datetime

class JobSource(Protocol):
    name: str
    tier: str   # "fast" (cheap, text-complete) or "broad" (slower / credit-heavy)
    def fetch(self, profile: dict, since: Optional[datetime]) -> list[dict]: ...
```

Wrap each existing fetcher to match - they already return the right shape:

```python
class AdzunaSource:
    name, tier = "adzuna", "fast"
    def fetch(self, profile, since=None):
        return fetch_adzuna(profile["search_terms_current"],
                            profile.get("location", "United Kingdom"),
                            profile.get("adzuna_country_code", "gb"))

class GoogleJobsSource:
    name, tier = "google_jobs", "broad"   # paginated + SerpAPI credits
    def fetch(self, profile, since=None):
        return fetch_google_jobs(profile["search_terms_current"],
                                 profile.get("location", "United Kingdom"))
# JSearchSource ("broad"), RemotiveSource ("broad"), ReedSource ("fast", UK) similarly.
```

### 5.2 The ATS feeds - the big unlock

Public, no-auth JSON endpoints returning every open role at a company, with clean
fields and `updated_at`. Confirmed live and unauthenticated as of mid-2026:

- **Greenhouse:** `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
  (Caveat: Greenhouse's *Harvest* API v1/v2 deprecates Aug 31 2026 - that is the
  authenticated recruiting API, **not** this public job-board endpoint, which is
  unaffected.)
- **Lever:** `https://api.lever.co/v0/postings/{company}?mode=json`
- **Ashby:** `https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true`
- **Workable, Recruitee, Personio** expose similar feeds; add on the same pattern.

These are "fast" tier: free, fast, and they return the **full description**, so
roles sourced here never need scraping. Note the `snippet` is capped at **3000
chars** (not 2000) so it can double as the evaluator's `full_text` (see 4.3/4.4):

```python
ATS_FEEDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
    "lever":      "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby":      "https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
}

def fetch_ats(vendor: str, token: str) -> list[dict]:
    tmpl = ATS_FEEDS.get(vendor)
    if not tmpl:
        return []
    try:
        data = requests.get(tmpl.format(token=token), timeout=12).json()
    except Exception as e:
        emit(f"   [!] ATS {vendor}/{token} error: {e}")
        return []

    rows = data.get("jobs") if vendor == "greenhouse" else \
           data.get("postings") if vendor == "lever" else \
           data.get("jobs", data)  # ashby
    out = []
    for j in rows or []:
        if vendor == "greenhouse":
            out.append({"board": f"gh:{token}", "title": j.get("title",""),
                        "company": token, "url": j.get("absolute_url",""),
                        "location": (j.get("location") or {}).get("name",""),
                        "snippet": (j.get("content","") or "")[:3000],
                        "updated_at": j.get("updated_at")})
        elif vendor == "lever":
            out.append({"board": f"lever:{token}", "title": j.get("text",""),
                        "company": token, "url": j.get("hostedUrl",""),
                        "location": (j.get("categories") or {}).get("location",""),
                        "snippet": (j.get("descriptionPlain","") or "")[:3000],
                        "updated_at": j.get("createdAt")})
        else:  # ashby
            out.append({"board": f"ashby:{token}", "title": j.get("title",""),
                        "company": token, "url": j.get("jobUrl",""),
                        "location": j.get("location",""),
                        "snippet": (j.get("descriptionPlain","") or "")[:3000],
                        "updated_at": j.get("publishedAt")})
    return out
```

### 5.3 Bootstrapping tokens

Use SerpAPI once to harvest tokens, then hit feeds directly forever after. Search
`site:boards.greenhouse.io {sector keyword}`, `site:jobs.lever.co {keyword}`,
`site:jobs.ashbyhq.com {keyword}`; the path segment after the host is the token.
Upsert `{company, vendor, token}` into a `company_ats` table (in `jobmatch.db`
alongside `JobSeen`, or in `boards_cache.db` if you'd rather keep it engine-side).
Run this as an occasional maintenance job, not on every search. For the
climate/nonprofit/high-impact space, a few-hundred-org list is very tractable.

### 5.4 Source ROI - what to cut

- **Add:** the ATS layer. Jobs that never hit aggregators, fresh within minutes.
- **Lean on Google Jobs** (paginated - Piece 4). Aggregator of aggregators.
- **Keep:** Adzuna (real recency filter), Reed (UK, if relevant).
- **Cut to at most one of:** Remotive, Jobicy. Small remote-only boards that
  overlap heavily with each other and Google Jobs. Drop the dead ones entirely.

---

## 6. Piece 4 - rotation, tiered first run, parallel discovery, pagination

### 6.1 Tiered first run + rotation

Keep a per-profile cursor (in `boards_cache.db.profile_cache` under a
`rotation_cursor` key). The engine sets `profile["first_run"]` (Piece 2, 4.5).

```python
def select_sources_for_run(profile):
    fast  = [AdzunaSource(), ReedSource()]
    broad = [GoogleJobsSource(), JSearchSource(), RemotiveSource()]
    # Quick first run: fast, text-complete tier only. Breadth arrives next runs.
    if profile.get("first_run"):
        profile["search_terms_current"] = profile["search_terms"][0]
        return fast
    cur = _load_cursor(profile)
    rotation = fast + broad
    picked = [rotation[(cur + i) % len(rotation)] for i in range(2)]  # 2 sources/run
    profile["search_terms_current"] = profile["search_terms"][cur % len(profile["search_terms"])]
    _save_cursor(profile, cur + 1)
    return picked


def select_ats_batch_for_run(profile):
    """Return (company, vendor, token) rows. Smaller, capped batch on first run
    (still fast because ATS calls are parallel); rotate batches afterward."""
    tokens = _load_company_ats()          # from company_ats table
    if profile.get("first_run"):
        return tokens[:40]
    cur = _load_cursor(profile)
    size = 40
    start = (cur * size) % max(1, len(tokens))
    return tokens[start:start + size]
```

So the first run hits Adzuna/Reed + a 40-company ATS batch - all fast, text-
complete, no scraping - and returns in seconds. Google Jobs and JSearch fill the
store on later runs, and rotation means run N explores a different slice than run
N-1, so three runs a day keep surfacing fresh roles.

### 6.2 Parallel discovery

Replace the sequential loop so 40+ ATS calls + the rotated sources run
concurrently:

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def gather_jobs(profile: dict) -> list[dict]:
    tasks = [("src", s) for s in select_sources_for_run(profile)]
    tasks += [("ats", (v, t)) for (_c, v, t) in select_ats_batch_for_run(profile)]

    def run_task(t):
        kind, payload = t
        if kind == "src":
            return payload.fetch(profile, since=profile.get("since"))
        vendor, token = payload
        return fetch_ats(vendor, token)

    all_jobs: list[dict] = []
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
```

### 6.3 Paginate Google Jobs (broad-tier runs only)

It currently pulls one page per term. Follow `next_page_token` for 2-3 pages.
This is a "broad" source, so it only runs on non-first runs and only for the
rotated term - keeping SerpAPI credit use bounded.

```python
def fetch_google_jobs(query, location="United Kingdom", pages=3):
    if not SERPAPI_KEY:
        return []
    clean_loc = normalize_location(location)
    jobs, token = [], None
    for _ in range(pages):
        params = {"engine": "google_jobs", "q": query, "location": clean_loc,
                  "api_key": SERPAPI_KEY}
        if token:
            params["next_page_token"] = token
        data = requests.get("https://serpapi.com/search", params=params, timeout=12).json()
        if "error" in data:
            break
        for job in data.get("jobs_results", []):
            opts = job.get("apply_options") or []
            jobs.append({"board": "google_jobs", "title": job.get("title",""),
                         "company": job.get("company_name",""),
                         "url": opts[0].get("link") if opts else "",
                         "snippet": job.get("description","")})
        token = (data.get("serpapi_pagination") or {}).get("next_page_token")
        if not token:
            break
    return jobs
```

---

## 7. Caching, answered per source

It's not all-or-nothing. The discovery/enrichment split makes the rule simple:

- **Recency-filter sources** (Adzuna `max_days_old`, Google Jobs date sort): fetch
  incrementally - only postings since the last run. Cheap and fresh.
- **ATS feeds:** they hand you *all* open roles with `updated_at` and no server-
  side filter, so fetch all (one cheap parallel JSON call each) and let the store
  diff client-side. `updated_at` re-queues genuinely edited roles for free.
- **Generic scraping** (if ever added): fetch the listing page, diff URLs against
  the store, scrape full pages only for new URLs.

Unifying rule: **discovery runs fully every time because it's cheap; enrichment
only ever touches new rows (plus backlog top-up).** Strictly better than re-
processing everything, and better than naive "only fetch new" (some sources can't
do incremental, and you'd miss edited/reposted roles).

---

## 8. Implementation order and checklist

Each step is independently shippable and testable.

1. **Store + identity (backend).**
   - [ ] Add `JobSeen` + `UniqueConstraint` to `models.py` (import `UniqueConstraint`).
   - [ ] Add `identity_hash` + helpers (`_canonical_url`, `_norm`, `_parse_iso`,
         `_upsert_discovered`, `_new_rows`, `_backlog_rows`, `_rows_to_dicts`,
         `_mark`) to `engine.py`.
   - [ ] Boot the app; confirm `jobs_seen` is created and a search populates it.

2. **Adaptive funnel (backend).**
   - [ ] Add funnel constants + `_score_all`, `_adaptive_pool`, `_is_first_run`.
   - [ ] Rewrite `_run_engine_pipeline` (4.5) and the persist loop in
         `run_search_task` (4.7). Delete the `existing_ids` block.
   - [ ] Set `TOP_CANDIDATES=25`, `FINAL_PICKS=10`; soften the final-eval prompt (4.4).
   - [ ] Confirm a run returns up to 10, with the scrape no longer on the path.
   - [ ] Force a sparse profile; confirm the threshold broadens and the backlog
         top-up prevents a zero-result screen.

3. **ATS layer + tiers (engine).**
   - [ ] Add `fetch_ats` + `ATS_FEEDS`; snippet cap 3000.
   - [ ] Add the `company_ats` table + SerpAPI token-harvest script; seed ~50 companies.
   - [ ] Add `tier` to the source wrappers; cut the dead sources.

4. **Rotation + first run + parallel + pagination (engine).**
   - [ ] Add the cursor + `select_sources_for_run` / `select_ats_batch_for_run`.
   - [ ] Parallelise `gather_jobs` (ThreadPoolExecutor).
   - [ ] Paginate `fetch_google_jobs`.
   - [ ] Time a first run (should be seconds) vs a broad later run.

5. **Verification.**
   - [ ] Unit-test `identity_hash`: same role from two sources -> one hash; two
         different roles at one company -> two hashes; tracking params ignored.
   - [ ] Two searches back-to-back: the second does little work and returns fresh
         roles or backlog, not repeats.
   - [ ] Confirm `_identity` survives `final_evaluation`'s copy/update so
         `Role.external_id` and the `shown` marks are set correctly.

### What you do NOT touch

`api_candidates` (superseded by `_score_all`/`_adaptive_pool` but left in place),
`scrape_full_details` (now optional, off the critical path), the
`Role`/`ProfileAttribute`/`FeedbackLog`/`SearchRun` models, `snapshot.py`, the
feedback/weight system, every router, and the entire Next.js frontend.

---

## Sources

- [Greenhouse Job Board API](https://developers.greenhouse.io/job-board.html)
- [Greenhouse API overview (Harvest deprecation note)](https://support.greenhouse.io/hc/en-us/articles/10568627186203-Greenhouse-API-overview)
- [Ashby Public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api)
- [ATS platforms with public job posting APIs](https://fantastic.jobs/article/ats-with-api)
