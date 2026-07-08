# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

JobMatch: an AI job-matching app that parses a CV into structured "memory" (profile
attributes), runs a multi-stage search/ranking pipeline against several job boards and
ATS vendors, and learns from tick/cross feedback (no retraining — just weight nudges).
Single-user prototype (`user_id` hardcoded, but present everywhere so multi-user auth is
a drop-in later).

Stack: FastAPI + SQLAlchemy (SQLite) backend, Next.js (App Router) + TanStack Query
frontend, wrapping a pre-existing standalone search engine (`full_auto.py`).

## Commands

Backend (from repo root, using the committed `venv/`):
```
venv/Scripts/python -m pip install -r backend/requirements.txt   # API deps
venv/Scripts/python -m pip install -r requirements.txt           # engine deps (crawl4ai, playwright, numpy, openai)
venv/Scripts/python -m playwright install chromium                # once, for live full-page scraping
cd backend && ../venv/Scripts/python -m uvicorn app.main:app --reload --port 8000
```
API docs at `http://127.0.0.1:8000/docs`. SQLite db (`backend/jobmatch.db`) and tables
are created automatically on startup (see `database.py::init_db`, not Alembic — see below).

Frontend:
```
cd frontend
npm install
npm run dev      # http://localhost:3000
npm run build
npm run lint
```
`frontend/.env.local` sets `NEXT_PUBLIC_API_URL` (defaults to the backend above).

Convenience launcher: `start.bat` / `start.ps1` opens backend + frontend each in their
own PowerShell window.

Seed the ATS company-board store (optional, runs automatically once on first boot if
`company_ats` is empty — see `backend/app/services/seed.py`):
```
venv/Scripts/python seed_ats.py
venv/Scripts/python seed_ats.py --harvest "climate" "fintech" "data engineering"   # + a site: search harvest, costs API credits
```

There is no test suite in this repo (no pytest/jest config; the one file under
`inactive components/` is not wired to anything) and no linter beyond Next's default
`next lint`. Don't assume either exists when asked to "run the tests."

All API keys (OpenAI, Reed, Adzuna, serper.dev, SerpAPI, RapidAPI) live in the
repo-root `.env`, loaded explicitly by `backend/app/config.py` regardless of cwd. There
is no `.env.example`.

## Architecture

### The engine boundary — read this before touching search logic

`full_auto.py` (repo root) is a pre-existing, working 6-phase search engine, originally a
standalone script. **`backend/app/services/engine.py` is the only module that imports
it** (lazily, so the API boots without crawl4ai/playwright installed). Don't casually
refactor `full_auto.py`'s internals; it still contains its own separate legacy CLI path
(`run_pipeline`/`main()`, a single blended-embedding pipeline with no clustering) that
predates and duplicates the backend-used functions — that path is dead weight from the
backend's perspective but is kept working for standalone `python full_auto.py` runs.
Its only logging mechanism is `emit()` (prints, and pushes to a queue if one's set via
`set_log_queue`) — grep for `emit(` to trace what the pipeline is actually doing rather
than expecting structured logs.

`legacy/` is an archived pre-rewrite Flask prototype — not part of the running app.

### Data model (`backend/app/models.py`)

- **`Profile`** → **`ProfileAttribute`**: the core "memory." Every editable fact about a
  candidate (past_role, skill, experience, seniority, target_role, salary, location,
  country, location_scope, custom) is one typed row with a `weight` and a `confirmed`
  flag, not a blob — see `config.ATTRIBUTE_TYPES`/`ATTRIBUTE_DIRECTION`. `target_role`
  (what they want) is deliberately kept separate from `past_role` (what they've done) so
  the engine can weight them differently.
- **`Role`**: one row per listing surfaced to the user in a given search run, with a
  lifecycle `status` (`new → saved/crossed/ignored → applied → deleted`) driving both the
  UI tabs and re-search semantics (crossed/leftover-new roles are pruned before each new
  run; saved/applied persist).
- **`JobSeen`**: the *persistent discovery store* — every listing ever seen for a
  profile, separate from `Role` (which is just what got shown). Discovery upserts here
  (deduped by `identity_hash`); scoring/backlog top-up reads from here across runs so a
  thin fresh-discovery run can still surface previously-seen candidates. It caches the
  three expensive per-job artifacts so a resurfacing job re-does none of them: the
  `embedding` (computed once per job ever), the scraped `full_text` (persisted only when
  a real fetch beat the snippet, so blocked pages still retry), and the final-AI verdict
  (`eval_verdict` = strong/backup/reject + `eval_analysis` JSON, keyed by
  `eval_signature` = a hash of the cluster CV so it's reused only while the profile is
  unchanged). A re-queued row (posting changed at source) clears `full_text`/`eval_*` so
  it's re-scraped and re-judged.
- **`CompanyATS`**: cached vendor/token registry for the ATS discovery tier (Greenhouse,
  Lever, Ashby, Workable, Recruitee, Personio), populated by `seed_ats.py` (curated,
  live-validated) and grown by `harvest.py`'s occasional `site:`-search harvest — this
  list itself is cached fine; it's the *job data fetched from* these companies each run
  that needed a TTL guard (see below).
- **`FeedbackLog`**: append-only tick/cross/ignore/apply audit trail.
- **`SearchRun`**: one row per kicked-off search; drives `/search/status` polling and the
  daily search cap.
- **`Setting`**: generic key/value store (`profile_id=NULL` = global) for source
  toggles, the domain blocklist, the full-scrape toggle, and per-profile freshness
  markers (ATS-harvest keyword hash, ATS-batch last-fetched timestamp).

Schema changes are **not** Alembic — `database.py::_migrate_columns()` is a hand-rolled,
idempotent `ALTER TABLE ADD COLUMN` dict. Add new columns there.

### The search pipeline (the part that touches the most files)

1. **`backend/app/services/snapshot.py::build_snapshot`** turns a profile's attributes
   into the engine's input contract. It clusters the profile's `target_role` values into
   1-3 "streams" by underlying job function (`cluster_target_roles`, one LLM call,
   strongly biased toward a single cluster — see the prompt before assuming clustering
   is broken; over-splitting was a real bug fixed once already). Each cluster gets its
   own weighted embedding text (`_weighted_text`, scoped to just that cluster's target
   roles) and its own scoped synthetic-CV text (`cv_text_for_cluster`) for the final
   judge — this is what lets a candidate targeting two unrelated fields get judged fairly
   on each, instead of being averaged into a fit-for-neither blend.
2. **`backend/app/services/engine.py::_run_engine_pipeline`** orchestrates, per run:
   discovery (`full_auto.gather_jobs` — Reed/Adzuna/Google-Jobs-via-serper.dev/JSearch/
   Remotive + the ATS vendor batch; search terms sent to the board APIs are the
   profile's `target_role`s only — `past_role`s used to be appended too, which pulled in
   results matching what the candidate has *done* rather than what they want next) →
   blocklist/training/country filters → dedupe-upsert into `jobs_seen` → embed &
   cosine-score every candidate against **every** cluster embedding, assigning each job
   to its single best-scoring cluster → free heuristic prescreen (`_heuristic_prescreen`:
   title-regex drops obvious seniority mismatches — Director/VP for a junior, Intern for
   a senior — before any LLM spends a token) → adaptive strict/broadened pool per cluster
   (`TARGET_POOL` = 90) → **one merged sector+seniority screen per cluster**
   (`full_auto.screen_gate`, a cached cheap-model call that *annotates* rather than
   drops): the backend then hard-drops only off-sector jobs, **demotes rather than
   drops** seniority failures, auto-admits the top `AUTO_PASS_TOP` by embed_score, and
   **guarantees a per-cluster floor** so the cheap gate can never starve a cluster to
   zero (the full-text final AI stays the real seniority judge) → fair-allocate to
   `TARGET_POOL` → **cheap numeric rank stage** (`full_auto.rank_gate`, also run once
   *per cluster* with that cluster's own roles — a profile-wide call was diluting a
   minority cluster's fit scores with the candidate's other target-role cluster's
   context, a real bug fixed once already): a 0–100 fit estimate per job; the bottom
   `RANK_AUTOREJECT_FRACTION` (20%) is dropped **per cluster, before** fair-allocating to
   `JUDGE_POOL` (40), so a cluster that happens to score lower can't lose more than its
   own share before fair-allocate ever runs → optional Phase 5 full-page scrape (skipped
   for ATS-sourced jobs, for any snippet already long enough to judge —
   `SNIPPET_SUFFICIENT_CHARS`, deliberately above Adzuna's exact-500-char API truncation
   so Adzuna snippets don't wave through as "sufficient" by coincidence — and for
   anything with a persisted `full_text` from a prior run — see
   `engine.py::_needs_full_scrape`) → **Phase 6 final LLM evaluation runs once per
   cluster**, each a **single** expensive call (`full_auto.final_evaluation_split`)
   returning a strict `strong` list, a lenient disqualifier-only `backup` list, and an
   optional `disqualified` list (a short AI-authored reason for any job hard-excluded by
   a DISQUALIFIERS rule, persisted into that job's `eval_analysis` instead of the blank
   field a plain reject used to get — added because 100% of historical reject verdicts
   had zero captured reasoning, which made a past investigation into thin results unable
   to see why anything was excluded); jobs already judged under the current profile are
   served from their stored verdict and never re-sent, and a job with a stored `reject`
   verdict under the current signature is never resurfaced — including by the
   fallback below, which used to pull from the full unfiltered candidate list and could
   re-show exactly these rejects as an "inconclusive, showing anyway" placeholder → a
   deterministic top-N fallback fires **only when the expensive call itself failed**
   (exception/malformed response) — never when it succeeded and genuinely rejected
   everyone, which now correctly contributes zero picks for that cluster rather than
   padding a wrong-function role into the results → fair-allocate the combined
   per-cluster picks to a final cap (`FINAL_PICKS` = 12) → persisted as `Role` rows.
   Historical note: this stage used to run two separate gates (sector per-cluster,
   seniority once globally) and a strict-then-relaxed two-call final eval. The global
   seniority gate over-pruned and starved clusters, which is why fallbacks fired as the
   *main* path; the merge + keep-floor + single-call design above replaced that.
3. Cluster/stream identity is used internally (gate routing, per-cluster LLM calls) but
   is **not** currently exposed as UI grouping — by design, not an oversight; it only
   ever surfaces as an optional "Matched via: X track" clause in `ai_analysis` when more
   than one cluster exists.

Key cost/reliability guards layered into this pipeline (tune via env vars, see
`config.py` / top of `full_auto.py`):
- `MAX_SEARCHES_PER_DAY` (default 5) — per-profile daily search cap.
- `DISCOVERY_ATS_CACHE_TTL_HOURS` (default 4) — skips re-querying the ~40-company ATS
  batch on back-to-back searches within the window; the term-based API sources always
  fetch fresh.
- `SERPER_SITE_OPERATOR_OK` (default false) — serper.dev's free tier rejects `site:`
  queries outright, so ATS-token harvesting (`full_auto.harvest_ats_tokens`) builds a
  plain keyword+domain query by default; flip this once/if the plan supports `site:`
  again. `ATS_HARVEST_MAX_QUERIES` caps how many (vendor, keyword) combos one harvest
  call spends.
- The domain blocklist (Settings → Blocked domains, `moderation.py`) is consulted both
  at discovery time (drops listings before they enter the store) and inside Phase 5
  scraping (skips retrying a known-bad domain instead of paying retries/timeouts on it).
- `AUTO_PASS_TOP` (default 2, `engine.py`) — per cluster, the highest-embed_score
  candidates skip the gate's seniority veto and go straight to the expensive AI.
- `MIN_RESULTS` (default 3) — the per-cluster keep-floor the merged screen backfills to.
- `JUDGE_POOL` (default 40) / `RANK_AUTOREJECT_FRACTION` (default 0.20, `engine.py`) —
  the cheap rank stage's cap and per-cluster autoreject fraction, see pipeline step 2.
- Google-organic discovery (`full_auto.fetch_google_jobs`/`_looks_like_category_page`)
  drops board-owned category/search-listing pages (e.g. a charityjob.co.uk "N jobs in
  X" results page) that aren't an individual posting — these used to enter the store
  and consume pool/scrape/eval slots as if they were real listings.
- Phase 5 scraping: every source shares one concurrency lane (`MAX_CONCURRENT`, no more
  Adzuna-specific single-lane serialization — live testing showed it wasn't preventing
  any actual blocking, just adding minutes of pure sleep), a redirect-tracking stub
  (e.g. Adzuna's `/jobs/land/ad/...` click-through pages, which resolve 200 OK but are
  just a "you're being redirected" page) is detected and treated as a failed scrape
  rather than persisted as real content, and the whole phase has a 60s total wall-clock
  budget — whatever hasn't finished by then falls back to its snippet.
- `emit()` (`full_auto.py`) catches `UnicodeEncodeError` and re-encodes ASCII-safe —
  a log line containing an emoji used to crash whatever phase was running on a
  console whose stdout isn't UTF-8-capable (confirmed reproducible on this repo's own
  venv under some Windows launch paths).
- `fetch_jsearch` (`full_auto.py`) retries once with a longer timeout on a read
  timeout — the JSearch RapidAPI endpoint (`/search-v2`, since RapidAPI retired the
  older `/search`) runs slow enough that a single fixed 12s timeout was dropping the
  source's results on ordinary slow responses, not just outages.
- Cross-run reuse (persisted `full_text` + `eval_*` on `JobSeen`, above) means the
  cheapest run is a *repeat* run: unchanged jobs are re-embedded/re-scraped/re-judged by
  nothing. Editing a profile attribute changes the gate/eval signatures and re-opens
  everything, which is the intended invalidation.

### Feedback / weight system

Tick/cross/ignore on a `Role` (`services/feedback.py`) nudges the `weight` of whichever
`ProfileAttribute`s that role matched, by `config.DELTAS`, clamped to
`[WEIGHT_MIN, WEIGHT_MAX]`. This is the entire "learning" mechanism — there is no model
retraining. Weight has two effects:
- **Embedding pre-filter** (`snapshot._weighted_text`): each value is repeated
  `max(0, round(base_emphasis * weight * proficiency_mult))` times — sustained ticks
  amplify a value's pull on the semantic pre-filter, and sustained crosses can now
  genuinely suppress it out of the embedding text entirely (count reaches 0),
  symmetric in both directions. This used to be `max(1, round(base * max(1,
  round(weight)) * mult))` — the *inner* `max(1, round(weight))` alone already floored
  the effective weight at 1 for anything below ~1.5, and the *outer* `max(1, ...)`
  floored the final count too, so a value crossed all the way down to `WEIGHT_MIN`
  (0.1) still counted the same as a never-touched 1.0 default — crossing could stop
  future amplification but never actually suppress anything. Fixed once already —
  watch for this if touching the formula again; dropping only one of the two `max(1,
  ...)` calls does not fix it.
- **Cheap gate/rank prompts** (`full_auto._screen_prompt`/`_rank_prompt`, via
  `snapshot.build_snapshot`'s `target_role_weight_tiers`/`skill_weight_tiers`): a
  non-neutral-weight target role or skill is annotated with a coarse priority label
  (e.g. "strongly preferred by candidate" / "candidate has shown disinterest --
  deprioritize") so the cheap screen/rank models get some signal from the candidate's
  own feedback too — this is what lets weight have some influence on which roles reach
  the expensive final judge (and a small nudge on the cheap rank score), not only on
  which jobs float toward the top of the embedding pre-filter. The **expensive final
  judge still sees a flat, unweighted attribute list** (each value once, just possibly
  reordered since attributes are sorted by weight within their group) — weighting
  doesn't argue a value more strongly to it, only shapes which candidates arrive there.

### Background execution & routers

Search runs as a FastAPI `BackgroundTask` (`services/engine.py::run_search_task`); the
frontend polls `GET /profiles/{id}/search/status` every ~2.5s
(`frontend/lib/hooks.ts::useSearchStatus`) until it leaves `running`. Routers
(`backend/app/routers/`) are thin — `profiles`, `attributes`, `onboarding` (CV/text
parsing + AI suggestions), `search` (kickoff/status/roles/role-lifecycle actions), and
`settings` (source toggles, blocklist, full-scrape toggle, source-performance funnel).

`backend/app/main.py` force-sets `WindowsProactorEventLoopPolicy` before anything else
loads — required because Playwright (used by Phase 5 scraping) needs subprocess support
the default Windows Selector loop doesn't have; don't remove this on Windows.

### Frontend

Pages: `/onboarding` (CV upload/paste → attribute chips), `/dashboard` (profile tabs +
live attribute editing + stats), `/search` (ranked results, tick/cross), `/my-roles`
(saved/ignored/applied + application-status). `AttributeRow`/`Chip` are the shared
add/edit/delete building blocks for every attribute type; `RoleCard` is the shared result
card across `/search` and `/my-roles`. TanStack Query handles server state, polling, and
cache invalidation on tick/cross.
