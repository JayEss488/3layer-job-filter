# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## User preferences
Don't run a full end to end search in testing because it uses real API credits.

## What this is

Four in a Thousand: an AI job-matching app that parses a CV into structured "memory" (profile
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

Regenerate the UK geo reference data (rarely — the committed
`backend/app/uk_geo_gen.py` is the runtime artifact; see the commute-distance section):
```
venv/Scripts/python scripts/gen_uk_geo.py
```

Regenerate the UK charity employer seed list (rarely — the committed
`backend/app/uk_charity_gen.py` is the runtime artifact; needs the gitignored
Charity Commission extract in `txt non code/`, see the direct-employer section):
```
venv/Scripts/python scripts/gen_uk_charity_seed.py
```

Run a pass of the direct-employer ATS-detection crawl (plain HTTP, no API credits, no
LLM; resumable, and a no-op if everything was probed inside the recheck window):
```
venv/Scripts/python scripts/crawl_direct_employers.py --limit 200
```
```
venv/Scripts/python scripts/crawl_direct_employers.py --status
```
```
venv/Scripts/python scripts/crawl_direct_employers.py --misses
```

Backfill the distance / place-name / normalised-salary columns onto roles that predate
them (pure, offline, idempotent — no API, no LLM):
```
venv/Scripts/python scripts/backfill_role_geo_salary.py --dry-run
```

Regenerate the UK licensed visa-sponsor list (rarely — the committed
`backend/app/uk_sponsor_gen.py` is the runtime artifact; needs the gitignored Home Office
register CSV in `txt non code/`, see the visa-sponsorship section):
```
venv/Scripts/python scripts/gen_uk_sponsors.py
```

Measure the sponsor matcher against the live job store (read-only, offline — the
regression bar for any matcher change, see the visa-sponsorship section):
```
venv/Scripts/python scripts/audit_sponsor_match.py
```

Measure how many already-surfaced roles are still live (read-only, offline, writes
nothing; `--browser` escalates the rows a plain GET can't answer for):
```
venv/Scripts/python scripts/audit_listing_liveness.py --limit 45 --browser
```

**Run one observation pass — POINT A SCHEDULER AT THIS, DAILY** (board-API quota only,
zero OpenAI; never writes `jobs_seen`, never creates a Role or a SearchRun, so it cannot
consume the daily search cap). Daily is a functional requirement, not tidiness: the
evergreen rule divides days-seen by days-since-discovery, so gaps suppress the signal
rather than merely delaying it:
```
venv/Scripts/python scripts/observe_listings.py
```
```
venv/Scripts/python scripts/observe_listings.py --status
```

Re-check tracked listings for liveness and stamp `dead_at` (plain GETs, no browser, no
LLM, no credits). **Run `--dry-run` first and read the output** — `dead_reason` is
unrecoverable:
```
venv/Scripts/python scripts/recheck_liveness.py --limit 20 --dry-run
```
```
venv/Scripts/python scripts/recheck_liveness.py
```

Score every stored listing against the ghost rules, offline and read-only — the
regression bar for any rule change. Judge it on the roles the user actually engaged
with (bar: **zero applied-to roles flagged high**), never on the store-wide hit count:
```
venv/Scripts/python scripts/backtest_ghost_rules.py
```

Characterise the final judge's requirements checklist across every persisted verdict
(read-only, offline, free) — the cheap half of the judge-prompt regression bar. Read the
PER-RUN table and its `tok/pick` budget column before the store-wide mean; see the
judge-checklist section:
```
venv/Scripts/python scripts/audit_judge_checklists.py --profile-id 1 --since-run 20
```

A/B two judge system prompts live over the SAME listings (the expensive half — a couple
of EXP_MODEL calls per arm; nothing is written and no verdict is persisted). Run BOTH
samples: the balanced draw, and `--worst-checklists`, which targets the listings whose
stored checklist was thinnest:
```
venv/Scripts/python tests/judge_harness.py --profile-id 1 --sample-size 20 --seed 1
```
```
venv/Scripts/python tests/judge_harness.py --profile-id 1 --worst-checklists
```

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
  country, location_scope, custom, avoid, must_have) is one typed row with a `weight` and
  a `confirmed`
  flag, not a blob — see `config.ATTRIBUTE_TYPES`/`ATTRIBUTE_DIRECTION`. `target_role`
  (what they want) is deliberately kept separate from `past_role` (what they've done) so
  the engine can weight them differently. `avoid`/`must_have` are the candidate's own
  hard filters (auto-filled from the CV by `parsing.py`'s extraction call, editable as
  chips below target roles): unlike the soft, LLM-derived `requirements` list
  (`profile_intel.py`), they drive an *unconditional* drop at the cheap gate
  (`hard_gate_ok`) and a DISQUALIFIER rule at the final judge — see the pipeline section.
  **Some attribute types are deliberately UI-hidden but still live**: `sector_target` and
  `custom` no longer render on the dashboard/onboarding pages (the profile shifted from
  "match on paper" to "the candidate wants this"), but they still auto-fill from the CV
  and still feed the engine — `sector_target` one of only two embedding pre-filter signals
  (`snapshot._BASE_EMPHASIS`), `custom` the "Constraints" CV line. `skill` and
  `qualification` are also still-live-but-hidden types, but as of the formation rewrite
  they are **no longer auto-extracted from the CV at all** — the profile shifted the
  concrete skill/qualification detail into the `cv_summary` narrative (written by the
  formation "understand" call), which is what the gates and final judge now read for it
  (`snapshot.candidate_brief`); the types still exist so a user can add rows by hand and
  so their weight/evidence tiers still feed the gate prompts when present. Don't delete
  any of them as dead code because no UI references them.
- **`Role`**: one row per listing surfaced to the user in a given search run, with a
  lifecycle `status` (`new → saved/crossed/ignored → applied → deleted`) driving both the
  UI tabs and re-search semantics (only `crossed` roles are pruned before each new run,
  resetting the "passed this session" list; `new`/inbox roles persist indefinitely until
  the user acts on them, and saved/applied are never touched). `search_run_id` (FK to
  `SearchRun`, nullable — old rows predate the column) records which run produced a row.
  The `/search` page uses it to bucket a still-`new` role left over from an earlier run
  into its own "from earlier searches" section below the current run's picks, instead of
  interleaving every past run's unreviewed roles by `fit_rank` (each run numbers its own
  1..N, so ranks collide across runs). `saved` roles get their own "already
  saved" section, unranked (no `showRank`) — they used to sit inside the same ranked
  `current` list as this run's fresh picks, which caused literal duplicate rank badges
  (a saved role's stale fit_rank from its own original run colliding on-screen with an
  unrelated fit_rank=N from the new run). **That section is scoped to saved roles that
  are NOT one of this run's ranked picks** (`isCurrentRankedPick`: non-null `fit_rank`,
  `search_run_id === latestRunId`, no `provisional_stage`) — the original "any saved
  role" wording was too broad, and swallowed the one case where the rank badge is
  genuinely correct: a role the user **Kept mid-run** (or marked applied) that the judge
  then picked is upgraded in place at finalization with this run's own `fit_rank` and
  `status` untouched, so it is a current ranked pick that merely happens to be `saved`.
  Filing it under "already saved" pulled rank 1 out of the ranked list, and the visible
  numbering started at 2 with no indication anything was missing. `applied` rows from
  this run come back for the same reason — previously they matched no section's status
  filter at all and rendered nowhere while still being counted in "Showing N results".
  The `/my-roles` "Inbox" tab (every
  `status=new` role, any run) is deliberately unaffected — this is `/search`-page-only
  display grouping of the same underlying rows, not a new status or a data change.
  **Progressive paint — the /search page is written three times per run**
  (`provisional`/`provisional_stage`/`rank_score` columns). Each paint is the same
  `engine._upsert_provisional_rows` call with a different `stage`, and a job that
  reaches a later stage is the **same Role row updated in place** (matched by
  `external_id` within the run) — which is where the de-duplication between the three
  on-screen sections comes from, rather than any dedupe pass. A row is only ever
  promoted forwards (`embed → rank → final`); re-running the embed paint never demotes
  one back. Consequence worth expecting: the embedding section shrinks as a run
  progresses, so an 8-card "early matches" block routinely ends up as 3–4.
  1. **`stage="embed"`, `EMBED_PAINT_MAX`=8** — painted straight off the cosine
     pre-filter the moment `_cluster_candidate_queues` returns (seconds in; the embed +
     score phases measured 6.8s and 0.4s live, against ~90s for the first gate+rank
     round). Ordered by `embed_score`, **not** `_selection_score` — nothing has a rank
     score yet, so the default key would collapse to a flat 50.0 whose only remaining
     variation is the rich-text bonus, i.e. it would order the first cards the user ever
     sees by snippet length. No `rank_score` is written (a `50/100` chip on a card no
     model has read would be fabricated) and the card's corner reads "Not yet reviewed",
     not "Verifying…" — nothing is verifying it and it may never be examined at all.
     These are the least-informed cards the app shows: cosine similarity and nothing
     else.
  2. **`stage="rank"`, `PROVISIONAL_MAX`=12** — the pre-existing mid-run paint, below.
  3. **final** — `provisional=False`, `provisional_stage=None`.

  After the run, rank-stage leftovers do **not** all disappear: the top
  `UNREVIEWED_RETAIN_MAX`(8) by `rank_score` are retained
  (`engine._retain_unreviewed_provisional`) as `provisional=False` +
  `provisional_stage="rank"` — the one combination that outlives a run — and render in a
  trailing "quick-scored only" section with `fit_rank=None` (NULLS LAST). **Anything the
  judge actually rejected is excluded** from that retention: "a job with a stored
  `reject` verdict under the current signature is never resurfaced" is enforced
  everywhere else in the pipeline, and a rejected role coming back labelled "not
  reviewed" would also simply be false. So that section only ever means *not reached*,
  never *reviewed and failed*. Embedding-stage leftovers are resolved normally at
  finalization (deleted if never acted on). `_reconcile_provisional_roles` deliberately
  skips embed-stage rows — it runs at the end of gate+rank, exactly when the embedding
  section is supposed to still be on screen underneath the rank results.

  Provisional-row mechanics: rather than waiting for
  every cluster's gate+rank to finish, each gate/rank round (one screen_gate + one
  rank_gate call, examining up to `TARGET_POOL` candidates) reports its growing
  judge-eligible snapshot back to the main thread over a `queue.Queue` (worker threads
  still never touch the DB session — see `_gate_rank_refill_cluster`'s `report` param /
  `_make_progress_reporter`), which upserts the global top-`engine.PROVISIONAL_MAX`
  scorers seen *so far* across all clusters as `provisional=True` rows
  (`engine._upsert_provisional_rows`) — so the very first rank_gate call to return,
  across any cluster, can put "Verifying…" cards on screen, with later rounds adding to
  them. This interim view is a simple global top-N by score, not fairness-balanced
  across clusters the way the eventual judge pool is, and can transiently show *more*
  than `PROVISIONAL_MAX` cards (interim upserts only ever add/update, never delete, to
  avoid ever risking silently dropping a mid-run Keep) until gate+rank fully finishes and
  `engine._reconcile_provisional_roles` runs against the real `_fair_allocate`d pool,
  trimming it back to the true top-N in fair order. At finalization each surviving row is
  upgraded **in place** (matched by `external_id` scoped to the run — same row id, so the
  card swaps content, and `status` is never touched so a mid-run save/cross survives) or
  resolved by the leftover rules (`engine._resolve_leftover_provisional`): `applied` →
  always retained with a ⚠ marker prepended to `ai_analysis` and `fit_rank=None` (a real
  action already taken, never reverted); `saved` (i.e. Keep, a *tentative* preference,
  unlike an ordinary Save) → retained the same way if the judge rated it strong/backup
  (agreement, just short of this run's numeric cut), but flipped back to `status="new"`
  (returned to the Inbox for a real decision) if the judge rejected it outright, never
  got to judge it at all, or the run was interrupted before finishing; untouched `new` →
  retained as "quick-scored only" if it qualifies (rank stage, top
  `UNREVIEWED_RETAIN_MAX`, not judge-rejected — see the progressive-paint note above),
  otherwise hard-deleted; crossed → soft-deleted (keeps the FeedbackLog referent) **and
  `fit_rank` cleared**. Every branch of `_resolve_leftover_provisional` must clear
  `fit_rank`; the crossed branch used not to, and that was a real user-visible bug. A
  provisional card's rank comes from its position in the interim top-N, which is
  numbered independently of the final picks' 1..N, so a leftover keeping a stale rank
  collides with a genuine pick — a live run showed two different cards both badged "3",
  one of them a listing the judge had actually REJECTED.
  A user action always wins: `saved`/`applied` take the rules above, never the
  quick-scored retention. Cancel/failure/
  restart all run `engine._cleanup_provisional_roles` (four call sites incl.
  `reap_stale_search_runs`), preserving the "an unfinished run leaves nothing
  user-visible" invariant (a Kept row in an interrupted run always returns to the Inbox,
  same as a rejected verdict, never silently stays Saved). `GET .../roles` **filters
  provisional rows out unless `include_provisional=true`** — only the /search page opts
  in; /my-roles and stats never see them. The /search page's `!r.provisional` guard also
  hides leftovers during the short gap between a cancel and the background thread's
  cleanup commit. `/search` and the `/my-roles` "Inbox" tab both also expose a "Mark as
  applied" action directly on each role card (`POST /roles/{id}/apply`, pre-existing
  endpoint) — no need to Save first or navigate to `/my-roles`' Saved tab.
  `_find_soft_duplicate` (same company + title + an agreeing location) treats two
  locations as agreeing either by a shared word token **or by being the identical
  string**. The string half matters more than it sounds: `_location_tokens` keeps
  only alphabetic runs longer than two characters, so a bare UK postcode yields
  NOTHING (`"GU98AD"` → `{}`, both runs too short) and the function used to bail
  out and declare every such listing un-duplicatable however exactly it matched.
  Reed routinely gives a bare postcode as the whole location field: that was 243
  of 1,200 sampled store rows, and it stored Plum Personnel's "Junior Application
  Developer" twice (Reed ids 57177686/57177687), identical in company, title,
  location and text. Two identical location strings are strictly stronger evidence
  than the single shared token the original branch accepted, so this can only
  tighten what already counted as agreement. Measured old-vs-new over 1,200 real
  rows: **0 regressions, 11 duplicates the old code missed, 0 left unmatched**
  (was 243).
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
  **`state` is what decides whether a row is even a candidate, and its retirement rule
  was for a long time the single biggest constraint on how many roles a run could
  return.** The pool is `state='new'` plus a ≤`BACKLOG_TOPUP`(40) resurfacing of rows
  the judge already graded strong/backup; everything marked `enriched` is out. A row is
  marked `enriched` once a run has *examined* it — which includes every row the CHEAP
  gate dropped, long before the judge ever saw it. Because the gate examines in
  embed-score order, what it retires is disproportionately the **top** of the store, and
  the retirement was permanent and profile-independent. Measured on a live store: 882
  retired rows of which **838 cleared the relevance floor**, against **274** in the
  entire remaining pool — i.e. 75% of every genuinely-relevant listing ever discovered
  was locked out, and three consecutive runs saw their candidate queue fall 436 → 359 →
  114 while fresh discovery (mean cosine 0.315) could not replace the head of the
  distribution it was consuming. `gate_signature` fixes this as the gate-side twin of
  `eval_signature`: `_mark(..., "enriched", gate_sig)` stamps *which* profile the
  retirement was decided under, and `_gate_reopened_rows` re-admits any row whose
  `gate_signature` differs from the current one. Deliberately narrow — only rows with
  `eval_verdict IS NULL` (gate-dropped, never judged), so it can't route around the
  "a job with a stored `reject` verdict under the current signature is never resurfaced"
  invariant, and uncapped, because it corrects the pool's *definition* rather than
  topping it up when thin. An unchanged profile re-opens nothing, so this can never
  become re-gating the same rows every run; a NULL signature (retired before the column
  existed) counts as different, so shipping it re-opens the existing backlog once. Count
  lands in `funnel_counts.pool_gate_reopened`.
  `posted_at`/`expires_at` are the employer's stated posting/closing dates, distinct
  from `source_updated_at` (a change-detection key) and from `first_seen` (when *we*
  discovered it, which for a months-old listing says nothing about its age). Until these
  existed **only the ATS feeds carried a date at all** — Reed, Adzuna, JSearch and
  Careerjet, i.e. most real listings, produced rows with no age at any stage, so nothing
  in the pipeline could notice a months-old posting ranking top. Both are nullable and
  often null; every consumer treats unknown as *no penalty*, never as old. On refresh
  the **earliest** claimed `posted_at` wins (an aggregator re-listing an old posting must
  not launder it fresh) and the **latest** `expires_at` (an employer can genuinely
  extend a closing date).
  `soft_dup_key` is the normalised `"<company>|<title>"` (`engine._soft_dup_key`) that
  the discovery upsert's soft-duplicate lookup keys on, written at insert and backfilled
  by `database._migrate_soft_dup_key`. It exists for speed and correctness both. The
  lookup used to pre-filter on a `lower(company)` equality-or-LIKE-prefix and hydrate
  FULL `JobSeen` entities for every match — on a real store ~818 rows per call, each
  dragging an 8KB embedding, once per newly-discovered identity (992 in a measured run).
  **This, not the OpenAI call, is what the "embed" phase timing actually measures**: the
  embedding API for that run's entire 686-text batch takes 1.55s of a 48.62s phase (3%),
  and cross-run evidence is decisive — run 7 computed **2,597** embeddings inside a 19.3s
  phase while run 19 computed **235** inside a 60.3s one. Phase time tracks store size,
  not `embedded_new`. Keying on an indexed column took `_upsert_discovered` from **81.4s
  to 3.7s (22x)** at run-20 volume; a plain index on `lower(company)` is NOT a substitute
  (81.4s to 77.8s), because the cost is the VOLUME of rows a common company prefix
  returns, not scan time. The remaining win came from bucketing the in-batch candidates
  by the same key — that check was O(n^2) over the discovery batch (815k `_norm()`
  calls). It is also strictly MORE correct: `_norm_company` strips leading whitespace on
  the incoming side but SQL could not strip it on the stored side, so a company stored
  with a leading tab never matched its own earlier row. Head-to-head on 1,200 real rows:
  **0 regressions, 3 duplicates the old query missed**, and the 243 non-matches were
  identical under both (rows whose location yields no usable tokens, which
  `_find_soft_duplicate` has always declined to match on). Kept distinct from
  `repost_key`, which is the same shape but answers a different question — see that
  field. **Local embeddings would not help this phase**; the API is 3% of it.
- **`CompanyATS`**: cached vendor/token registry for the ATS discovery tier (Greenhouse,
  Lever, Ashby, Workable, Recruitee, Personio, SmartRecruiters), populated by
  `seed_ats.py` (curated,
  live-validated) and grown by `harvest.py`'s occasional `site:`-search harvest and by
  the direct-employer crawl (see below) — this
  list itself is cached fine; it's the *job data fetched from* these companies each run
  that needed a TTL guard (see below).
  **SmartRecruiters is the one vendor whose listing endpoint carries no description
  at all** (`/v1/companies/{token}/postings` returns metadata only), so one company
  costs 1 + N HTTP calls rather than 1 — hence `SMARTRECRUITERS_MAX_POSTINGS`/
  `_DETAIL_WORKERS`, which exist because this fan-out happens *inside* a slot of
  `gather_jobs`' 12-wide pool. Its detail payload splits into
  `companyDescription`/`jobDescription`/**`qualifications`**/`additionalInformation`,
  i.e. exactly the multi-field shape `_ats_text` exists to merge — taking
  `jobDescription` alone would drop the requirements section, the same bug already
  fixed for Lever/Recruitee/Workable. A posting whose detail call fails is **dropped,
  not emitted text-less**: `smartrecruiters` is an ATS key, so
  `_has_judgeable_text` would treat a text-less row as text-complete, skip phase 5,
  hand it `RICH_TEXT_SELECTION_BONUS` and send it to the judge on its title alone —
  the same inversion documented for un-enriched Adzuna rows.
  **Teamtailor was evaluated and rejected**: `api.teamtailor.com` requires a
  per-company `X-Api-Key`, and there is no no-auth per-company feed, so it cannot be
  reached at registry scale the way the seven above can.
- **`DirectEmployerProbe`**: one row per employer domain the direct-employer crawl has
  looked at — see the direct-employer section below. The seed list is NOT stored here
  (it lives in the generated `uk_charity_gen` module); this records only what a probe
  found, so it doubles as the crawl's cursor. **Keeping the misses is the point**:
  without them the crawl can't tell "not yet looked at" from "looked at, nothing
  there", would re-spend its budget on the same dead domains every pass, and would
  report a 100% hit rate by construction.
- **`FeedbackLog`**: append-only tick/cross/ignore/apply audit trail.
- **`SearchRun`**: one row per kicked-off search; drives `/search/status` polling and the
  daily search cap. Also the per-run diagnostics store, all written at the end of
  `run_search_task`: `phase_timings` (per-phase wall time, read by
  `GET /settings/run-timings` → the Settings "Search run timings" panel) and
  `funnel_counts` (JSON, ints/bools only — read by
  `GET /settings/run-funnel`), plus `snapshot_samples` (JSON
  `{stage: {"count": N, "samples": [{title, company, url}]}}`, read by
  `GET /settings/snapshot` and rendered by the Settings page's bottom "Snapshot" panel).
  `snapshot_samples` also carries a `_clusters` key — the PER-CLUSTER funnel
  (queue/examined/gate/judge/picks/stop_reason per role family), which the run-wide
  `funnel_counts` sums away and so cannot show: a strong track and a starving one average
  into numbers that look healthy. It rides in that column rather than a new one because
  the payload is free-form JSON and `get_run_snapshot` iterates a fixed stage list,
  ignoring unknown keys — so it needed no `_migrate_columns` entry. `GET
  /settings/run-timings` reads it back out alongside the timings.
  The snapshot answers what the funnel counts can't: *which* roles were actually at each
  stage, with URLs, so a weak stage can be inspected (or pasted into an AI) rather than
  inferred from a drop in the numbers. Samples are **random, not head-of-list**
  (`engine._sample_stage`) — every stage from the embedding on is score-sorted, so the
  first N would always be that stage's best and would never show what it really lets
  through. Note the plumbing quirk: `_run_engine_pipeline` stashes samples inside the
  `funnel` dict under a `"samples"` key purely to avoid changing its return arity across
  four early returns; `run_search_task` pops it back out into its own column.
- **`Setting`**: generic key/value store (`profile_id=NULL` = global) for source
  toggles, the domain blocklist, the full-scrape toggle, and per-profile freshness
  markers (ATS-harvest keyword hash, ATS-batch last-fetched timestamp).

Schema changes are **not** Alembic — `database.py::_migrate_columns()` is a hand-rolled,
idempotent `ALTER TABLE ADD COLUMN` dict. Add new columns there.

### Profile formation (CV/notes → memory)

A CV upload or text paste (`routers/onboarding.py::parse_cv`/`parse_text` →
`services/formation.py`) runs **three `MID_MODEL` (gpt-5.6-luna) LLM calls IN PARALLEL**
(`asyncio.gather` over `to_thread`; the calls are deliberately DB-free so the threads
never share a session — writes happen back on the request thread in one commit):

1. **Extraction** (`parsing.extract_attributes`) → the structured, engine-load-bearing
   chips only: PAID `past_role` (title alone; informal/unpaid roles are skipped),
   `seniority`, `sector_target`, `location`+work-types, `salary`, `custom`, `must_have`,
   `avoid`. It **no longer extracts `skill`, `qualification`, or a `cv_summary`** — those
   were noise as chips, and the concrete detail now lives in the summary the other call
   writes.
2. **Families** (`profile_intel.generate_families`) → the candidate's **role families**
   (`[{label, roles[]}]`, 1–3, generated *with their titles in one pass*) and a
   first-person `intent_draft` (only used if `intent_text` is empty). For a short CV
   (`< CV_SHORT_WORD_THRESHOLD`) the intent task and its field are **dropped from the
   prompt entirely** (`want_intent_draft=False`; the regenerate path's `_prompt` skips
   it on the same flag). A short document gives the model too little to paraphrase, and
   the draft then reaches every downstream stage as `intent_text` — which
   `snapshot.build_snapshot` ranks *above* every other want-signal, i.e. as the
   candidate's own words. A live run drafted "…with openness to roles involving
   automation" off a data/software CV, which is part of what let an industrial-controls
   "Automation Engineer" through as a top pick. The box is now empty and framed as
   optional on both `/onboarding` and `/dashboard` (`IntentEditor`), and is used
   verbatim whenever the candidate does fill it.
3. **Summary** (`profile_intel.generate_summary`) → the `cv_summary` (the evidence brief
   the judge/gates read) and the "Looking for…" `header` (told explicitly NOT to restate
   the summary — they used to overlap). For a short CV (`< CV_SHORT_WORD_THRESHOLD`)
   **this whole call is skipped** and the raw text is stored as the summary verbatim.
   `CV_SUMMARY_MAX_CHARS` is a runaway-reply guard, **not** a length budget — the
   length actually requested lives in `_SUMMARY_TASK` ("max 450 words"), and the cap
   has to sit clear of it or it silently becomes the real limit. At 3000 it did
   exactly that: a live parse was guillotined at 2,997 chars, mid-sentence, at word
   432 of a compliant ~460-word reply ("…the 14-attendee Model Climate Conference
   from 43 sign-up"). What that deletes is not random — `_SUMMARY_TASK` numbers its
   sections, so the tail is always (4) leadership/extracurriculars, (5) writing and
   communication work, (6) languages and other differentiators, and since skill/
   qualification chips are no longer extracted, `cv_summary` is the *only* place that
   evidence reaches the judge (`snapshot.candidate_brief`). So the cap was quietly
   deleting a whole class of evidence from every long-CV profile, permanently. Now
   4000, and both paths clip through `profile_intel.clip_summary`, which cuts at a
   sentence boundary (falling back to a word boundary) so an overrunning reply can
   never end mid-word again — a brief that trails off mid-claim reads to the judge as
   a claim that trails off, with no marker anywhere to say the document was cut.

Calls 2 and 3 were one "understand" call until the split. That call alone generated
~1300 output tokens and *was* the block's critical path (~11.2s of an 11.3s parse) while
extraction finished in ~4.6s and idled. They share nothing but the source document, so
splitting them costs one extra copy of the CV in input tokens and takes the families/
intent generation off the summary's critical path. The task prompts moved across
verbatim — only the per-call task numbering and JSON field list differ — so neither
output's calibration changed. Note the remaining floor: the summary call is intrinsically
the long pole (a ~450-word evidence brief), so the parse now costs about what that one
call costs; trimming it further trades directly against what the final judge can read.

`formation.persist_formation` writes the attributes, the summary, the families
(`families.seed_families_from_groups` — creates `RoleFamily` rows + their `target_role`
attributes directly, capped at `MAX_ROLE_CLUSTERS`), a drafted intent, and then
`profile_intel.store_seeded_intel` — which writes a profile-intel cache signature matching
the freshly-seeded state so the **pre-search `ensure_profile_intel` (engine.py) is a
no-op** and doesn't regenerate a flat role list that would reshuffle the seeded families.

This replaced a **serial chain of three STRONG calls** (parse → profile_intel → a separate
re-cluster), which was both slow (~52s on a 4400-word CV; two ~23s STRONG calls back-to-
back) and the source of the family over-split/over-merge: generating a flat title list and
then re-clustering it lost the grouping boundaries, so the cluster call re-guessed them and
disagreed. Generating families+titles together, biased toward ONE family and grouping by
day-to-day job function (not seniority/specialisation), fixes that — see the
`_FAMILIES_TASK` prompt in `profile_intel.py` before assuming it's miscalibrated.

The **regenerate/edit path is separate**: `ensure_profile_intel` (cached, signature-gated,
also `MID_MODEL` now) re-derives a *flat* target-role list + header from the profile's
current state, and `ensure_families` slots any newly-ungrouped roles into the EXISTING
families — family count/identity only ever changes via an explicit user action. A family
rename/delete's `cv_summary` reconciliation (`reconcile_summary_after_family_change`) now
runs **off-request** (`BackgroundTasks` → `_bg` wrapper with its own session) and is
skipped entirely when `cv_summary` is just the raw CV (`_summary_is_raw_cv`), so deleting a
family card is instant.

The Settings → "CV parse timing" panel (`routers/settings.py` + `services/diagnostics.py`)
measures this whole flow stage-by-stage against a throwaway profile (captures each LLM
call's model/tokens/duration via `llm.capture_llm_calls`); it costs the same MID calls
a real upload does, so it's user-triggered only. Its search-side counterpart is the
Settings → "Search run timings" panel (`GET /settings/run-timings`), which is a pure read
of what the last finished run already recorded — see the search-pipeline section.

### The search pipeline (the part that touches the most files)

1. **`backend/app/services/snapshot.py::build_snapshot`** turns a profile's attributes
   into the engine's input contract. The "streams" it scores per-cluster ARE the
   candidate's role families (`RoleFamily` rows — user-editable, seeded once from the CV;
   see the profile-formation section above), read straight off `list_families`;
   `cluster_target_roles` survives only as an in-memory FALLBACK for a `target_role` that
   somehow reaches the engine still ungrouped (`_role_groups`), not the primary path. Each
   cluster gets its own weighted embedding text (`_weighted_text`, scoped to just that
   cluster's target roles) and its own scoped synthetic-CV text (`cv_text_for_cluster`)
   for the final judge — this is what lets a candidate targeting two unrelated fields get
   judged fairly on each, instead of being averaged into a fit-for-neither blend.
2. **`backend/app/services/engine.py::_run_engine_pipeline`** orchestrates, per run:
   discovery (`full_auto.gather_jobs` — Reed/Adzuna/Google-Jobs-via-serper.dev/JSearch/
   Remotive + the ATS vendor batch; search terms sent to the board APIs are the
   profile's `target_role`s only — `past_role`s used to be appended too, which pulled in
   results matching what the candidate has *done* rather than what they want next.
   `gather_jobs` submits **one pool task per (source, term)** — each Source class has a
   `fetch_term` the pool calls directly; `fetch` is kept as a sequential loop over it for
   the legacy standalone path — because one whole-source task used to hold up to
   TERMS_PER_RUN sequential per-term calls in a single 12-wide-pool slot (Reed at 3
   pages/term = 18 sequential HTTP calls, the measured long pole of a 55s discovery
   phase). Reed/Adzuna page depth is set by `REED_PAGES_PER_TERM`/
   `ADZUNA_PAGES_PER_TERM` (env, **default 3**, 100/50 results per page), with
   `*_PAGES_PER_TERM_SPONSOR` (5) used instead when the licensed-sponsor filter is on
   — that filter keeps ~10% of rows, so the pool behind it has to be deeper.
   These were **cut to 1** on the measurement that a live run discovered 7,800 raw
   listings of which only ~100 were ever examined past the embedding stage, making
   pages 2–3 pure latency in front of first paint; they are back at 3 because two
   things changed — `RANK_EXAMINE_BUDGET` is now 320 rather than 40–80, so the deeper
   pages have somewhere to go, and across runs Reed and Adzuna are where most
   strongly-ranked picks actually come from. The cost is real and lands in the worst
   place (discovery precedes the first "early matches" paint); what bounds it is that
   `gather_jobs` fans out one task per (source, term), so 3 pages is 3 sequential HTTP
   calls inside ONE pool slot, not 3× wall clock. If time-to-first-card regresses,
   these two env vars are the knob, not the examine budget. `USAJOBS_PAGES_PER_TERM`
   stays 1 — it self-gates to nothing outside the US. The fetchers emit a
   "page cap hit … more results likely available" note whenever the last page came back
   full, and every discovery task emits its elapsed time plus a per-source
   "slowest term" aggregate, so both the coverage trade-off and any slow source stay
   visible in the console) →
   blocklist/training/country filters → dedupe-upsert into `jobs_seen` → embed
   (`engine._ensure_embeddings` — cached forever per job, and cached GLOBALLY across
   profiles: a vector is looked up in the `job_embeddings` store (a content-addressed
   table keyed by `sha1(EMBED_MODEL + embed_text)`, no user/profile scope — the embed
   text is pure job content, so the same job yields the same vector for everyone) and only
   text not already there hits OpenAI, which also collapses within-run duplicate texts
   (aggregator reposts) to one call each. Seed the store from existing `jobs_seen` vectors
   with `scripts/backfill_job_embeddings.py`. `jobs_seen.embedding` is still populated for
   the fast cosine path; the shared table is a compute cache in front of it. Fresh rows are
   embedded in `EMBED_CHUNK_SIZE` chunks fanned over an `EMBED_MAX_WORKERS`-worker pool,
   order-preserving via `ex.map`, DB writes staying on the calling thread) &
   cosine-score every candidate against **every** cluster embedding, assigning each job
   to its single best-scoring cluster → **two free, LLM-free prescreens** (see the
   pool-quality note below) → adaptive strict/broadened pool per cluster
   (`TARGET_POOL` = 90) → **Reed full-description enrichment**
   (`engine._enrich_reed_full_text` → `full_auto.fetch_reed_details`, see the
   text-supply note at the end of this section) → **one merged eight-axis screen per
   cluster, all clusters concurrently** (see the concurrency note at the end of this
   section)
   (`full_auto.screen_gate`, a cached cheap-model call judging role-function fit,
   whether the text is even a real single job posting, seniority, candidate-specific
   requirements, core-skills overlap, salary, and work arrangement in
   one response — grown from an original three-axis sector+seniority+requirements design;
   `SOFT_GATE_AXES` in both `full_auto.py` and `engine.py` is the single source of truth
   for which axes count, so the two modules can't quietly disagree on how many there
   are): `sector_ok`, `hard_gate_ok`, and `listing_ok` are the three unconditional hard
   drops. Despite its name, `sector_ok` no longer judges INDUSTRY sector — it used to
   also weigh an LLM-inferred `sectors` guess (`snapshot._infer_region`, invisible/
   uneditable in the UI, recomputed fresh each run from skills+past/target roles), which
   could drift from the candidate's actual current target roles and let a
   same-industry-different-function listing (e.g. a Lead Product Manager role surfacing
   for a Data & Insights candidate) through as "adjacent enough" — a live gate-harness
   diagnostic (`tests/gate_harness.py`) caught exactly this on a real profile.
   **`tests/gate_harness.py --ground-truth` is the way to check this stage's calibration**:
   it samples only listings the expensive judge has already ruled on (`JobSeen.eval_verdict`,
   balanced across verdicts) and scores the gate against those labels as two SEPARATE rates
   — good-jobs-kept and bad-jobs-caught — never one accuracy number, because dropping a job
   the judge liked destroys a result the user never sees while keeping one it rejects merely
   wastes a rank/judge call. Pair it with `--text-mode`: a judged row has been scraped, so it
   carries a `full_text` the gate never had at gate time, and re-screening it in the default
   `auto` mode measures a stage that doesn't exist. Running `snippet` (what the gate really
   had) against `full` (what it could do) is what separates a MISCALIBRATED gate from a
   STARVED one — opposite findings needing opposite fixes, and neither number alone tells
   them apart. It now
   judges purely on job-FUNCTION similarity to the candidate's own stated target roles,
   which were already part of this axis's prompt anyway; `rank_gate`'s prompt dropped the
   same `sectors` guess for the same reason.
   A `--ground-truth` audit on 113 labelled listings (2026-07-27) drove **screen_v11 → v12**,
   correcting three axes that were dropping judge-approved roles. The work-arrangement axis is
   now an explicit two-row conflict table in which **a hybrid LISTING never conflicts with
   anything** (it
   has on-site days, so it satisfies an On-site preference, and remote days, so it partly
   satisfies a Remote one — it had been hard-dropping hybrid roles for an On-site candidate),
   plus "if you couldn't classify the listing you cannot fail this axis". The seniority axis
   gained an **entry-level floor** (for a Graduate/Junior/Entry candidate, a graduate/junior/
   entry listing is a MATCH and can never be `seniority_low` — it had been citing "Graduate
   Analyst (graduate/early-career bar)" as evidence a graduate role sat *beneath* a Junior
   candidate) plus a restated direction self-check, after a plainly-`_high` argument came back
   under the `_low` code yet again. And `listing_ok` is now told to **read past board page
   furniture** (see below). Retention went 15/26 → 23/26 with **no loss of real screening
   power**: the raw catch rate fell 46% → 23%, but all 23 rejects the old gate caught and the
   new one passes had been dropped by exactly those broken axes, and *none* were knowable from
   the text the gate had (11 sat only in the scraped page, 9 carry no recorded reason at all).
   The old gate was dropping them for unrelated wrong reasons and happening to be right — the
   count of disqualifiers genuinely visible-and-missed is 1 before and 1 after. Don't read a
   fall in this stage's drop count as a regression without checking that distinction.
   **screen_v12 → v13** then carved the one exception back out of that entry-level floor:
   apprenticeships. v12's floor protected every apprenticeship except a "pre-degree" one, so a
   below-degree scheme passed the seniority axis clean for a graduate — and, worse, the scheme's
   skill list *matching* the candidate read as evidence of fit at every tier (a live run scored
   two analyst apprenticeships 84 and 90 at `rank_gate` and the final judge graded a BI
   Apprenticeship at National Minimum Wage **Strong fit**). An apprenticeship is a place on a
   course that happens to come with a job: it exists to teach someone who does *not* yet hold the
   qualification or the skills, and many carry an explicit eligibility bar against applicants who
   already hold an equivalent qualification. The rule fires only on **both** conditions — the
   candidate already holds a qualification at or above the level the scheme awards (for a
   degree-holder: any below-degree scheme; degree apprenticeships and Level 7 schemes are
   explicitly exempt, as are graduate schemes/programmes, which hire at the candidate's own level)
   **and** their evidence already covers what it says it will train them in, so an apprenticeship
   in a field they genuinely lack stays a real opportunity. Training-rate pay corroborates but is
   never required. Mirrored at all three tiers together — `screen_v13` (the floor's own
   APPRENTICESHIPS block), `rank_v10` (HARD DOWNGRADES rule f), eval 19 (DISQUALIFIERS rule 3's
   second paragraph) — because the live failure passed all three; leaving any one behind
   reinstates the hole at that stage.
   **screen_v13 → v14 (+ `rank_v12`, eval 21)** closed the CANDIDATE-side twin of the v12
   hybrid fix, which had let fully-remote roles reach the results page for a candidate who
   stated On-site and Hybrid. All three tiers had the same shape of hole, and it survived the
   v12 audit because the audit sampled a profile whose stated preference was On-site alone:
   * v12's conflict table phrased **both** rows "candidate stated **ONLY** X". A candidate
     stating On-site AND Hybrid matched neither row, so a remote listing fell into "every
     other combination → `work_arrangement_ok=true`" — with "apply this table, and nothing
     else" overriding the table's own "the listing only has to match ONE of them" bullet.
     The hybrid carve-out is about the *listing* being hybrid; it was being read as making a
     hybrid *candidate* compatible with everything. The remote row is now "stated preferences
     NOT including Remote", and the prompt states explicitly that Hybrid on the candidate
     side is not a wildcard. The on-site row is unchanged (still "stated ONLY Remote") —
     deliberately, since that half is the retention-sensitive one.
   * `rank_gate`'s HARD DOWNGRADE (d) was a pure FEASIBILITY test — "clearly cannot work
     given the candidate's stated location" — which a remote listing always passes. It is now
     split into a GEOGRAPHY half (unchanged) and a STATED ARRANGEMENT half.
   * The final judge's DISQUALIFIER 2 ended "**If location is remote** … do not raise a
     location objection", and `NO LOCATION COMMENTARY` separately forbids mentioning
     arrangement in `concerns` — so the judge could neither reject a remote role nor even
     flag it. Same two-check split; `snapshot.build_snapshot` now emits a dedicated
     "Work arrangement wanted (binding | a preference): …" CV line carrying the row's
     Hard/Soft, so the judge knows whether a mismatch excludes or only deprioritises.
     Folded into the `Location: <city> (<work types>)` parenthetical it read as a footnote
     to the place, which is part of why the rule collapsed into geography alone.
   Note where enforcement actually bites for a default profile: work-type rows default to
   **Soft** (`config.enforcement_for`), so `hard_axes` is empty, the cheap gate only demotes
   (one soft-axis failure), and the judge only deprioritises. The mechanical filter **used to
   be** `rank_gate`'s ≤15 cap landing under `RANK_REJECT_SCORE_FLOOR` — the same way the
   salary floor, also a Soft-by-default type, was enforced (rule e). That is no longer true
   and the distinction matters: a Soft-enforced arrangement/salary mismatch now goes to
   `_rank_prompt`'s **SOFT-PREFERENCE MISMATCHES** section, which sets `soft_violation` and
   leaves the score alone, and `_selection_score` demotes it by
   `SOFT_VIOLATION_SELECTION_PENALTY` instead of eliminating it. See that constant and
   `RANK_REJECT_SCORE_FLOOR` for why the two had to be separated before the floor could
   move. The Hard path is unchanged — those rules stay in HARD DOWNGRADES and still cap at
   15. Flipping Dashboard →
   Preferences → Work style to **Hard** adds `_work_arrangement_ok` to `hard_axes` (an
   unconditional cheap-gate drop, no `MIN_RESULTS` backfill) and makes it a judge
   disqualifier. The default was left Soft on purpose, for two reasons: the cheap gate
   reads a ~455-char teaser, and this is the exact axis whose over-firing caused the
   15/26 retention collapse; and a user who ticks these without much thought and then
   searches should still see a strong role they'd probably take. **What Soft does NOT
   have is any user-visible trace** — the judge is barred from mentioning arrangement in
   prose (`NO LOCATION COMMENTARY`) and `work_style` renders as a bare fact chip, so a
   Remote pick under an On-site/Hybrid profile reads as a bug rather than as the setting
   working as designed. `WorkStylePicker` closes that with a note under the buttons
   naming the arrangements that can still appear ("Soft filter: you may still be shown
   **Remote** roles…"), shown only when the combination makes it meaningful: Soft, at
   least one arrangement picked, at least one unpicked. Keep the note if the enforcement
   semantics change — it is the only place in the UI that says a Soft work-style
   preference does not exclude.
   `listing_ok` catches listings that clearly
   aren't one specific job posting at all — a job board's own search-results/category
   page or generic aggregator blurb that slipped past discovery-time filtering (e.g.
   `_looks_like_category_page`, which only runs at discovery time on Google-organic
   results and can't see this in a stored snippet) — since re-scraping/ranking/judging
   non-job text wastes every downstream stage. **It is explicitly told to read past board
   page furniture** (v12): where a candidate carries scraped `full_text`, crawl4ai keeps the
   board's own chrome, so an Adzuna posting arrives opening with `## <Title> jobs in <City> /
   Create email alert / ❮ back to last search` — verbatim the category-page pattern this axis
   is told to reject, sitting inside its 2000-char window, on an *unconditional hard drop*.
   Re-running the ground-truth sample with full text instead of teasers sent this axis from 3
   failures to **50** (retention 42%), i.e. supplying the gate more text was actively
   counterproductive; after the fix it's 0, and the full-text pass holds the same 88.5%
   retention as the teaser pass while catching nearly twice as many bad roles (44% vs 23%).
   That ordering — more text strictly better — is what would make widening
   `REED_ENRICH_PRE_GATE_CAP` worth doing; it was not true before v12.
   `hard_gate_ok` enforces the candidate's OWN stated non-negotiables — the `avoid` and
   `must_have` attribute chips (see the data model above) — and is false only when the
   listing *clearly* involves an avoid item or clearly can't satisfy a must-have,
   defaulting to true when the listing is silent, so a thin listing isn't dropped for
   merely failing to confirm. Unlike a sector or soft-axis drop, a `hard_gate_ok` (or
   `listing_ok`) failure
   is also excluded from the `MIN_RESULTS` floor backfill below: resurfacing a job the
   candidate explicitly said to avoid (or that isn't a real job posting at all) would
   defeat the point of asking. The other five
   axes are individually
   soft — a job failing only *one* of them still proceeds to `rank_gate` (which gives it
   a real per-job fit score) and reaches the final judge carrying it as a hint, same as
   before. How many soft failures earns a hard drop is now **dynamic per gate round**
   (`full_auto.dynamic_hard_drop_threshold`, shared by `screen_gate`'s own diagnostic log
   and `engine.py`'s actual decision so the two can't disagree): normally 2+ failures
   (two independent clear-mismatch signals agreeing is confident enough to skip paying
   for `rank_gate`/the expensive judge on it, without the risk either signal carries
   alone), but if more than `_GATE_CLEAN_ROUND_FRACTION` (**0.35**) of a round's
   in-sector candidates pass every soft axis
   clean, that's a sign the round is thin on genuine mismatches rather than that
   everyone really fits, so the threshold tightens to 1+ for that round. Added after a
   live run showed `86 in-sector, 72 pass all soft axes, 1 hard-dropped` — a fixed 2+
   floor was barely discriminating on a round that clean. The fraction was loosened
   from 0.5 to 0.35 (i.e. the gate tightens **sooner**) when `RANK_EXAMINE_BUDGET` went
   to 240: the gate's job is to stop the mid tier paying to rank hopeless candidates, so
   tripling the intake both makes that saving worth more and removes the reason to be
   lenient — a wrongly-dropped borderline job used to cost a scarce slot out of ~40
   examined, where now there are 200 more candidates behind it. The soft-fail check itself
   only marks an axis false on a *clear* mismatch, defaulting true when unsure, so even
   the tightened 1+ threshold needs one real, confident signal, not a coin-flip. The
   work-arrangement axis specifically first classifies the *listing's own* arrangement —
   explicit remote/distributed/wfh wording → remote; explicit hybrid wording → hybrid;
   a stated city/office location with no remote/hybrid/wfh wording at all → **on-site**,
   not remote-by-default — then compares that against the candidate's stated
   Remote/Hybrid/On-site preference(s) (`profile["work_types"]`, sourced from the same
   `location` attribute rows the free-text city comes from, split apart in
   `snapshot.build_snapshot`). Before this, the axis's own prompt text referenced "the
   candidate's own requirements/preferences above" while neither the candidate's location
   nor their work-type preference was ever actually included in the prompt — the axis
   compared against nothing and so could only ever default to a pass. **Guarantees a
   per-cluster floor** (`MIN_RESULTS`) via a three-tier backfill — primary (passed, or
   failed fewer axes than the round's threshold) → demoted (failed at/above the
   threshold) by embed_score → full pool by embed_score — so the cheap gate can never
   starve a cluster to zero (the full-text final AI stays the real seniority/requirements
   judge for anything that rides through) → fair-allocate to `TARGET_POOL` → **cheap
   numeric rank stage** (`full_auto.rank_gate`, also run once *per cluster* with that
   cluster's own roles — a profile-wide call was diluting a minority cluster's fit scores
   with the candidate's other target-role cluster's context, a real bug fixed once
   already): a 0–100 fit estimate per job, scored against an absolute cutoff
   (`RANK_REJECT_SCORE_FLOOR`, replacing an older relative bottom-20%-of-batch trim) —
   anything below the floor is dropped **per cluster, before** fair-allocating to
   `JUDGE_POOL` (40), so a cluster that happens to score lower can't lose more than its
   own share before fair-allocate ever runs. **The mid tier is deliberately run wider
   than the judge**: it accumulates toward `RANK_TARGET_POOL` (80) judge-eligible
   approvals out of a run-wide `RANK_EXAMINE_BUDGET` (240, later 320 — see below)
   examined, and the judge then
   takes the best `JUDGE_POOL` (40) of those. Before this the accumulation target *was*
   `JUDGE_POOL`, which made the judge's input "whatever survived" rather than a curated
   best-of — a live run reached it with 35 candidates, 5 of them in one cluster, so the
   judge could only pick the least-bad of five and did. Both budgets are run-wide totals
   split evenly across active clusters (so a 3-family profile costs the same as a
   1-family one, with thinner shares), replacing the old per-cluster
   `SINGLE_CLUSTER_EXAMINE_CAP`(80)/`MULTI_CLUSTER_EXAMINE_CAP`(40) pair.
   `RANK_REJECT_SCORE_FLOOR` moved 40 → 55 → **50** across that change: at 40 it was a
   no-op (a live run showed `85 gate survivors -> 85 judge-eligible`); 55 made it a
   *selection* mechanism, which is the wrong instrument — a cutoff set high enough to
   select is also high enough to starve a thin cluster. At 50 it is back to being a
   "not clearly a no" bar, and selection is done by the wide-pool-plus-top-N cut, which
   cannot starve. `rank_gate`'s own log line reports the batch's min/max/avg score so a
   floor that's silently toothless again is visible without a gate_cache query. The
   rank-side `MIN_RESULTS` floor backfill (below) still guarantees
   a cluster with any gate survivors reaches the judge.
   Ordering into the judge pool is `engine._selection_score`, **not** `_rank_score`:
   the model's score plus `RICH_TEXT_SELECTION_BONUS` (3.0) for a candidate that
   doesn't need a phase-5 page fetch (`_needs_full_scrape` false — ATS text, a
   Reed-enriched or previously-persisted `full_text`, or a long-enough snippet). Purely
   a tie-break, so among candidates the mid tier rated equally the judge pool fills with
   the ones it can actually read — which shortens phase 5 (the run's longest tail and
   its main anti-bot exposure) and gives the judge better text. Kept separate from
   `_rank_score` on purpose: the card's "Fit estimate N/100" chip and
   `RANK_REJECT_SCORE_FLOOR` both still use the unmodified model score, so rich text can
   never lift a genuinely poor job over the floor, and gate_cache still stores the
   model's own number so the bonus can be retuned without invalidating a single score.
   `rank_gate`'s
   prompt (`_rank_prompt`, cache gate "rank_v13.{intent hash}") carries **the candidate's
   own `intent_text`** — which used to reach only the final judge, leaving this stage
   scoring function fit against bare role TITLES with no access to what the candidate
   meant by them, precisely the signal needed to tell an ambiguous title's two
   professions apart. The intent hash rides in the *gate name* rather than in
   `_profile_signature` (shared with `screen_gate`) so editing that box re-scores
   without also re-running every cheap screen call for a guaranteed identical answer.
   `screen_gate` is deliberately still **not** shown intent: its sector axis is scoped
   to job FUNCTION on purpose, and feeding free-text aspiration into it is the same
   drift that removing the `sectors` guess fixed. The prompt also carries the candidate's location/
   work-type preference/salary floor (previously never in this prompt at all — an
   on-site-Cyprus listing scored 84 for a UK-remote candidate with no way to know it
   was even rejectable) and a **HARD DOWNGRADES** section mirroring the final judge's
   DISQUALIFIER rules at the cheap tier: a clearly-stated experience bar the
   candidate's evidence doesn't meet (evidence tagged self-directed/academic/
   ai-assisted doesn't count as paid experience — the judge's evidence-strength rule,
   which this stage used to lack entirely), a required named credential/tool with no
   evidence, a closed/expired listing, a clear location/arrangement conflict (same
   on-site-unless-stated-otherwise classification as the gate axis and judge rule), or
   a salary clearly under the stated floor — each caps the score at 15, added after a
   26-mismatch audit showed this stage scoring 80+ on listings the judge then
   hard-rejected on exactly these grounds. All downgrade rules require CLEAR visible
   evidence: listings carrying only their source API's ~500-char teaser are tagged
   `[truncated source teaser ...]` in the listing block (`RANK_TEASER_MARKER_CHARS`)
   so truncation is never read as absence — Adzuna has no fuller pre-scrape text to
   give it (see the text-supply note below). Between rank and fair-allocate,
   **near-duplicate suppression** (`engine._suppress_judge_duplicates`): same
   normalized company + title + near-identical text prefix (a recruiter template
   re-posted per city — `_find_soft_duplicate` deliberately keeps those as separate
   store rows because their locations differ) keeps only the top-ranked copy in the
   judge pool, so the freed slots go to real candidates; the dropped copy keeps its
   cached rank score and no verdict, so it can resurface if the kept copy dies.
   That exact-prefix test only catches a *verbatim* repost, and cross-BOARD
   syndication is not verbatim: a live run judged "BI Analyst / Erin Associates"
   twice — once from reed.co.uk, once from jobs.womenforhire.com — and the judge
   itself wrote "Duplicate of Job 5" as the second one's reject reason. The two
   copies wrap the same description in different board chrome, truncate it at
   different lengths, and (once one has been enriched/scraped and the other hasn't)
   hold very different amounts of it, so their 400-char prefixes can never match.
   A second test (`_same_vacancy`) covers it: word-4-gram sets compared by
   **containment** (shared ÷ the *smaller* side), not Jaccard — a 455-char Reed
   teaser against the same vacancy's 4,000-char scraped page has a Jaccard of ~0.1,
   while containment asks the question that actually matters, "is the shorter copy
   essentially wholly inside the longer one". Deliberately narrow: same normalized
   company **and** title are required first (so it only ever adjudicates candidates
   the prefix test was already trying to separate), blank-company rows are excluded
   (nothing to anchor on — `_dup_key`'s own blank-company path still covers
   aggregator reposts), and `_DUP_MIN_SHINGLES` demands real text on the shorter
   side. A false merge is cheap and recoverable for the same reason the prefix test's
   is — the dropped copy keeps its rank score and takes no verdict.
   **Both of those tests keyed on the TITLE, which is the field a recruiter
   varies.** A live run showed "Junior Application Developer" and "Junior
   Software Developer" (Plum Personnel, same GU98AD, same "Circa 30,000",
   word-for-word identical body text apart from the title) graded Strong fit and
   shown at ranks 1 AND 2. Nothing could catch it: `identity_hash` differs
   (different Reed ids), and `_find_soft_duplicate`, `_dup_key` and
   `_same_vacancy` all require an exact title match. The near-text bucket is now
   keyed by `_canonical_title_key` — the title reduced to a SET of tokens with
   noise words dropped and a small explicit synonym map applied
   (developer/engineer/programmer, software/application/app, jr/junior, sr/senior)
   — so word order and near-synonyms agree while seniority stays significant.
   **Do not widen this to company-only**, however tempting: over this store's
   44,654 same-company/different-title pairs, 7.9% reach ≥0.80 text containment
   and the high end is dominated by genuinely DIFFERENT vacancies sharing a
   template — Wise's "Senior Data Analyst - Growth" vs "- FinCrime Operations"
   (500-char Adzuna teasers that are pure company boilerplate and never mention
   the role, containment **1.000**), TransPerfect's "Croatian language trainer" vs
   "Slovenian language trainer", "Back End" vs "Front End". Those are one template
   with one word swapped, i.e. mechanically the same shape as the Plum case, so
   TEXT CANNOT SEPARATE THEM — only the titles can. Measured over the 4,656
   different-title pairs whose text already passes `_same_vacancy`: **28 merge,
   4,628 are left alone**, and every merge was hand-checked as one vacancy
   ("Director of Finance"/"Finance Director", "Data & Research Analyst"/"Research
   & Data Analyst", "Certified Nursing Assistant - CNA"/"CNA - …"). Extend
   `_TITLE_SYNONYMS` only with words naming the same JOB; a specialism, product,
   region, language or seniority belongs nowhere near it.
   Widening the match made WHICH COPY SURVIVES matter, which it previously didn't:
   the two copies can now differ enormously in text (the Plum pair was a 453-char
   teaser against the same vacancy's 4,299-char description). The pool is
   `_selection_score`-sorted so the best-SCORING copy takes the slot, but score
   says nothing about text, and suppressing on score alone would have sent the
   judge the teaser and discarded the full description — worse than not
   deduplicating at all, since before this both copies at least reached the judge.
   `_keep_richer_copy` therefore substitutes the better-read copy into the
   winner's slot, carrying the winner's score over so ordering is untouched
   (`RICH_TEXT_SELECTION_BONUS` is only 3.0 points and can't be relied on to
   decide a pairing). Note the slot is recorded as `(that cluster's kept list,
   index)`, never a bare index: the dedupe maps are shared across clusters while
   `kept` is per-cluster.
   Count lands in `funnel_counts.judge_dupes_suppressed` (rendered on the Settings
   run-funnel panel) → **provisional
   early display reconcile**: by this point in the pipeline, provisional Role rows
   already exist for the /search page's "Verifying…" cards — interim rows have been
   upserted incrementally as each gate/rank round finished, not just here (see the
   `Role` data-model bullet for the full lifecycle) — so this step reconciles them
   against the now-final, fair-allocated judge pool: fixing fit_rank/membership up to
   the true top-N before the tail phases run → optional
   Phase 5 full-page scrape, **run per cluster and pipelined straight into that
   cluster's Phase 6 judge** (skipped
   for ATS-sourced jobs, for any snippet already long enough to judge —
   `SNIPPET_SUFFICIENT_CHARS`, deliberately above Adzuna's exact-500-char API truncation
   so Adzuna snippets don't wave through as "sufficient" by coincidence — and for
   anything with a persisted `full_text` from a prior run — see
   `engine.py::_needs_full_scrape`) → **Phase 6 final LLM evaluation runs once per judge
   group** — usually one per cluster, with thin clusters merged into a shared call, see
   `JUDGE_MERGE_THIN_CLUSTER_MAX`/`_judge_groups` — each a **single** expensive call
   (`full_auto.final_evaluation_split`)
   returning a strict `strong` list, a `backup` list of every other worth-applying-to
   role (both capped at `FINAL_PICKS`, both SHOWN — see the v26 note below), and
   **two exclusion lists that account for every remaining job**: `disqualified` (hard
   DISQUALIFIERS hits, reason must carry the verbatim quoted clause) and `not_selected`
   (passed the disqualifiers but wasn't among the best picks). Every `job_number` the
   judge is given must appear in exactly one of the four lists. Both exclusion lists ride
   home merged in the third return slot, each entry tagged **`_disqualifier`** True/False
   — a 3-tuple because every caller and the failure sentinel are built around one, and
   both are consumed identically (a reject verdict plus the AI's own reason, persisted
   into `eval_analysis`). Only `disqualified` used to be requested at all, so a job the
   judge merely passed over recorded **nothing**: a ground-truth audit found 15 of 24
   rejects in the judge pool carrying no reason, which made it impossible to tell a job
   that was beaten from one the cheaper tiers had misread on its way in — the single
   biggest blocker on auditing this pipeline, and free to fix. Keep the two kinds
   distinct downstream: `funnel_counts["final_disqualified"]` and the per-cluster
   `judge_disqualified` count **only** `_disqualifier` entries (that count is the
   diagnostic for "is the judge hard-rejecting anyone", and folding in the out-competed
   ones would inflate it to nearly everything), while `funnel_counts
   ["final_reject_reasoned"]` counts both. A reject with no matching entry still falls
   through to a blank analysis, so a judge that stops honouring "account for every
   job_number" shows up as `final_reject_reasoned` falling short of
   `final_fresh_judged - strong - backup` rather than as a silent regression.
   The judge is deliberately structured as an explicit
   **reasoning** step, not a similarity score (`full_auto._FINAL_EVAL_REASONING`): it is
   told to (A) read the JD on three axes — required vs nice-to-have, the *real* seniority
   bar (an "entry-level" label can be marketing), and actual day-to-day vs aspirational
   listing language; (B) assess **want-fit separately from can-do-fit** — a candidate can
   be qualified for a role they plainly don't want, and that must not score strong on
   skills alone — weighing evidence *strength* over mere presence; and (C) name the
   single **weakest link** per pick. These land as `want_fit`/`can_do_fit`/`weakest_link`
   in the schema, are persisted into `eval_analysis` (so a cache-served verdict renders
   identically to a fresh one), and are rendered by `engine._compose_analysis`. There is
   still no separate JD-summarisation pass — the three-axis read happens inside this same
   call, on the `full_text` it already receives, deliberately avoiding an extra paid call
   per job. **Any edit to these prompts must bump `FINAL_EVAL_PROMPT_VERSION`** (now 26)
   or every already-persisted verdict is served stale forever.
   **`fit_level` is derived mechanically from the model's own step-D requirements
   checklist and `concerns`, not from an overall impression, and is decoupled from which
   list the pick landed in** (v16). A live run graded 9 of 11 picks `strong` and 2
   `very_strong`, with zero `ok`/`stretch` — no discrimination at all — because nothing
   in the prompt tied the grade to anything: the model listed a decisive concern ("no
   hands-on exposure to PLCs, industrial control systems, robotics") and still returned
   `strong`. Two rules fix it. The **rubric** (in `_FINAL_EVAL_SCHEMA`) sets `very_strong`
   = all core requirements met + no concern touching a core requirement (it also required
   `sector_match` until v26 removed that field),
   `strong` = all core met + at most one such concern, `ok` = one unmet core or 2+ such
   concerns, `stretch` = 2+ unmet core; a `strong`-list pick may legitimately grade `ok`,
   so the strong/backup split (a disqualifier + worth-showing decision) no longer forces
   the badge. The **checklist discipline** (step D) is the other half, and the more
   load-bearing one: the same run's checklists were being drafted at whatever level of
   abstraction made every item "met" — the industrial role's three core items were
   "graduate-level technical foundation", "ability to learn automation work" and
   "programming and analytical problem-solving" (unfailable by construction), with
   *industrial automation/control systems/robotics* filed as **secondary**. Step D now
   forbids capacity/attitude phrasings outright, requires the JD's own specificity
   ("Computer Science degree", not "a related technical degree"), and states that the
   domain-defining asks are core by definition and cannot be demoted to secondary
   because the candidate lacks them. Since the rubric reads off the checklist, a padded
   checklist is the way an unearned grade gets produced — fix that before touching the
   rubric thresholds.
   **v28 is the checklist-COVERAGE rework, and its first finding is that the checklist
   had been shrinking run over run while the postings got longer** — 6.00 items (run 20)
   → 4.33 → 4.33 → 3.75 → 3.67 (run 24). Over 160 persisted checklists the median is 4
   items / 3 core, only 20 reach step D's own stated "4-10 core, 2-6 secondary", 46
   carry no secondary item at all, and size barely tracks the posting's length
   (pearson r = 0.25). Live: a 4,979-char JD naming Node.js/TypeScript, Ruby/Rails, Nuxt,
   AWS CDK and Salesforce produced a ONE-item checklist ("Full stack / software
   engineering", met) graded `strong` with zero concerns; another used the job title
   itself as its only item. Because the rubric reads `core` items ONLY, this does not
   produce a cautious grade — **it produces an inflated one**, silently.
   **The cause is OUTPUT PRESSURE, not a wrong rule, and that distinction is the reason
   to read `scripts/audit_judge_checklists.py`'s `tok/pick` column before its size
   column.** Judge completion tokens per pick object fell 1305 → 1397 → 665 → 655 → 491
   across those five runs and checklist size tracks it monotonically: `judge_pool_size`
   grew 22 → 40 (the cap) and v26 widened `backup` from 3 to `FINAL_PICKS`, roughly
   doubling the pick objects one call must emit, while completion tokens rose only
   sub-linearly. Faced with that, the model economised on the one field v27 opened by
   calling it "internal reasoning only -- not shown to the candidate". That framing is
   gone; step D now says what the field is (the input `fit_level` and `concerns` are
   computed from) and names prose as what to shorten first.
   Four other changes, all measured with `tests/judge_harness.py`: a **shape test**
   ("could a competent person in this field plausibly FAIL this item?"), **ONE ITEM PER
   ASK** (a heading like "Full stack / software engineering" is not a capacity, so the
   v16 rule never caught it), **the posting's own heading decides `core`** (a live pick
   filed the two asks under "Must have hands on experience with SQL, and data
   visualisation tools" as *secondary* while promoting two Key-Responsibilities duties to
   core — which, since the rubric reads core only, made the posting's own stated bar
   unable to affect the grade), and **the hint is a floor, not a ceiling** (the
   `[key requirements]` pass reads the truncated opening, i.e. the blurb; a checklist
   matching the hint and adding nothing is the symptom of skipping the ADD step).
   > ⚠ **Pushing for a longer checklist without the shape test makes things WORSE, and
   > the first v28 draft did exactly that.** It raised the checklist on 14 of 14 paired
   > listings and moved two picks to `very_strong` — on items like "strong attention to
   > detail" and "ability to work independently", which always come back `met` and which
   > the rubric then counts toward "every core requirement met". Padding and
   > under-filling do the same damage by opposite routes. A second draft over-corrected
   > the other way (a blanket "leave boilerplate off"), *cutting* core items 4.82 → 3.09;
   > the shipped wording replaces an unfailable item rather than deleting it. The
   > harness's `unfailable_items` counter exists because of this and should be read on
   > every step-D change.
   **What v28 is NOT claimed to do.** The harness could not reproduce production's thin
   checklists at all: on the identical rows where production stored 1–3 items, the
   unmodified v27 prompt produces 5–10 today (mean 6.55). So the production symptom is
   not attributable to the prompt, and no prompt edit should be expected to fix it —
   the structural lever is the per-call output budget above (`FINAL_EVAL_MAX_JOBS_PER_CALL`,
   and how many pick objects `backup` asks for). What v28 *is* measured to do, across
   four paired A/B runs: remove unfailable padding every time (7→1, 5→0, 6→2, 1→0),
   deflate unearned `very_strong` (all five became `strong` on the worst-row sample),
   raise core items where there was room (+1.30 on the worst rows, flat on a balanced
   draw), and never shrink the results page (picks 10→12, 14→14).
   **The three output-budget levers, measured — two work and one is a no-op.**
   * **An output-token ceiling is NOT a lever, and the code never had one anyway.**
     `llm()` set no `max_tokens` at all, so every call ran at the model's default. The
     question "is that default binding?" is now answered rather than assumed:
     `_record_llm_usage` counts responses that stopped on `length` instead of `stop`,
     surfaced as `tokens_{stage}_length_capped` on the run-funnel panel. Measured **0
     across every judge call**, at both chunk sizes. Nothing was ever being truncated,
     so raising a ceiling cannot buy room. `FINAL_EVAL_MAX_OUTPUT_TOKENS` (32000) was
     added anyway as a **guard, not a lever**: a truncated `require_json` reply is a
     parse failure that vanishes down `_run_final_eval`'s fail-open path, so a future
     model-default change would look exactly like the model choosing to write less.
   * **Splitting into smaller calls DOES buy budget, and is FASTER** —
     `FINAL_EVAL_MAX_JOBS_PER_CALL` 20 → **10**. Same prompt, same 20 listings:
     587 → **885 completion tokens per pick (+51%)**, wall clock 81.2s → **73.5s**,
     because chunks run concurrently in a `ThreadPoolExecutor`. It also eases the 90s
     read timeout the constant exists to protect. Cost is the re-paid ~12k system
     prefix per extra call, which the 24h prompt cache is there to absorb — measured
     **63% cache hit** over 2 concurrent calls (production run 24 managed 29%).
     **`tokens_judge_cached_tokens` is the number to watch**: if that ratio falls, the
     extra calls are being billed in full and this trade stops paying.
   * **But the extra budget did not move the checklist** (5.33 → 5.31 items at
     +51% tokens/pick), which is consistent with the harness never reproducing the
     production symptom in the first place — there was no suppression there to relieve.
     So the chunk-size change is kept for the speed, the headroom and the timeout
     margin, **not** on a claim that it fixes checklist size. `FINAL_PICKS` was left at
     12 deliberately: it is the only one of the three levers that costs the candidate
     results, and smaller chunks already cut per-call pick objects without doing that.
   v18 adds the two remaining halves of that discipline. (a) Step D's
   **fourth** checklist rule, the mirror of the widening error: an ask the posting itself
   says it TRAINS for, labels beneficial/desirable/not essential, or states alongside a
   weaker actual minimum is **secondary**, never core, and may never be the concern that
   drives the grade down — the item still sits on the checklist `met: false` (the
   capacity rule is unchanged), but an employer budgeting to teach X is not screening on
   X. A live pick was graded `ok` partly on "no evidence of uploading data into a custom
   workplace analytics platform" for a posting whose own text promised a two-week
   training period on that platform. Step C carries the display half: such a gap must
   quote the JD's framing in the same breath and never be listed first. (b) Step E's
   **wording lock** — `top_match_reason` was written AFTER `fit_level` and had to match
   its register, with an explicit per-grade vocabulary and an explicit ban on the
   qualifier-laundered forms ("a strong graduate fit", "a strong entry-level option")
   that a live run produced under an "OK fit" badge three times. *That whole rule went
   away with the field in v23 (below) — if a narrative verdict field is ever
   reintroduced, reintroduce the lock with it.*
   **v23 replaced step E's `top_match_reason` with APPLICATION GUIDANCE** —
   `filters_on` (2–4 of the step-D checklist items this employer will actually screen
   on **and** the candidate can evidence, in the JD's own words) plus `highlight` (2–3
   second-person sentences naming which of the candidate's own projects/tools/results to
   lead with against them). The card renders both under a **"Highlight when applying"**
   heading as `This role likely filters on: …` followed by the guidance
   (`engine._compose_analysis` → `§apply-highlights`; the retired `§ai-reasoning` marker
   is still parsed by `RoleCard.tsx` so pre-v23 rows keep rendering until re-judged).
   The narrative was cut because it was the fourth thing on the same card arguing the
   same verdict — after the grade badge, the `role_type`+`summary` headline and the
   `can_do_fit` line — and the one output field that gave the candidate nothing to act
   on. Two constraints carry the value: `filters_on` **excludes anything the candidate
   has no evidence for at all** (that is a gap, and gaps belong in `concerns` — this
   field is only what can go on the page), and `highlight` must name evidence that
   actually appears in the profile, framing weak/self-directed evidence honestly rather
   than dressing it as commercial. Two things step E used to own moved to `concerns`: a
   false `sector_match` trade-off (step G, itself removed in v26) and a want-fit mismatch
   (step B).
   v23 also closed a **nice-to-have vocabulary hole**: "ideally", "preferably",
   "desirable", "a plus", "a bonus", "an advantage", "welcome", "would be great" were
   absent from QUOTE-THEN-CLASSIFY's SOFT list and from step D's rule (b), so
   "ideally Databricks or Snowflake" could be read as a bar — including a list of named
   tools introduced by one of those words, which is the form that misled. And the
   `not_selected` reason field carried none of the discipline steps C/D impose on
   `concerns`: it is now explicitly held to the same bar, and may never cite a
   nice-to-have or a trained-for ask as the reason a role was passed over ("out-competed"
   is the honest answer there).
   **v26 is the leniency/output rework, and it changes what the judge's two lists MEAN.**
   * **`backup` is no longer last-resort filler.** It was capped at 3 ("least-bad
     survivors") and `engine._evaluate_cluster` used it **only when a cluster had zero
     strong picks** — so a run could finish with 3 picks while a dozen judged,
     perfectly-applicable roles sat discarded, and every other such role had been filed
     under `not_selected`, which carries only an internal audit phrase and therefore
     **cannot be displayed at all**. Now: capped at `FINAL_PICKS`, described in the prompt
     as SHOWN to the candidate, and `picks = strong_tier + backup_tier` unconditionally.
     The run-wide assembly is grade-ordered (`_VERDICT_GRADES`) and capped at
     `FINAL_PICKS`, so appending backups can never displace a better-graded pick — it only
     fills slots that would otherwise go empty. `eval_fallback` is still tagged only when
     there were NO strong picks, but **it no longer prints a banner**
     (`engine._SILENT_FALLBACK_TAGS`). It used to render *"X matches were thin this run —
     showing the closest available instead of only confident picks"*, which was true under
     the pre-v26 design and is not under this one: a run made entirely of backup picks is
     now a normal run of verified, applicable roles, so the banner framed a verified pick
     as a consolation prize — exactly the reason the per-card "Closest available match"
     line went in the same rework. The tag is still SET and still lands in the run
     diagnostics; suppression is one entry in a frozenset, and it has to be done there
     rather than by deleting the message, or the tag falls through to
     `_compose_fallback_warning`'s generic multi-cluster line and says the same thing in
     vaguer words. Every other fallback reason (`cluster_skipped`, `gate_fallback`,
     `broadened`/`floor_fallback`) still speaks. Two knock-on
     fixes: the scam-verify backup fallback was **removed** (backup is now already inside
     `picks` and has been through the same filter, so re-adding it would resurrect
     listings just corroborated as scam and persisted as reject overrides), and the
     `MIN_RESULTS` backfill retry — the single most expensive optional call in the run —
     now fires far less often because `picks` is fuller. Harvesting `not_selected`
     directly was considered and rejected: those entries have no `summary`/`can_do_fit`/
     `fit_level`/`concerns`, so they would render as blank cards, and they are persisted
     as rejects. Widening `backup`, which already carries display-quality output, gets the
     same roles onto the page for free.
   * **A WISH-LIST bar.** Employers hire "under-qualified" candidates far more often than
     their adverts suggest. Three tells that a posting's stated requirements are a
     recruiter's ideal-hire sketch rather than a bar — posted by a **recruitment/staffing
     agency** (a padded summary of a manager's brief, written for a wide funnel); a
     **contract/interim/fixed-term/day-rate** role (lower, more negotiable bars, less
     long-term risk); a **long "essential" list** (~8+ items, especially split into
     technical/analytical/communication groupings) and/or **"negotiable"/"competitive"
     pay**. At the judge these push toward including a role in `backup` and toward the
     more generous of two adjacent `fit_level`s. Mirrored at the mid tier as a **WISH-LIST
     EXCEPTION** on `rank_gate`'s HARD DOWNGRADES (a) and (b) *only* — those two are about
     stated expectations, so a wish-list posting takes an ordinary DEPTH FIT deduction
     instead of a score cap. It explicitly does NOT apply to (c)–(h): closed listings,
     geography/right-to-work, max-age elimination and apprenticeship over-qualification
     are facts about **eligibility**, not negotiable expectations. Not added to
     `screen_gate`: its soft axes only demote and are floor-protected, and touching that
     prompt costs a `screen_v` bump that re-screens the whole store.
   * **`sector_match` and reasoning step G are gone**, along with DISQUALIFIER 5's closing
     "judged separately as a ranking signal" paragraph, the sector clauses in steps B/E,
     and the `very_strong` rubric's dependency on it. Rule 5 is renamed **PROFESSIONAL
     FIELD FIT** and now says explicitly that it is about the professional FIELD only,
     never the industry/sector/cause, and that an industry objection may never appear
     anywhere in the output. The field's only user-visible effect was a *"this is not
     within your stated clean-energy, science, climate or nonprofit sector interests"*
     item in `concerns` — noise on a card about whether the candidate can do the job —
     while silently gating `very_strong`. `snapshot.build_snapshot` no longer emits the
     "Sector interests:" CV line and `profile_intel._BACKGROUND_TYPES` no longer carries
     `sector_target` (`PROFILE_INTEL_VERSION` 9 → 10). **`sector_target` rows still exist
     and still auto-fill from the CV** — they are a DISCOVERY-side signal only now
     (`services/harvest.py` picks ATS-harvest keywords from them, which is how a
     charity-sector candidate reaches charity employers at all). Nothing the candidate
     reads is derived from them.
   * **Step G is now STRENGTHS**, required for an `ok`/`stretch` pick and omitted for
     `very_strong`/`strong`: 1–3 concrete things the candidate genuinely DOES bring,
     each naming a step-D item marked `met: true` and the candidate's own evidence for
     it. Those two grades' cards previously showed a list of gaps and **nothing
     alongside them** for a role the judge was recommending. `concerns` is capped at 3
     (asked for in step C, enforced by `_sanitize_bullets`, which caps `strengths` to
     match) — a longer list stops being things to address and reads as "don't bother".
   * **A WORDING rule on whose side a shortfall is stated from.** `can_do_fit`,
     `concerns` and `not_selected` reasons must describe a gap as something the POSTING
     asks for or prefers, never as a deficiency in the candidate: "you would be a stretch
     because the role expects X" becomes "the posting prefers X". "you would be a
     stretch", "you lack", "you fall short", "you are under-qualified", "you do not
     meet", "you are not a fit" are banned outright.
   Card-side (`engine._compose_analysis` / `RoleCard.tsx` / `lib/types.ts`): the
   `⚠ Closest available match — no role fully met the bar this run.` line is **gone**
   (it fired for every non-strong-list pick, which is now a routine outcome, and framed a
   verified role as a consolation prize); `⚠ You lack N aspects:` is now `⚠ Note that:`
   (the count invited the card to be read as a score, and "you lack" states a property of
   the candidate); strengths render as `✓ You have:` + bullets; and **`VERDICT_LABEL` no
   longer labels `ok`/`stretch` at all** — the badge is the first thing read on a card and
   "Ok fit" told the candidate to discount a role the judge had just verified, while the
   card body says the same thing with the specifics attached. The grade still does its
   real job (ordering, `_VERDICT_GRADES`), it just isn't printed; the schema tells the
   model this explicitly so it grades honestly rather than protectively. `VERDICT_LABEL`
   is now a `Partial<Record<…>>` and `RoleCard` already guarded on the lookup. Note the
   `§qualification` parse rule: a `✓` line ENDING IN A COLON is a bullet-list heading
   (collapsible, with its bullets), a `✓` line that doesn't is the always-visible
   `can_do_fit` verdict.
   **Tier hand-off — what the cheap/mid stages pass forward so the judge re-derives
   less** (`full_auto._final_eval_job_block`, all as bracketed notes in each job block;
   the judge's system prompt has a `WHAT THE BRACKETED HINTS IN A JOB BLOCK ARE`
   paragraph telling it these came from passes that saw LESS text, so they say where to
   look and never what to conclude): `[screen note: ...]` = every non-`ok` axis code
   from `screen_gate`'s packed `_gate_reason`; `[key requirements: ...]` =
   `screen_gate`'s extracted JD asks with their required/nice-to-have tag;
   `[earlier screening pass thought: ...]` = `rank_gate`'s own one-line `_rank_note`,
   which was previously computed and then thrown away. The rank SCORE is deliberately
   **not** passed — a number anchors a grade, a phrase points at something checkable.
   The `[key requirements]` hand-off is the load-bearing one and doubles as a
   calibration fix: reasoning step D now tells the judge to START from those items
   (carrying their required/nice-to-have tag as the core/secondary split) and only then
   add what the fuller text reveals, because they were extracted by a pass that had
   **never seen the candidate** and therefore cannot have been bent to fit them — which
   is exactly the failure the step-D rules above exist to prevent. Note what is
   deliberately NOT delegated: `fit_level`, the met/unmet judgement, and the
   listing FACTS (`work_style`/`role_seniority`/`role_salary`/`deadline`). The facts
   look delegable but aren't — the judge reads phase-5-scraped full text while
   `rank_gate` often saw only a ~455-char teaser, so a handed-down fact would be
   strictly worse than the one it can read itself.
   **`[key requirements]` also goes DOWN a tier, to `rank_gate`** (`_key_requirements_
   text`, shared by both blocks so they can't describe the same hand-off differently).
   It had gone only to the final judge, leaving the mid tier re-deriving the asks from
   raw prose while a cleaner, already-tagged version sat unused on the candidate dict.
   Free — `screen_gate` extracted it from the first `GATE_LISTING_TEXT_CHARS` of the
   same string `rank_gate` reads more of — and its value is precisely the
   required/nice-to-have split, which is what HARD DOWNGRADE (a)/(b) turn on and what a
   scoring pass reading a wall of text most often blurs. The prompt states both limits:
   the list is capped and was read from a SHORTER excerpt, so it is never complete (an
   ask appearing only in the fuller text still counts), and it is an extraction, not a
   verdict — it says what the employer asked for, never whether the candidate meets it.
   Disqualifier rule 5 (SECTOR/DOMAIN FIT) also carries a **shared/ambiguous job title**
   carve-out: some titles name two different professions and are told apart only by the
   duties ("Automation Engineer" = software/RPA vs industrial PLC/robotics; "Analyst" =
   data vs financial/intelligence; "Engineer" = software vs mechanical/electrical). For
   those, a word-for-word match against a target role is explicitly *not* evidence of a
   field match — see the matching carve-outs in `_screen_prompt`'s ROLE FUNCTION FIT
   (which had the inverse rule: a verbatim title match was *presumptively* a match) and
   `_rank_prompt`'s FUNCTION MATCH. All three were changed together and all three cache
   versions bumped (`screen_v11`, `rank_v8`, eval 16 — all since superseded); leaving any one behind reinstates
   the hole at that stage.
   The
   LOCATION/VISA/RELOCATION disqualifier rule
   applies the same on-site-unless-stated-otherwise classification as the gate's
   work-arrangement axis above, then runs **two** checks off it (eval 21): GEOGRAPHY (can
   the candidate physically take it — a remote role always passes) and STATED ARRANGEMENT
   (does it match one of the candidate's stated work types — a remote role passes only for
   a candidate who listed Remote). Keep those separate: collapsing them into the single
   feasibility test the rule used to be is exactly what let remote roles through, since the
   rule then ended "if location is remote … do not raise a location objection".
   **GEOGRAPHY → v22 (+ `rank_v13`)**: judged bare distance between the candidate's stated
   place and the listing's, with no awareness of `location_scope` — so a "national"-scope
   candidate (deliberately searching country-wide, not narrowed to their city) still got an
   in-country on-site/hybrid role downgraded/disqualified as "geographically impractical"
   merely for being a different, distant city. A live case: a Newcastle hybrid role rejected
   for a Southend candidate searching nationally. Both `rank_gate` (`_location_scope_note`)
   and the final judge (`snapshot.build_snapshot`'s new "Location search scope" CV line) now
   state what the candidate's scope actually means and instruct GEOGRAPHY to defer to it —
   "national"/"international" scope means in-country/any-country distance is never itself a
   GEOGRAPHY failure; only a different country (under "national") or an unmet visa/
   right-to-work requirement (either scope) still fails it. Whether the judge is actually
   rejecting anything was hard to see from the console before — a per-cluster line now
   reports `N strong, N backup, N rejected (N with a disqualifier reason)` after every
   fresh judge call (`engine.py`'s `final_evaluation cluster[...] took Xs...` line), and
   `GET /settings/run-funnel` exposes a `final_judge_rejected` count (rendered on the
   Settings page's run-funnel panel) alongside the existing strong+backup total — added
   after a live run's funnel numbers alone couldn't confirm whether the judge stage was
   rejecting anyone or everyone was quietly passing through; jobs already judged under
   the current profile are
   served from their stored verdict and never re-sent, and a job with a stored `reject`
   verdict under the current signature is never resurfaced — including by either
   fallback below, which used to pull from the full unfiltered candidate list and could
   re-show exactly these rejects as an "inconclusive, showing anyway" placeholder. If a
   cluster's judge call *succeeds* but comes back with fewer than `MIN_RESULTS` picks, a
   **single bounded retry** pulls the next-highest-`rank_gate`-scored candidates that
   lost the `JUDGE_POOL` cut (still held in `rank_by_cluster`, judged on whatever text
   is already available — no second scrape pass) and judges them too, once, merging any
   results in. Separately, a deterministic top-N fallback fires **only when the
   expensive call itself failed** (exception/malformed response) — never when it
   succeeded and genuinely rejected everyone (or rejected everyone in the retry too),
   which correctly contributes zero picks for that cluster rather than padding a
   wrong-function role into the results → fair-allocate the combined per-cluster picks
   to a final cap (`FINAL_PICKS` = 12) → persisted as `Role` rows. That last
   fair-allocate runs **once per `_VERDICT_GRADES` tier** (very_strong → strong → ok
   → stretch → ungraded), not once over a strong/backup split: `fit_rank` is assigned
   purely by position in the final list and nothing downstream re-sorts, so tiering on
   the `strong_fit` boolean alone left the judge's finer `fit_level` — the grade the
   card's badge actually shows — doing nothing, and a live run ranked four "Strong fit"
   picks above two "Very strong fit" ones purely on cluster iteration order. Each
   grade's own pass still spreads that grade's slots across clusters fairly.
   Historical note: the gate stage has gone through three designs. Originally two
   separate gates (sector per-cluster, seniority once globally) with a
   strict-then-relaxed two-call final eval — the global seniority gate over-pruned and
   starved clusters, which is why fallbacks fired as the *main* path. That was replaced
   by a merged per-cluster annotate-only screen (sector hard-dropped; seniority/
   requirements purely informational — no hard drop at all). The current
   compounding-failure demotion (described above) is narrower than either predecessor —
   per-cluster, floor-protected, and normally requires two independent clear-mismatch
   signals agreeing (tightening to one when a round's pass rate makes clear the gate
   isn't discriminating, see `dynamic_hard_drop_threshold` above) — specifically to get
   some of the original design's cost savings back without reproducing its starvation
   failure.

   **Listing age.** `full_auto._listing_age_tag` renders `JobSeen.posted_at`/
   `expires_at` (see the data model) as a `[listing age: ...]` note in both the
   `rank_gate` listing block and `_final_eval_job_block`. Past `STALE_LISTING_DAYS`
   (45) it reads "STALE, treat as a negative"; a closing date inside 7 days, or already
   passed, is stated too. **It is a downgrade, never a filter**: a live months-old
   posting is still applicable-to, it is just a materially worse use of an application
   than an equally-good fresh one, because most of its shortlist is already decided.
   `rank_gate` takes it as SCORING component 3 (≈-10, applied after function/depth fit,
   explicitly unable to outweigh a good function match); the final judge is told to name
   it in `concerns`, let it push a *borderline* grade down one step, and break ties
   toward the fresher role. A passed closing date is the one hard case — it feeds
   `rank_gate`'s CLOSED LISTING downgrade (rule c). Two things must stay true: a listing
   with **no** age tag has an unknown date and is never penalised for it (roughly a
   third of the store, and silence is not evidence of age); and the tag is the one
   bracketed hint in a job block that is **not** an earlier pass's opinion but a fact
   from the board's API that the posting text usually doesn't state — so the judge's
   standing "your reading of the fuller text always wins over a hint" rule is explicitly
   carved out for it, since there is nothing in the text to check it against.

   **Text supply: what the cheap stages can actually read.** `GATE_LISTING_TEXT_CHARS`
   (2000) and `RANK_LISTING_TEXT_CHARS` (**5000**, raised from 3000 — see below) are
   *ceilings*, and for most candidates
   there is nothing like that much to truncate. A measured live store (838 rows) broke
   down as **402 Adzuna rows averaging 497 chars and 361 Reed rows averaging 455** —
   both APIs truncate their description to a ~500-char teaser — against only ~67 ATS
   rows carrying a real 3000-char description. That teaser is the *opening blurb*, never
   the requirements section, and `full_text` (the only other source of real text) is
   written by Phase 5, which runs **after** gate and rank. So ~91% of candidates were
   screened and ranked on ~455 chars: an audit of jobs the expensive judge disqualified
   on an experience bar ("3+ years as a Data Analyst") found the requirement in the
   snippet for **1 of 24**, and in the scraped `full_text` for 7. The cheap stages
   weren't miscalibrated — they were starved, and the expensive judge was doing triage
   work that should have happened three stages earlier. (Those two constants' own
   comments describe an audit of a real Reed posting whose requirements started at char
   ~1980 — that was a *scraped* `full_text`, not what a freshly-discovered job supplies.)

   **The ATS rows were not the exception they looked like.** An ATS posting is the one
   source that arrives text-complete at discovery, which is exactly why losing part of
   it there is unrecoverable: `_needs_full_scrape` skips ATS rows and
   `SNIPPET_SUFFICIENT_CHARS` waves through anything that long, so *nothing* downstream
   ever re-fetches one. Several vendors split a posting across **several** response
   fields and `fetch_ats` was storing only the first: Lever returns the opening blurb in
   `descriptionPlain` and puts "What You'll Do"/"What You'll Bring" in a separate
   `lists` array (plus `additionalPlain`); Recruitee splits `requirements` out of
   `description`; Workable documents `requirements`/`benefits` alongside it. In every
   case the dropped part is the **requirements section** — the half a fit judgement
   turns on — while the part kept is the company marketing blurb. Measured on the live
   Lever posting that surfaced this (computercare "Data Analyst"): 2,064 chars stored,
   **4,417 dropped**, including the "2-4+ years of experience working across data
   engineering and analytics domains" bar. The final judge graded it a *strong*
   entry-level fit, correctly, on the only text it was ever given — the intro genuinely
   reads entry-level ("an excellent opportunity for someone who wants to gain hands-on
   experience"). `_ats_text`/`_lever_text` now merge the sections in the vendor's own
   order, capped at `ATS_SNIPPET_CHARS` (8000, matching `FINAL_EVAL_JOB_TEXT_CHARS` —
   no point storing more than the judge can read). Greenhouse (`content`) and Ashby
   (`descriptionPlain`, measured 5.5k on a live board) really are whole-posting fields
   and were never affected. Note `_embed_text` only reads `snippet[:2000]`, so this
   changes no embedding and costs no re-embedding.

   Once a candidate *does* carry a page, the ceiling starts binding, and
   `RANK_LISTING_TEXT_CHARS` went **3000 → 5000** on that basis. A `tier_analysis
   --text-mode full` run located by character offset the exact clause the final judge
   quoted when rejecting a job the mid tier had scored into the judge pool: of the 5
   locatable ones, 4 sat at offsets 3456 / 3758 / 3817 / 4546 — past the old cap, all
   under 5000. The characters were already in `JobSeen.full_text`, just unread, so this
   is the one text fix that needs no extra fetching. Store shape behind the number:
   median scraped page 3227 chars, p75 4592, p90 6079, max 8000 (`FINAL_EVAL_JOB_TEXT_
   CHARS`' own cap), with 109 of 192 pages longer than 3000 — the old cap was truncating
   the majority of pages, right where a JD's requirements section tends to start.
   Nothing was found between 5000 and 8000, so raising it further has no evidence yet.
   **This was only safe after screen_v12**: before `listing_ok` learned to read past
   board chrome, giving the cheap tiers more text made them strictly worse. Re-check
   that ordering (`tests/gate_harness.py --ground-truth`, snippet vs full) before
   raising any of these budgets again.

   `fetch_reed_details` closes this for Reed: its per-JOB endpoint returns the whole
   description (measured avg ~3900 chars, ~8.6x the teaser) for one plain HTTP call, no
   LLM and no browser — a 24-id batch resolved in 1.7s at the same 12-wide pool width
   `gather_jobs` uses. `_enrich_reed_full_text` runs it over exactly the slice each
   cluster is about to examine (`queue[:cluster_examine_cap]`, already embed-score
   ordered), on the main thread before the cluster pool starts, since it commits.
   Deliberately NOT the whole store: most rows never reach a gate. It fails soft per id
   (404/410/timeout → that id is simply absent and the candidate keeps its teaser) and,
   like `_persist_scrape`, only stores text that actually beats the snippet. Two free
   downstream effects: the text persists on `JobSeen.full_text` for every future run,
   and `_needs_full_scrape` skips anything carrying it, so **Phase 5 shrinks** by
   however many were enriched. Independent of the Settings full-scrape toggle, which
   governs browser page-reading before the *final judge*, not this.

   **Adzuna** has no per-job *API* route (its `description` is truncated with no
   detail endpoint), and for a long time that read as unfixable — so its rows stayed
   teaser-only until Phase 5, which then couldn't scrape them either: the API hands
   out a `/jobs/land/ad/{id}` tracking URL that resolves to a JS interstitial,
   correctly detected by `_looks_like_redirect_stub` and abandoned. Net effect, an
   Adzuna row had **no path to real text at any stage** — 225 of 261 in a measured
   store carried none, and the *final judge* was grading them on 500 chars of company
   blurb (23 of 38 `strong` verdicts in that store were issued with no `full_text` at
   all). `fetch_adzuna_details` closes it: Adzuna's own **website** detail page for
   the same ad id (`/details/{id}`, on the host the listing arrived on — so no
   country-TLD map is needed) returns 200 to a plain GET and carries the whole
   description in a JSON-LD `JobPosting` block. Measured 4,792 chars for the Avara
   Foods listing that prompted this, against its 500-char teaser, including the
   "Proven experience working as a Data Analyst" clause the judge needed and never
   saw. Read from the JSON-LD rather than the rendered markup: it's a stable
   schema.org contract, and it arrives already scoped to this posting so the board's
   "similar jobs" list can't leak in.
   Throttled much harder than the Reed twin — `ADZUNA_DETAIL_MAX_WORKERS` (3, vs
   Reed's 12) and `ADZUNA_ENRICH_PRE_GATE_CAP` (40, vs 100) — because each response
   is a ~100KB HTML page and the host starts returning 429 after a handful of rapid
   requests; on repeated 429s the batch is abandoned outright rather than retried,
   since being rate-limited out of *discovery* (same host) would cost far more than
   the text is worth. Both enrichers share `engine._enrich_pre_gate`, which owns the
   subtle half: only text that BEATS the snippet is stored, `_has_full_text` must be
   set or the gate cache-key richness marker goes stale, and the DB write is one
   indexed SELECT + commit.
   *Following* the tracking redirect doesn't work **for enrichment** — the land URL
   403s a plain HTTP client. The claim that it is "bot-walled behind the browser too"
   was **wrong and has been corrected**: headless resolves it in 2-4s, which is what
   the final-pick liveness check now uses (see the Adzuna liveness note below). It
   remains useless *here* because pre-gate browser-scraping is rejected on cost —
   ~4-5s/page would add minutes per run.

   **Both enrichers run a SECOND time, on the judge pool.** The pre-gate caps above
   are sized for latency (they sit in front of time-to-first-card) and are spent in
   embed-score order, so a candidate that climbs into the judge pool from outside
   that head slice reaches the expensive model still holding its teaser. For Reed
   that only wastes a phase-5 fetch; for Adzuna it is terminal, because
   `_needs_full_scrape` skips the `/jobs/land/ad/` interstitial and there is no other
   route to text. A live run rejected an Adzuna "BI Analyst" with *"the available
   description does not provide enough role requirements or seniority detail to
   establish a genuine fit"* while that posting's own detail page carried a full
   responsibilities-and-Power-BI-requirements section. None of the pre-gate caps'
   reasoning applies at this point in the run: the provisional cards are already on
   screen so nothing is waiting on it, it is plain HTTP with no LLM and no browser,
   and it is bounded by `JUDGE_POOL`(40) rather than by an examine budget — and it
   *shrinks* phase 5, since anything enriched here then skips the scrape. Count lands
   in `funnel_counts.judge_pool_enriched`.

   Relatedly, **`_needs_full_scrape` is not a proxy for "has enough text to judge"**
   — `engine._has_judgeable_text` is, and `_selection_score` reads that one. The two
   come apart on exactly one case and it was inverted: an un-enriched Adzuna row's
   URL is an interstitial, so a fetch cannot help and `_needs_full_scrape` correctly
   returns False — but the text it is stuck with is a 500-char blurb. Reading that
   False as "text is fine" handed those rows `RICH_TEXT_SELECTION_BONUS`, i.e. the
   most text-starved candidates in the store were being *preferentially promoted*
   into the judge pool over candidates the judge could actually read. Keep the two
   questions ("would a fetch help" vs "is there enough to judge") separate.

   Because of all this, `full_auto._gate_job_id` carries a **text-richness marker**.
   `_gate_cache_key` keys on (gate, profile signature, job id) and
   *not* on the text that was judged, so without the marker a verdict reached on the
   455-char teaser would be served forever for a job whose full description has since
   arrived — silently cancelling the enrichment for exactly the jobs that most needed
   re-judging. The marker used to be **two-state** (`:full` or nothing), which keyed on
   *where* the text came from and so could not see a text that grew **in place** — an
   ATS row never has `full_text` at all, so when `fetch_ats` started merging in the
   requirements sections (above), every one of those rows kept serving the verdict it
   reached on its blurb alone, permanently. It is now `:{full|t}{len//1000}`, bucketing
   the length in 1k steps: text that grows materially re-screens, text merely re-fetched
   identically does not, and no global `screen_v` bump (which would re-screen the whole
   store to get identical answers for the untouched majority) is needed. Keep it in sync
   with whatever `_has_full_text` means — and prefer extending this marker over a
   version bump whenever a change affects only *some* rows' text.

   **Concurrency (what runs at the same time as what).** Three levels, all added
   because a live 3-cluster run spent `gate=83s`, `scrape=48s`, `final_eval=53s` doing
   these strictly one after another:
   - *Within one gate call*, `screen_gate` fans its `_GATE_BATCH`(=20)-sized LLM calls
     out over a `ThreadPoolExecutor(max_workers=min(3, len(batches)))` — the identical
     pattern `rank_gate` already used, bounded at 3 for the same reason (a burst risks a
     short-window rate cap).
   - *Across clusters at the gate*, `_run_engine_pipeline` submits one
     `_gate_rank_refill_cluster` per cluster to a thread pool. Safe because both budgets
     (`cluster_judge_target`, `cluster_examine_cap`) are derived from the active-cluster
     count *before* any cluster runs, so no cross-cluster fairness decision is left to
     disturb; results are merged sequentially afterwards, in queue order, so counters and
     log lines stay deterministic.
   - *Across clusters at the tail*, each cluster's Phase 5 scrape is `await`ed and then
     handed straight to its own Phase 6 judge (`_scrape_then_judge`, one `asyncio.gather`
     over clusters), so one cluster's expensive judge call overlaps the others' page
     fetching instead of every cluster waiting for a single global scrape phase. The two
     laps merged into one `scrape+judge` timing, since there's no longer a wall-clock
     boundary between them.

   Three things make this safe and must stay true if you touch it: (a) worker threads
   must never touch the request `Session` — `_gate_rank_refill_cluster` takes a
   `cancel_check` callable (`_make_cancel_check`, which opens its own short-lived
   session, throttles to one SELECT every 3s and latches once cancelled) instead of the
   `(db, run)` pair `_check_cancelled` needs, and every DB write (`_persist_scrape`,
   `_persist_dead_scrapes`, `_persist_verdicts`, scam-verify) happens back on the main
   thread; (b) the concurrent scrapes share ONE semaphore and ONE alt-source budget via
   `scrape_full_details`'s optional `sem`/`alt_budget` params, or N clusters would mean N
   independent `MAX_CONCURRENT` lanes hammering the same sites; (c) `full_auto.get_db()`
   sets `timeout=30` so the now-concurrent `gate_cache` writers can't collide into
   "database is locked" and silently lose a batch's cache entries.
3. Cluster/stream identity is used internally (gate routing, per-cluster LLM calls) but
   is **not** currently exposed as UI grouping — by design, not an oversight; it only
   ever surfaces as an optional "Matched via: X track" clause in `ai_analysis` when more
   than one cluster exists.

**Prompt caching — the pipeline's largest fixed cost, and why it's now measured.**
Every prompt here is a long FIXED prefix followed by a short variable payload:
`_screen_prompt` (~4.5k tokens of rules + profile, then the listings), `_rank_prompt`
(~2.5k, then the listings), and `_FINAL_EVAL_SYSTEM` (~12k, with the CV + jobs in a
separate user message). That is exactly the shape OpenAI's automatic prompt caching
discounts, and the prefixes really are byte-identical across calls — `build_snapshot`
runs once per run so `engine_profile` cannot drift between batches, and
`_FINAL_EVAL_SYSTEM` interpolates nothing at all, so it is identical across every run
and every user. Nothing read `usage` back, though, so whether the discount was landing
was **unknowable** — on measured volumes that is ~110K tokens a run of luna+terra riding
on an unverified assumption. Three things now:
- `full_auto.llm()` records `prompt_tokens` / `cached_tokens` / `completion_tokens` per
  `stage` (`_record_llm_usage`, mutex-guarded because gate/rank/judge calls all run on
  pool threads). `run_search_task` resets it per run — deliberately *after*
  `ensure_profile_intel`, so a run that happened to regenerate intel doesn't skew the
  per-stage hit rates — and flattens the rollup into `funnel_counts` as
  `tokens_{stage}_{metric}`, surfaced by `GET /settings/run-funnel` and the Settings
  funnel panel. **Un-flatten by stripping affixes, never by splitting on `_`**: both the
  stage names (`rank_fallback`) and the metric names (`prompt_tokens`) contain
  underscores, so any `rsplit`-based parse silently mis-attributes one to the other.
- `llm()` passes `prompt_cache_key`. This is a **routing** hint, not a correctness key —
  the API still requires an exact prefix match, so a coarse or stale key costs at most a
  miss and can never serve the wrong cache. It matters here because this pipeline is
  aggressively concurrent (4-wide gate batches, all clusters at once, concurrent judge
  chunks) and concurrent requests otherwise land on different machines, each missing a
  prefix the others just wrote. Screen/rank key on `_profile_signature` (+ the intent
  hash for rank), mirroring their `gate_cache` key's scoping because those are precisely
  the profile facets interpolated ABOVE the listing block.
- The judge additionally passes `cache_retention="24h"` against a **constant**
  `_FINAL_EVAL_CACHE_KEY`. Worth it only because that prefix is identical across runs;
  the default few minutes of inactivity would expire it between searches, and paying
  12k tokens once per cluster per run is this stage's dominant cost. Note the arithmetic
  that follows: a judge call costs ~13.3k tokens before it reads a single job, and each
  job then costs ~770 — **jobs are ~17× cheaper than calls**, so merging two thin
  clusters into one call saves far more than trimming `JUDGE_POOL`, and the thin-cluster
  backfill retry (a whole second `final_evaluation_split`) is the single most expensive
  optional thing the run does.

Key cost/reliability guards layered into this pipeline (tune via env vars, see
`config.py` / top of `full_auto.py`):
- `MAX_SEARCHES_PER_DAY` (default 6) — daily search cap **per USER** (counted by
  `routers/search.py::_searches_today`, which joins `Profile` and filters on
  `user_id`), not a single global pool — one beta user can't exhaust everyone's
  quota. A user's profiles share their one allowance. Note a run killed mid-flight
  (crash, restart, host OOM) still counts: the row exists and it really did spend
  API credits.
- `MAX_CONCURRENT_SEARCHES` (default 2) — orthogonal *capacity* guard: how many runs
  may be in flight process-wide, counted off `SearchRun.status == "running"` so two
  simultaneous kickoffs can't both slip past it. Each concurrent run drives its own
  headless Chromium (`full_auto.MAX_CONCURRENT` pages apiece) plus several thread
  pools, so it's effectively a memory ceiling — on a 2GB VM a few simultaneous runs
  OOM the machine, killing *every* user's search rather than delaying one. Over the
  limit returns 503, not 429 (a retryable "busy", not a spent quota). Raise it only
  alongside the VM's memory.
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
- `MIN_RESULTS` (default 3) — the per-cluster keep-floor the merged screen backfills to,
  and the same floor that triggers the final-judge stage's bounded backfill retry.
- `RELEVANCE_PRIMARY` / `RELEVANCE_FLOOR` (`engine.py`, both 0.39) — the embedding
  cosine-score cutoffs. `RELEVANCE_FLOOR`, not `RELEVANCE_PRIMARY`, is the actual
  pool-admission gate: `_cluster_candidate_queues` always admits everything scoring
  `>= RELEVANCE_FLOOR` regardless of `RELEVANCE_PRIMARY` (which only flags a cluster as
  `harsh`/labels a `broadened` fallback for logging). Raised twice from an original 0.35:
  first to 0.37 after a diagnostic re-score (`analyze_embedding_gate.py`) found the
  lowest genuinely-decent match sitting at 0.379; then `RELEVANCE_FLOOR` raised from 0.20
  to 0.39 (with `RELEVANCE_PRIMARY` raised in lockstep, since floor must never exceed
  primary) after a live `tests/gate_harness.py` run confirmed jobs scoring below ~0.39
  were reliably screened out later anyway — admitting them at all just burned gate/rank
  calls on jobs with no realistic path to a final pick.
- `RANK_EXAMINE_BUDGET` (240 → 320 → **480**) / `RANK_TARGET_POOL` (80) / `JUDGE_POOL` (40) /
  `RANK_REJECT_SCORE_FLOOR` (50 → **32**), all `engine.py` — the four numbers that set what the
  cheap+mid stages cost and what the judge gets to choose from, see pipeline step 2.
  `RANK_EXAMINE_BUDGET` is the honest cost dial: it is roughly a 3x increase on what the
  old per-cluster caps summed to, and screen (CHEAP) + rank (MID) calls scale directly
  with it. Raised 240 → 320 after a live single-cluster run stopped on "absolute pool
  cap" at 240 examined / 69 gated / 9 judged against a 760-deep queue — the ceiling
  itself, not `MIN_RESULTS`/`JUDGE_POOL`/a thin queue, was the limiting factor. Raised
  again 320 → 480: with the free pool-quality prescreens removing ~65% of the queue
  before a token is spent, too little was still reaching the judge on a real profile.
  Because the gate screens the whole budget in ONE wave (see `GATE_ROUND_SIZE` below), a
  wider budget costs more parallel batches rather than more serial waves — so if
  time-to-first-card regresses, `REED_/ADZUNA_ENRICH_PRE_GATE_CAP` and `*_PAGES_PER_TERM`
  are the knobs, not this one.
  **`RANK_REJECT_SCORE_FLOOR` was doing two unrelated jobs, and could not be lowered
  until they were separated.** It reads as a quality bar, but `rank_gate`'s work-arrangement
  and salary HARD DOWNGRADES capped a violating listing's score at 15 *precisely so this
  floor would eliminate it* — i.e. the floor was also the enforcement path for two
  preferences the candidate had explicitly marked **Soft**, and lowering it would have
  silently switched that enforcement off. `_rank_prompt` now renders those two rules into
  their own **SOFT-PREFERENCE MISMATCHES** section whenever the candidate left them Soft
  (they stay in HARD DOWNGRADES when marked Hard — the prompt is assembled from
  `profile["hard_axes"]`). That section sets a `soft_violation` boolean and explicitly
  does NOT touch the score; `engine._selection_score` then subtracts
  `SOFT_VIOLATION_SELECTION_PENALTY` (12.0) — ordering only, never `_rank_score`, under the
  same rule as `RICH_TEXT_SELECTION_BONUS` and `_unverified_penalty`, so the card's "Fit
  estimate" chip and the floor test both keep showing the model's own number. That is what
  "Soft" was supposed to mean all along; a capped-to-15 score was indistinguishable from a
  genuinely terrible fit at every downstream stage. With that split, 32 is free to be what
  the name says ("the mid tier is not telling us this is clearly a no"), clearing the
  score-15 band the real hard downgrades still occupy with headroom. The flag rides in
  `gate_cache`'s text column as a third packed field (`{score}|{note}|{0|1}`); `rank_v15` →
  `rank_v16`, since a v15 score of 15 for a soft-mismatching listing means "preference
  mismatch", not "bad fit", and is not comparable.
- `STALE_SELECTION_PENALTY` (8.0) / `STALE_SELECTION_PENALTY_DOUBLE` (12.0), stamped by
  `engine._annotate_stale` — **a SOFT "Maximum listing age" was the one stated preference
  with no ordering path at all.** Hard is a real drop (`listing_over_max_age`, before any
  LLM call); Soft got the age tag's wording, `rank_gate`'s STALENESS scoring component
  (~-10, explicitly unable to outweigh a good function match) and the judge's "push a
  borderline grade down one step" — and nothing that touched ordering. Measured on a live
  profile with a **7-day Soft** limit: **18 of 36 shown roles were over it, 9 past double
  it**, at ranks 1–12 (a 22-day listing was rank 1, a 28-day one rank 5; over-limit mean
  rank 7.44 vs 5.56 within limit). Deliberately DETERMINISTIC rather than routed through
  `rank_gate`'s `soft_violation` the way arrangement/salary are: the age is a fact this
  system already holds, so re-deriving it with a model would be less reliable AND cost a
  `rank_v` bump for a known answer. Two steps, because "over the limit" and "several times
  over it" are not the same claim — the doubled step is the "hard cut at double the limit"
  instinct expressed as a demotion, since Soft means the candidate asked *not* to be
  excluded on this. Unknown and merely-approximate dates are never penalised, the same
  rule every other age consumer follows, and the whole thing is a no-op when the
  preference is Hard. Counted as `stale_soft_demoted`/`_double` and surfaced on the
  Settings run-funnel panel, which names the Hard setting as the way to exclude instead.
  **The `_selection_score` half alone could not have reached the page**: it decides the
  judge POOL, while `fit_rank` is positional in the final assembly, so `_stale_penalty`
  is also a stable tie-break WITHIN each `_VERDICT_GRADES` bucket there. That tie-break
  is exactly what the judge's own prompt already asks for ("prefer the fresher role when
  choosing between two comparable picks") — ordering is decided in code, so until now
  that instruction had nothing to act on.
  **Neither of those two is enough on its own, because a demotion in the judge POOL is
  not a demotion in the RESULTS.** A role over the limit but well above the floor still
  reaches the judge, and the judge had no rule telling it to care: `_listing_age_tag`
  emitted only two severities the judge's system prompt describes — `STALE` (open for
  months, downgrade-only) and `ELIMINATE` (past a HARD maximum → DISQUALIFIER 8) — and a
  SOFT maximum matched neither, so it was read as ordinary staleness, i.e. "one step down
  IF the grade is borderline". There is now a **third severity** between them, and it is
  what makes the grade itself move: `OVER THE CANDIDATE'S STATED MAXIMUM (a preference,
  not a hard limit)`, and at twice the limit `MORE THAN DOUBLE THE CANDIDATE'S STATED
  MAXIMUM`. It plugs into the EXISTING mechanical `fit_level` rubric rather than adding a
  parallel adjustment — over the limit counts as ONE concern touching a core requirement,
  past double counts as TWO (capping the role at `ok`) — so a demoted grade moves the
  role down the page for free, since final assembly is grade-tiered. It never excludes,
  and is explicitly never a reason to move a role out of `backup` into `not_selected`.
  Keep those two keyword strings in sync between `_listing_age_tag` and the WHAT THE
  BRACKETED HINTS ARE paragraph; the prompt keys on them literally. Folded into
  FINAL_EVAL_PROMPT_VERSION 28 rather than taking a 29, because 28 had not yet run in
  production and so cost no extra store-wide re-judge.
  `full_auto.rank_gate`'s fail-open path (its `llm()` call erroring,
  e.g. an intermittent permission/rate error on `MID_MODEL`) retries once on the same
  model after a short backoff, then falls back to `CHEAP_MODEL`, before giving up; a
  job that still has no real score after all of that is tagged `_rank_gate_failed` and
  bypasses `RANK_REJECT_SCORE_FLOOR` entirely in `engine._gate_rank_refill_cluster`
  rather than being compared against it — the fallback neutral score (50) no longer
  coincides with the floor at all, but the bypass stays: it must not depend on those
  two numbers happening to differ either.
- `JUDGE_MERGE_THIN_CLUSTER_MAX` (6) / `engine._judge_groups` — which clusters SHARE a
  Phase 6 judge call. A judge call pays ~12k tokens of `_FINAL_EVAL_SYSTEM` plus the
  cluster CV before it reads a single job, and each job then costs ~770 — jobs are ~17x
  cheaper than calls, so two 4-job clusters cost far more as two calls than as one 8-job
  call. Deliberately a LOW threshold rather than "merge whenever it's cheaper": the
  per-cluster CV (`cv_text_for_cluster`) is the mechanism that stops a candidate targeting
  two unrelated fields being judged against a blend, and a profile-wide judge call diluting
  a minority cluster is **a bug this pipeline has already had once**. Merging only genuinely
  thin clusters keeps that protection where it works while removing the case it protects
  worst — a 3-job cluster whose judge can only pick the least-bad of three either way. A
  group never exceeds `FINAL_EVAL_MAX_JOBS_PER_CALL`, or `final_evaluation_split` would
  re-split it into concurrent chunks and hand back exactly the per-call overhead the merge
  was for. Groups are decided from the selected pool *before* scraping so each group can
  still scrape-then-judge as one pipelined task (`_scrape_then_judge` now takes a group and
  `asyncio.gather`s its clusters' scrapes). `_run_cluster_final_eval` takes `idxs: list[int]`
  and returns `"idxs"`; the caller loops per GROUP for the per-call work (verdict persistence,
  run-wide counters, the shared scam-verify budget) and splits `picks`/diagnostics back per
  cluster by each entry's own `_cluster`. `_cluster_label` is stamped per JOB, not per group,
  or a merged group would tell the user a role was "matched via A + B track" when the
  embedding stage only ever assigned it to A. Count lands in
  `funnel_counts.judge_calls_saved_by_merge`.
- `GATE_ROUND_SIZE` (80) / `full_auto._GATE_MAX_WORKERS` (4) — the latency side of that
  budget. **The gate no longer examines its budget in incremental rounds.** It used to:
  a small `GATE_FIRST_ROUND` then `GATE_ROUND_SIZE`-sized ones, each a blocking
  `screen_gate` call followed by a blocking `rank_gate` call, so that
  `_gate_rank_refill_cluster` could stop early once `judge_target`
  (`RANK_TARGET_POOL`/clusters = 40) judge-eligible candidates had accumulated.
  **That early exit never once fired.** Across every run that recorded a per-cluster
  `stop_reason` (17 cluster-runs) the tally is `absolute_pool_cap` 16, `pool_exhausted`
  1 (a queue of only 114), `target_reached` **0** — `judge_eligible` lands at 5–12 per
  cluster against a target of 40. So the rounds bought no LLM calls at all and cost 2
  serial latencies each: 3 rounds × 2 = 6 waves for a 160-candidate cluster, measured
  at ~10.3s per wave (`gate` = 61.93s).
  The whole budget is now screened in ONE `screen_gate` call and ranked in ONE
  `rank_gate` call. That is the *same* number of `_GATE_BATCH`(20)-sized sub-calls over
  the same `_GATE_MAX_WORKERS` pool — 160 candidates = 8 screen batches = 2 waves, plus
  one rank wave — so it costs identical tokens and lands ~3 waves instead of 6.
  Three things this rests on, all of which must stay true:
  * `judge_target` survives as a **post-hoc trim** (`stats["judge_target_trimmed"]`),
    not a loop break, so the bound still exists; it now trims the WORST by
    `_selection_score` rather than keeping whatever arrived first.
  * It must **not** overwrite `stop_reason`. That field is the only evidence that the
    early exit never fired, i.e. the only way to re-check this decision later.
  * `GATE_ROUND_SIZE` still exists, but now only as the SLICE over which
    `dynamic_hard_drop_threshold` is computed. That threshold asks "is this stretch of
    the queue thin on real mismatches?", and the queue is embed-score ordered, so
    pooling one fraction over all 160 would average a strong head into a weak tail and
    change gate strictness as an accidental side effect of a latency change.
  The visible cost is that rank-stage "Verifying…" cards now appear at the END of the
  gate phase rather than after a 20-candidate first round. The embed-stage paint
  (`EMBED_PAINT_MAX`, ~7s in) is unaffected and is what actually fills the early screen.
  `REED_ENRICH_PRE_GATE_CAP` (100, run-wide) still caps pre-gate Reed enrichment rather
  than following the examine budget out to 320 — it is blocking main-thread HTTP in
  front of first paint (a live run enriched 45 in 4.1s), so it covers the head of the
  budget and the deep tail rides its teaser. `_GATE_MAX_WORKERS` is the knob to turn
  back down if 429s/401s appear, and the knob to raise (4 → 8) if the remaining 2 screen
  waves are worth collapsing to 1.
- `CATEGORY_EXPAND_ENABLED` (default false) — see the category-page-expansion note
  above; off by default since it currently recovers ~0 jobs on JS-hydrated category
  pages while still paying full crawl cost.
- Google-organic discovery (`full_auto.fetch_google_jobs`/`_looks_like_category_page`)
  drops board-owned category/search-listing pages (e.g. a charityjob.co.uk "N jobs in
  X" results page) that aren't an individual posting — these used to enter the store
  and consume pool/scrape/eval slots as if they were real listings. `expand_category_pages`
  tries to recover individual posting links from those dropped pages instead of just
  discarding them; it merges crawl4ai's `result.links.internal` **and** `.external` lists
  before filtering, rather than trusting crawl4ai's internal/external split alone — that
  split is based on the page's *final resolved* URL, so a bare-domain-to-`www` (or
  http-to-https) redirect on the category page can put every real posting link in
  `external` and leave `internal` empty, which used to look identical in the logs to the
  page genuinely having no links yet (e.g. not-yet-hydrated JS). `_posting_link_reject_reason`'s
  own host check already tolerates a subdomain relationship, so merging the lists in is
  safe — a truly foreign host still gets filtered there. The `0 kept` diagnostic log now
  reports the internal/external split so a future zero-link page can still be told apart
  from the JS-hydration case. Even after that fix, live runs still showed `0 kept; raw
  links=0` on category pages with genuinely zero links found by crawl4ai (JS-hydrated
  pages it isn't waiting for) — rather than keep paying the ~4-5s/page crawl cost for a
  stage recovering nothing, `CATEGORY_EXPAND_ENABLED` (default false, see below) now
  gates the whole expansion call off; category hits just stay dropped, same as before
  `expand_category_pages` existed. Flip it back on once the JS-hydration issue is
  addressed.
- Phase 5 scraping: every source shares one concurrency lane (`MAX_CONCURRENT`, no more
  Adzuna-specific single-lane serialization — live testing showed it wasn't preventing
  any actual blocking, just adding minutes of pure sleep), a redirect-tracking stub
  (e.g. Adzuna's `/jobs/land/ad/...` click-through pages, which resolve 200 OK but are
  just a "you're being redirected" page) is detected and treated as a failed scrape
  rather than persisted as real content, and the whole phase has a 60s total wall-clock
  budget — whatever hasn't finished by then falls back to its snippet.
- `screen_gate` **re-asks for any listing a response omitted**. The cheap model regularly
  returns valid JSON that simply skips some of the listings it was given (a live 113-listing
  run silently skipped 17). Those used to fall through to the "everything ok" fail-open
  default *and* get written to `gate_cache` as a real verdict — so a listing no model had ever
  looked at was recorded as passing every axis, permanently, and was never re-screened. It now
  re-asks for just the skipped ones (one bounded retry, a much shorter prompt); anything still
  unjudged stays fail-open for the run but is **not cached**. `_screen_one_batch` also sets
  `c["_gate_unjudged"]`, because an unjudged listing is otherwise byte-identical to one the
  model actively cleared — the fail-open default sets every axis true, and `"missing_decision"`
  is normalised out of the packed `_gate_reason` by the `seniority_ok` branch.
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

### Pool quality — what is allowed to spend an examine slot

`engine._heuristic_prescreen` + `engine._pool_quality_prescreen`, both run at pool
admission, both free (no LLM, no network, no DB).

**The measurement that motivated them.** An audit of one run's 320-candidate examine
budget graded every examined candidate by hand. The ranking was fine: of 8 roles shown
the user applied to 2, and a random 26 of the gate-DROPPED candidates were **26/26
correctly dropped — zero false negatives**. The problem was upstream. Of 282 examined
rows, **28% could not have become a pick under any ranking**: 12% located outside the
candidate's country (spacex Redmond WA, dadavidson Irvine CA, gallup Omaha, pmx
Birmingham *Alabama*), 17% carrying a plainly senior title, plus board category pages
with no posting underneath — two of which survived all the way to the **expensive
judge**, which spent a full slot each to report that the page contained search results
rather than a job description. A single ATS board (`workable:tiger-analytics`) supplied
34 of the 282.

So the low examined→strong rate was never a ranking failure. It is what the examine
budget is being spent ON.

Each check is a FACT about the listing, checkable for free, and wrong to pay an LLM to
discover. That is the same bar `_heuristic_prescreen` already set. They are deliberately
NOT left to `screen_gate`'s `listing_ok` / work-arrangement axes — those are the backstop
for *ambiguous* cases, not the place to catch a Texas listing for a UK candidate.

- **Seniority** (`_SENIOR_TITLE_RE`, extended). The pattern carried only the most formal
  words, so "Lead Data Scientist", "Staff Applied Scientist", "ML Ops Architect", "Sr.
  Business Analyst" and "Engineering Manager" all reached the gate for a Junior profile.
  `staff`/`architect`/`manager`/`sr.`/`lead` were added, each needing its own guard:
  `lead` is matched only when not followed by "generation" (the pre-existing "Lead
  Generation Specialist" collision), and — the load-bearing one — a title matching
  `_JUNIOR_MARKER_RE` (junior/graduate/trainee/assistant/associate/intern/…) is **exempt
  from the senior reject entirely**. That exemption is what makes broad tokens like
  `manager` safe, and it applies only in the junior-profile direction (the senior
  direction rejects ON that marker, so exempting it there would disable the check).
  **The senior direction is conditional on the candidate's "Allow overqualified"
  preference** (`allow_overqualified`, single-value boolean, default **off**, Dashboard →
  Preferences → "Roles below your level", just above Visa sponsorship). With it on, a
  junior-marked title stops being an unconditional drop and goes through to the soft gate,
  and `_screen_prompt`/`_rank_prompt`/the judge are told a lower-pitched role is not itself
  a mismatch. Strictly one-directional: it never loosens the junior-profile direction,
  since a Graduate candidate is not helped by being shown Director roles.
  **The trap, and the thing not to undo:** it must NOT re-admit apprenticeships or
  placement years (`_INELIGIBLE_REGARDLESS_OF_LEVEL_RE`, which still hard-drops them).
  Those were never excluded for being *junior* — an apprenticeship is a place on a course
  with an eligibility bar against applicants who already hold the qualification, and a
  placement year requires the applicant to still be mid-degree. Both get **worse**, not
  better, the more qualified the applicant is, which is exactly the failure fixed at all
  three LLM tiers together in screen_v13 / rank_v10 / eval 19. "I'll consider a more junior
  role" is not consent to a course you cannot enrol on. The carve-out is therefore restated
  at every tier alongside the flag, for the same reason those apprenticeship rules are:
  dropping it at one tier reinstates the hole at that tier.
  Cache scoping: the flag rides in `full_auto._profile_signature_v2`, which appends to the
  base signature **only when the flag is on** — so a default profile's hash is byte-identical
  and nothing is re-screened, while turning it on invalidates just that profile's screen/rank
  verdicts. `screen_gate`, `rank_gate` and the run's `gate_sig` (`JobSeen.gate_signature`, the
  gate-retirement key) all use `_v2`, so flipping it also re-opens rows a previous run retired
  under the other setting. The final judge's system prompt is a byte-identical constant across
  every profile (that is what makes its 24h prompt-cache retention pay), so the flag reaches it
  as an **"Open to more junior roles:" line in the per-run CV text** instead — the same
  mechanism as the "Maximum listing age" and "Location search scope" lines, and DISQUALIFIER 1
  keys off that exact wording.
- **Placement year** (`_PLACEMENT_YEAR_RE`). A sandwich-year/industrial-placement role
  exists for someone still part-way through a degree, reads as a near-perfect match on
  every other axis, and therefore survives all three LLM tiers — a live run put a
  "12month/placement year" internship at rank 5. The body text is consulted **only for a
  title that already announces an intern/placement/student role**
  (`_INTERNSHIP_TITLE_RE`): scanning every body outright was validated against the live
  store and wrongly flagged ".NET Developer, Graduate / Junior", "Junior Data Analyst"
  and "Junior Sales Analyst", whose recruiter boilerplate merely *mentions* placements.
  A bare "internship" is deliberately NOT matched — for a finished graduate that can be a
  real entry route.
- **Foreign location** (`_is_positively_foreign`). A narrow supplement to
  `_filter_by_country`, **not** a replacement, and the distinction matters: that filter
  keeps anything it cannot positively resolve *on purpose*, because the worldwide token
  set carries only ~20 UK cities and most genuinely-UK postings ("Gloucester, GB",
  "Potters Bar, GB", "SE19EQ") resolve to `None` and MUST be kept. The measured
  consequence is that US rows resolve to `None` on the same rule — `country_of` returns
  `None` for "Redmond, WA", "Bastrop, TX", "Irvine, CA" and "Remote/US" alike — so a live
  run's candidate-stage country filter dropped **0 of 6,642** rows while 12% of what it
  passed was American. This also repairs a false POSITIVE in the other direction:
  `country_of` resolves "Birmingham, Alabama" to `gb` on the city token, and the
  state-name test runs first. Scope is the **US only**, deliberately: it is 90%+ of the
  observed leakage (where the ATS vendor registry is headquartered) and the one country
  whose location strings are regular enough to match without guessing. State
  ABBREVIATIONS are matched only as `", XX"` at end-of-string or before a ZIP, because
  many collide with UK postcode areas (CA Carlisle, NE Newcastle, LA Lancaster, WA
  Warrington, TN Tonbridge) — a loose match here would recreate the exact failure the
  country filter was loosened to avoid. "Washington" and "Georgia" are excluded from the
  full-name list for the same reason (Washington, Tyne and Wear; the country Georgia).
- **Junk listings** (`_JUNK_TITLE_RE` + `_JUNK_MIN_TEXT_CHARS` = 220). A board's own
  search-results/category/alerts page. Requires the title pattern AND thin text: a real
  posting could legitimately be titled "Jobs at Acme" if it then has a description, and a
  short posting with an ordinary title is thin evidence rather than junk. A live run
  examined rows of 23, 114, 152, 159 and 163 characters, several of which **passed** the
  cheap gate's `listing_ok` axis precisely because there was too little text to look wrong.

**Validated against the live store before shipping**, which is the bar any change here
should meet — the risk of a free filter is a silent false positive, so measure that, not
the hit count. Over 7,867 stored rows the four checks remove 65.1% (2,784 seniority/
placement + 2,306 foreign + 34 junk), leaving queues still far above the 320 budget
(run 20's were 887 and 1,835). Against the 93 roles ever SHOWN to the user, exactly one
would now be dropped: the placement-year internship, which is the intended catch. Against
the 6 roles the user has ever APPLIED to, **zero**.

Counts land in `funnel_counts` broken out by reason
(`pool_quality_dropped_foreign_location`, `_junk_listing`,
`heuristic_prescreen_dropped`) and render on the Settings run-funnel panel as "Free
pre-filters". Keep them separate: a filter that removes candidates before any model sees
them is only safe to keep while its cost stays attributable to a specific rule.

**Post-hoc auditing of a run is harder than it should be.** `engine._sample_stage`
retains only **3** samples per stage and nothing persists which rows were examined, so
the audit above had to be reconstructed from `gate_cache` keys and store state.
`JobSeen.gate_signature` cannot stand in for it: `_mark` skips `state='shown'` rows, so a
role shown in run 12 still carries run 12's signature. Raising that cap is the cheapest
way to make this repeatable.

### Listing liveness verification (expired-listing suppression)

`engine._verify_listings_alive`, run immediately before the expensive judge (between
`_suppress_judge_duplicates` and the `_fair_allocate` into `JUDGE_POOL`).

**The problem.** The dead-listing machinery (`full_auto._dead_listing_signal`,
`_looks_like_expired_listing`, `_looks_like_generic_careers_hub`, `dead_reason`/`dead_at`)
was good and almost never RAN. It hangs off Phase 5 scraping — skipped for anything whose
snippet clears `SNIPPET_SUFFICIENT_CHARS` — and off the Reed/Adzuna detail endpoints, the
only sources with a revalidation path (`LISTING_REVALIDATE_AFTER_DAYS`). JSearch, Google
Jobs, Careerjet and ATS rows were never verified at any stage. Measured on a live store:
`last_verified_at` set on **17 of 9,042 rows (0.2%)**, `dead_reason` on **2**, and **100%
of surfaced rows had never been directly verified**. Re-fetching every surfaced role found
**26% already dead**. The listing that prompted this reached the judge, was graded
`strong` and shown at rank 6 with 0 chars of `full_text`, no `posted_at`, no `expires_at`
and no verification — judged entirely on a 1,802-char aggregator teaser.

**What it does.** One plain HTTP GET (no browser, no LLM) per judge-pool candidate that
has no `full_text`, sits on a re-posting mirror, or is past
`LISTING_REVALIDATE_AFTER_DAYS`; capped at `VERIFY_MAX_PER_RUN`, 8-wide, ordered
best-first by `_selection_score`. It runs against `rank_by_cluster` rather than the
allocated pool **on purpose**: dropping a dead row lets the following `_fair_allocate`
backfill that slot from the same cluster, so no cluster loses a slot to a corpse.
Dead rows get `dead_reason`/`dead_at`, after which the pipeline's existing
`dead_reason IS NULL` selection filters exclude them from every future run for free —
no new suppression path — and `_auto_hide_dead_roles` retires any already-shown card.

**The pass above does NOT guarantee the results page, and `_verify_final_picks` is what
does.** `_needs_liveness_check` is a *budget* heuristic — right for rationing ~40 fetches
across a rank pool, wrong for the dozen listings actually shown to a user who reasonably
reads "here are your matches" as "these exist". It skips anything carrying `full_text`
that isn't on a mirror and was verified inside `LISTING_REVALIDATE_AFTER_DAYS` (2), and an
ATS row with `full_text` from an earlier run hits every one of those: it skips this pass,
skips Phase 5 (`_needs_full_scrape` excludes ATS rows), and reaches the card checked by
nothing. So after the judge and **before any Role row is written**, every final pick is
verified unconditionally — ~12 plain GETs at the end of a ~4 minute run. The only picks
skipped are ones a fetch already read this run, tracked by `_verified_at` stamped on the
job dict by `_verify_listings_alive` and `_persist_scrape` (the same dicts flow all the
way to the persist site, so no extra plumbing was needed).

Three routes to an answer, strongest first:
- **ATS rows → re-read the vendor feed** (`_verify_ats_picks`). Strictly better than
  fetching the posting and cheaper: a vendor board lists exactly the reqs that are open, so
  a URL absent from the feed is *definitively* closed, where an HTML fetch gives at best an
  inferred answer (several vendors serve a soft-200 "no longer available" page). Cost is
  one call per distinct BOARD among the picks, 2-4 in practice. An **empty or failed feed
  yields no verdict at all** — it is indistinguishable from a board that closed every req
  at once, and `dead_reason` is unrecoverable.
- **Adzuna `/jobs/land/ad/` → `fetch_adzuna_details`**, for the reason `_needs_liveness_
  check` already skips those: the interstitial answers every request with a stub, so a
  direct fetch can only ever return "unverifiable".
- **everything else → `_classify_listing`**, unchanged.

A dropped pick's slot is refilled from the graded picks that lost the `FINAL_PICKS` cut,
and the refills are verified too — **once**. One extra round, never recursion: the point is
not to show a corpse, not to guarantee a full dozen.

**Browser escalation** (`_verify_via_browser`) covers the ~20% of checks where the host
declines to answer a plain request. It is a second `AsyncWebCrawler` launch (Phase 5's has
closed by then), bounded by `VERIFY_BROWSER_MAX` and `VERIFY_BROWSER_BUDGET_SECONDS`, and
**fail-open** — anything it also can't answer for is KEPT, because unknown is not dead.
Measured over 45 surfaced roles with `scripts/audit_listing_liveness.py`: plain HTTP gave
66.7% alive / 11.1% dead / **22.2% unverifiable**; with the browser, 80.0% / 15.6% /
**4.4%** — i.e. it converted 8 of 10 shrugs into a definite answer and found 2 more dead
listings. Three of the 5 dead found on the plain pass were `status="new"`, sitting in the
inbox at the time.

**Deliberately NOT built: post-run re-checking.** No endpoint re-verifies a role after its
run, and a saved role is never re-checked. The guarantee is "live when shown", not "live
forever". The card therefore badges the **absence** of a check, not its presence
(`RoleCard.unverifiedChip`): since `_verify_final_picks` verifies every pick
unconditionally, a positive "Checked live" chip was a constant on every card and carried
no information, while a row that was never verified at all (provisional/quick-scored, or
from a run predating the check) is worth saying. It fires on the stable property — no
`last_verified_at` on the row — never on a clock, because a role verified last week WAS
verified when it was shown, and ageing the chip into a warning would contradict the
guarantee above rather than restate it.

**Check order, and the trap in it.** 404/410 first (the workhorse — every genuine death
in a 45-row live sample was a hard 404, nothing else contributed one), then a schema.org
`validThrough` already in the past, then `_dead_listing_signal`. **Never call
`_EXPIRED_LISTING_RE` raw here**, and never hand any of it `_strip_html` output.

Both halves of that were live bugs, and the second one was hiding the first. `_strip_html`
removes TAGS but keeps the CONTENTS of `<script>`/`<style>`, which is right for its actual
remit (an ATS description fragment) and catastrophic on a whole document from a JS
framework. `full_auto._visible_text` (used by `_classify_listing` and by
`fetch_adzuna_details`' unbounded closure-phrase check) strips those elements first.
Measured on the flexa.careers page that prompted this: `_strip_html` gave **234,757 chars
with the closure notice at offset 107,967**, `_visible_text` gives **7,736 with it at 288**
— and `_looks_like_expired_listing`'s gates are calibrated on exactly those two numbers,
so the listing read as alive, was graded a pick, and was shown at rank 6 badged
"Checked live" while its page said *"we're really sorry but this job is no longer
available"*.

Two consequences worth keeping:
- **`_looks_like_expired_listing` no longer has a length ceiling** —
  `_EXPIRED_LISTING_MAX_CHARS` is gone and `_EXPIRED_LISTING_HEAD_CHARS` (1500) is the
  only gate. The ceiling was a proxy for "the phrase is somewhere incidental", and a bad
  one: the flexa page kept the ENTIRE job description rendered below the notice (16k of
  markdown), so no ceiling under 16k could ever have caught it. What makes dropping it
  safe is that the same `_visible_text` fix removed the ceiling's original justification —
  the bebee false positive was never in the page's TEXT, it was in the inlined script/style
  `_strip_html` left behind. Re-measured over 382 live pages (376 stored scraped pages with
  real text + 6 live bebee postings of 3.0k–6.6k visible chars): the pattern matches **zero
  of them at any offset**. The two bebee rows that did match were hard 404s at offset 36,
  which check 1 catches anyway. The head window is now the only guard — don't widen it.
- **A 200 that renders to nothing is `unverifiable`, not `alive`**
  (`_VERIFY_MIN_VISIBLE_CHARS`, 200 chars, and no JSON-LD either). The existing
  `len(body) < 400` floor reads the RAW body and cannot see this: flexa served **89,201
  bytes of Next.js bootstrap with 0 chars of readable text** to `_VERIFY_UA`, and the old
  code called that alive. Saying unverifiable is what routes it to `_verify_via_browser`,
  which renders the JS and returns a real answer — measured end-to-end, this exact listing
  now goes `plain GET → unverifiable/no_visible_text` then `browser → dead/expired_phrase`.
  Note the UA quirk behind it, which is worth not "fixing" blind: flexa serves the SSR'd
  page to an honest bot UA and the client-only shell to `_VERIFY_UA`'s Chrome string, i.e.
  here the browser-spoofing UA is what costs us the content. The architectural fix
  (unverifiable + escalate) is right regardless of one host's behaviour; the UA is not.

**Mirror hosts are not a drop list, and there is no host-level dead rate.** An earlier
reading of the data appeared to show bebee/glassdoor at a 100% dead rate — that was an
artefact of sampling old STORE rows, i.e. it measured **listing age, not host health**.
Re-measured against live URLs the split is bebee 1 dead/3 alive, glassdoor 1/2,
jobviewtrack 8/26, prosple 2 alive, and a live bebee posting serves a full JSON-LD
`JobPosting` with a 4.3k-char description. The platforms work; individual listings die.
`MIRROR_BRANDS`/`MIRROR_HOSTS` therefore do exactly two narrow things: prioritise which
rows get a fetch, and decide that an **unverifiable** row with no readable text isn't
worth a judge call. `ListingHostStat` records per-host outcomes for **observability only**
and nothing reads it to decide anything.

**Unverifiable ≠ dead.** A 202/403/429/empty response means the host refused to answer
(Cloudflare), not that the vacancy closed — prosple does exactly this. Those rows are
never marked dead. They're dropped only when they are *both* a mirror *and* have no
readable text (nothing to judge, no way to check); otherwise they take
`UNVERIFIED_RANK_PENALTY` on `_selection_score` **only**, never `_rank_score`, so the
card's "Fit estimate" chip and `RANK_REJECT_SCORE_FLOOR` keep showing the model's own
number — the same separation `RICH_TEXT_SELECTION_BONUS` respects.

**Some hosts cannot report closure at all, and a 200 from them is not evidence of life**
(`_LIVENESS_BLIND_HOSTS`, today just `linkedin.com`). This is a *narrower and different*
claim from `MIRROR_BRANDS`: an ordinary mirror still 404s or serves a closure notice when
its copy comes down, which is precisely what `_classify_listing` reads. A blind host
renders an apparently-healthy posting to a logged-out client regardless of the vacancy's
real state, so every check passes — and passes for a dead vacancy.

Measured on the listing that prompted it: a jsearch-sourced LinkedIn row (Finance Data
Analyst / Polaris Consulting International) shown at **rank 6** and stamped
`last_verified_at`, i.e. badged as checked, while the user's signed-in view of the same
page read *"No longer accepting applications"*. Fetched anonymously that URL returns
**200 with 11,755 chars of visible text**, an active apply button, "Applications so far
50" and "Closes 15 Sept 2026" — **zero** occurrences of "no longer", "closed" or
"expired" anywhere in 304KB of HTML, and **no JSON-LD `JobPosting` at all**. LinkedIn's
guest job-posting fragment (`/jobs-guest/jobs/api/jobPosting/{id}`) says the same. There
is no anonymous signal to read.

Three things about the fix, all deliberate:
- The verdict is **`unverifiable`, never `dead`** — nothing here is evidence the vacancy
  closed either, and `dead_reason` is unrecoverable. What it buys is the three things the
  pipeline already does with an unverifiable row: the `UNVERIFIED_RANK_PENALTY` demotion,
  the card's honest "not verified" chip (which fires precisely because
  `_persist_verified_alive` only ever stamps `alive` rows), and the
  mirror-with-no-readable-text drop.
- The check sits **after every dead route, not before the fetch**. A removed LinkedIn job
  really does 404, and 404 is the workhorse — every genuine death in the original 45-row
  sample was one. Only the NEGATIVE conclusion is withheld.
- Blind hosts are excluded from **browser escalation** but still counted in
  `final_verify_unverifiable`. The browser renders the same logged-out page, so it can
  only fail open, and `VERIFY_BROWSER_MAX` is 12 slots a host that genuinely 403s a plain
  client can still use.
`linkedin` is also in `MIRROR_BRANDS`, which it plainly is — the page that prompted this
renders *"This is an excerpt from Reed. Click apply to see the full job description … on
Reed.co.uk"* in its own body. Note the ceiling: LinkedIn rows arrive only via jsearch and
were 59 of 9,998 store rows, so this is a correctness fix, not a volume one.

**Adzuna's own pages cannot report closure either, and this one produced BOTH top
picks of a live run as already-dead listings.** Same shape as the LinkedIn case above,
on a primary source rather than a marginal one. Measured on the two roles:

* Golden Charter (ad 5831595906, rank 1): `/jobs/details/` returned **200 with 4,489
  chars** of full job description, no closure phrase, and a JSON-LD `validThrough` of
  2026-08-23 — still in the future.
* Connected Health (ad 5832285695, rank 2): likewise a full 6.5k description. Its land
  redirect resolves to `nijobs.com/job/107811063`, which reads *"This listing went
  offline. Sorry, the listing that you're looking for is expired."*

So every existing route said ALIVE: `_classify_listing` on the detail page, and all
three of `fetch_adzuna_details`' dead signals (404/410, passed `validThrough`, closure
phrase), none of which can fire on a page serving the original description with a
future expiry. `validThrough` here is Adzuna's own retention window, not the employer's
closing date.

Three changes, at `_verify_final_picks` only:
1. **Every Adzuna pick is now handled by the Adzuna branch, both URL forms.** It used to
   key on the `/jobs/land/ad/` interstitial alone, so a `/jobs/details/` pick fell
   through to the generic HTTP path — 1,033 of 1,578 store rows are that form (the API
   returns either in the same response). That is exactly how the rank-1 pick was missed.
2. **`fetch_adzuna_details` returning a description no longer means alive.** It can still
   return *dead*; anything else becomes `unverifiable`, so the row is kept and shown but
   carries the honest "not verified" chip instead of a false checked stamp.
3. **Land-form rows get one browser attempt at the tracking redirect**
   (`_verify_via_browser` honours a per-job `_verify_url`), and a **dead verdict is drawn
   only from the DESTINATION page's content, on a different host.**

> ⚠ **Never reconstruct the land URL from the ad id, and never read Adzuna's status
> code.** The obvious version of this derived `/jobs/land/ad/{id}` so details-form rows
> could be verified too. Tested against ads known to be LIVE, it is a false-positive
> machine:
>
> | ad | signed URL | bare (id only) |
> |---|---|---|
> | Tarmac (fresh, live) | 404 | 400 |
> | Sage (fresh, live) | 404 | 400 |
> | Connected Health (dead) | 200 → nijobs.com | 400 |
>
> A bare land URL 400s for **everything** — it reports a missing `se`/`v` signature, not
> a missing ad — and the signed URLs 404'd two live ads on the same pass a dead one
> returned 200. Adzuna's status codes degrade under repeated requests and carry no
> information about the vacancy. `dead_reason` is unrecoverable, so `_adzuna_land_url`
> returns the stored URL **verbatim or nothing**, and the verdict comes only from where
> the redirect lands.

Cost is small: 1–5 land-form picks per run across recent runs, against
`VERIFY_BROWSER_MAX` 12, at 2-4s each gathered concurrently. Coverage is partial by
design — 34% of Adzuna rows are redirect-verifiable; the other 66% are reported
unverifiable rather than guessed at. `funnel.final_verify_redirect_routed` counts them
separately from `final_verify_unverifiable`, because an Adzuna row here is a deliberate
routing state, not a host that refused to answer, and folding the two together would
make that counter read as a rising failure rate the moment this shipped.

Relatedly, **`_verify_listings_alive` no longer stamps `_verified_at` on Adzuna rows.**
It still calls them alive (that pass rations fetches across a ~40-row rank pool, and
penalising every Adzuna candidate there is a far larger change than the evidence
supports) — but the stamp would make `_verify_final_picks` SKIP the row, cancelling the
only check that can answer for it, and would write the `last_verified_at` that badges
the card as checked.

**`_EXPIRED_LISTING_RE` gained two phrasings** on the back of this: *"this listing went
offline"* and the **copular** *"the listing … is expired"* (the existing branch wanted
the auxiliary — "has expired"). Neither is Adzuna-specific; boards write the copular
form routinely, so this was a general gap. Re-measured over 461 stored scraped pages:
fires on **1**, exactly as before, i.e. zero new false positives. `is expired` carries a
`(?![\w-])` guard so it cannot match inside a hyphenated compound
("expired-air-handling"), which an adversarial probe did hit.

**`_needs_liveness_check` skips known dead-end URLs first.** Adzuna's `/jobs/land/ad/`
interstitial (`_KNOWN_DEAD_END_URL_RE`) answers every request with a stub, so fetching it
can only return "unverifiable" — spending a request to learn nothing *and* penalising the
row for a property of the URL scheme rather than of the vacancy. Those rows verify through
`fetch_adzuna_details` against `/details/{id}` instead.

**The fetch pays for itself twice.** Because a whole HTML page has already been retrieved,
`_persist_verified_alive` harvests the JSON-LD `description`/`datePosted`/`validThrough`
from it for any row still lacking text — 43% of surfaced rows carry a parseable
`JobPosting`. That fixes the other half of the bug: rows that were being judged blind
become judgeable at no extra cost. It reuses the enrichment rules exactly (only text that
BEATS the snippet is stored, `_has_full_text` must be set or the gate cache-key richness
marker goes stale, dates through `_merge_posted_expires`). The loop is driven by the
verification RESULTS, not by the rows the `SELECT` returned — keying it off the DB meant a
candidate with no matching `JobSeen` row silently kept its teaser.

Separately, `_drop_expired_candidates` now drops a candidate whose **stated** `expires_at`
has passed, at pool admission. Previously a passed closing date produced only prose
(`_listing_age_tag`) and a `rank_gate` score cap, so a definitively-closed listing could
still consume gate/rank/judge budget and still be shown. Unknown stays unknown — ~90% of
the store has no expiry date and is never penalised for it.

`full_auto._adzuna_description_from_html` was renamed `_jobposting_from_html` (thin alias
kept) — it was never Adzuna-specific, just a schema.org `JobPosting` reader.

### Location scope & country filtering

`location_scope` (`config.LOCATION_SCOPE_CHOICES`: local/national/international) is a
single-value attribute, like seniority, that decides how far the candidate's stated
location/country is trusted as a hard filter — computed once in
`snapshot.build_snapshot` into `engine_profile["country_codes"]`/`["location_scope"]`.
The three values form a strict superset chain, not independent switches: `local`
narrows to the candidate's city on top of the country filter; `national` (today's
default) country-filters but doesn't narrow by city, so it already includes every
`local` match; `international` drops the country filter entirely, so it already
includes every `national` (and therefore `local`) match. `LocationPicker.tsx`'s scope
buttons reflect this by highlighting every choice up to and including the selected one
(`SCOPE_ORDER.indexOf(choice) <= SCOPE_ORDER.indexOf(scope)`), not just the single
active value, so picking "International" visibly lights up Local + National +
International rather than looking like an exclusive radio choice.

`national` is a **fail-closed** default, not "no filter": with no explicit country chip
picked, the filter defaults to the country inferred from the candidate's typed location
text (`_infer_region`) rather than sitting inert — a prior bug let a profile with no
country row at all silently mean "Global," letting out-of-scope roles through. The only
two ways to actually clear the country filter are picking "International" scope, or
picking the "Global (no filter)" country chip while scope is "National" — but the latter
had a bug of its own: `explicit_global` (the flag for "the Global chip is selected") was
only ever consulted when `location_scope` was blank/unset, so choosing Global while
scope was explicitly "national" (the common case, since it's the default) was silently
discarded and the fail-closed inferred-country filter applied anyway (a live run logged
`[pipeline] country filter ['gb']: 3562 -> 1019` despite Global being selected). Fixed in
`snapshot.py` by checking `explicit_global` directly whenever `scope == "national"`, not
just in the "scope was never set" fallback — scoped specifically to `national` so a
stale "global" country row left over from an earlier national selection doesn't also
waive the country filter for `local` scope, which always narrows by country+city
regardless of any country chip (the UI hides country chips entirely once scope leaves
"national").

The country filter itself (`engine._filter_by_country`) is **no longer positively-must-
match**: it was hard-dropping ~97% of genuinely-UK listings because the worldwide
`countries_data.py` token set carries only ~20 UK cities, so most postings (which name only
a town like Slough/Watford/Derby) resolved to `None` via `country_of` and were dropped — a
live UK run collapsed `country filter ['gb']: 2502 -> 63`. It now keeps a job **unless it
positively resolves to a *different* country**, with two escape hatches: (1) country-scoped
boards (`_COUNTRY_SCOPED_BOARDS` = reed [UK-only] / adzuna [always queried on the profile's
own country endpoint]) are trusted unconditionally, and (2) an unrecognised/blank/descriptor
location is kept, not dropped. The positive in-country check uses `full_auto.country_matches`
(tests the *allowed* codes directly) instead of `country_of`, which sidesteps `country_of`'s
alphabetical first-match misroute (e.g. "Newcastle" → `au` because Australia sorts before
`gb`, even though gb also carries the token). The cheap gate's work-arrangement axis and the
final judge's LOCATION disqualifier remain the real location enforcers for anything kept.
Relatedly, `select_sources_for_run` only adds the `CareerjetSource` aggregator to the
always-on tier when the profile's country is **not** Adzuna-supported (`cc not in
_ADZUNA_SUPPORTED`) — it's the essential coverage backstop for UAE/etc. (Adzuna serves
neither, Reed is UK-only) but redundant blank-company repost noise for UK, where it was
crowding out the clean Adzuna/Reed feeds (worsened by `fetch_adzuna` silently dropping a
whole term on any API error, now retried once).

The candidate's own Remote/Hybrid/On-site preference(s) are captured as `location`
attribute rows too (same attribute type as the free-text city — `LocationPicker.tsx`'s
`WORK_TYPES` buttons), split apart from the place name in `snapshot.build_snapshot` and
exposed to the engine as `engine_profile["work_types"]`. See the search-pipeline section
above for how this now actually reaches `screen_gate`'s work-arrangement axis.

### Visa sponsorship filter (`backend/app/services/sponsors.py`)

A single-value boolean preference (`visa_sponsor_only`, default **off**, bottom of
Dashboard → Preferences) restricting results to employers on the Home Office register of
licensed sponsors. Same dev-time-generator / committed-plain-data-module posture as
`uk_geo_gen` and `uk_charity_gen`: `scripts/gen_uk_sponsors.py` reads the gitignored
register CSV from `txt non code/` and writes `backend/app/uk_sponsor_gen.py`. No API and
no LLM at runtime — every lookup is a set/dict hit.

**This is the only filter in the pipeline that DROPS ON UNKNOWN**, and that inversion is
deliberate. Everywhere else (country, listing age, salary floor, liveness) unknown means
no penalty, because wrongly dropping a good role costs more than carrying a doubtful one.
Sponsorship reverses that: a candidate who needs a visa cannot act on a role they can't
confirm sponsors. Off by default, so a candidate who doesn't need it never meets the
behaviour. Don't "fix" this to match the rest of the pipeline without re-reading this
paragraph.

**Storage shape.** 142,694 CSV rows → **127,227 unique organisations** → 147,361
normalised keys (trading-as aliases are expanded on the REGISTER side too, which is what
makes `Canada Life` an exact hit via `CLFIS (UK ) Ltd - Canada Life`). Held as ONE
newline-joined string literal, not 127k separate literals — measured import 0.046s, index
build 0.198s, both lazy so a profile with the preference off pays neither. All routes are
kept: Skilled Worker alone is 121,891 of the 127,227, so filtering by route changes
nothing and would risk dropping the Charity Worker route the charity vertical cares about.

**Matching, and what it can't do.** Exact normalised match, else a **guarded prefix** match
requiring ≥2 shared leading tokens. That guard is the false-positive control, not a tuning
knob: unguarded, a company named `Futures` matches `Wild Futures`. It costs real hits
(a listing saying only `Kaplan` never reaches `Kaplan Financial Limited`) and that is the
right trade under a hard filter. Measured on a live 9,042-row store (1,244 unique
companies): **20.6% exact + 3.2% prefix = 23.8% of unique companies, 10.0% of rows.**
Re-measure with `scripts/audit_sponsor_match.py` — it prints the per-source table, the
prefix-only hits (where a false positive surfaces first) and the misses.

Two limitations are **structural** and no matcher tuning fixes them: **recruitment
agencies** (the listing names the agency, not the licence holder — `Hays Specialist
Recruitment` misses even though `Hays PLC` is registered) and **blank-company** aggregator
rows. Both are dropped under the strict setting, and the funnel counts blank-company drops
separately so the cost stays visible.

**The third limitation is the one the register can never answer, and it needed a second
signal.** The register is EMPLOYER-level: it says an organisation holds a licence, never
which vacancies it will use it on. A licensed employer routinely advertises roles it will
not sponsor — **21 rows in the measured store** are exactly that, and every one of them
carried a "Visa sponsor" badge and passed the sponsors-only filter.
`sponsors.statement_in_text` reads what the LISTING ITSELF says
(`"offered" | "not_offered" | None`), and that outranks the register in **both**
directions: a `not_offered` row is dropped even when the employer is licensed, and an
`offered` row is kept even when the register cannot resolve the company — which is the
first thing that has ever closed part of the agency hole above (4 rows in the same store).
Counted as `sponsor_filter_listing_said_no` / `_said_yes` so the feature's only measurable
effect doesn't vanish into the totals.

Free, offline and deterministic — **not** an LLM axis. The phrasings are highly formulaic,
a regex is auditable and re-measurable against the store, adding an axis to `screen_gate`
would cost a `screen_v` bump (re-screening the whole store for identical answers on the
untouched majority), and this reads the WHOLE text where the gate sees only the first
`GATE_LISTING_TEXT_CHARS` — a sponsorship note is almost always near the end
("Please note, we are unable to provide sponsorship…").

Three things in it are load-bearing, all found by measuring:
- **The non-visa senses are the majority and are excluded explicitly.** Of 12,273
  text-bearing rows, 293 mention "sponsor" and most are not about visas at all:
  "company-sponsored lunches/life insurance", "executive/programme sponsors", "event
  sponsorship sales", "paired with a sponsor who mentors you", "we sponsor co-working
  space", and — the one that also blocks a naive positive — "manage visa applications …
  needed for **sponsoring employees**", which is a JD *duty*. That is why a nearby "visa"
  is not sufficient context on its own.
- **Negation must reach the verb by two routes.** The first cut required a second
  "sponsor" token after the verb, which caught "unable to OFFER SPONSORSHIP" but not
  "unable to SPONSOR visas" — and the latter then matched the POSITIVE pattern on the
  "able to sponsor visas" sitting inside "unable to". **Six real listings were classified
  as offering sponsorship when they said the exact opposite.** `_NEG` is factored out for
  that reason, and the positive pattern carries `(?<!un)` / `(?<!\bno )` guards
  independently, because a false "offered" spends someone's application.
- **Clause-scoped, not document- or sentence-scoped.** A document search pairs a "not
  eligible" in the benefits section with a "sponsorship" three paragraphs away; sentences
  aren't enough either, because scraped headers run 300 characters with no full stop. The
  window is bounded by distance *and* by the nearest clause break, and that window is also
  the quote the card shows.
Measured after those fixes: **7 `offered` (all genuine), 120 `not_offered` (all genuine),
12,146 silent.** Negative wins ties, checked first and returned immediately.

**UI.** The chip is three-state and no longer says "Visa sponsor" — that wording asserted
a property of the *job* while holding a fact about the *employer*. `"Sponsorship offered"`
/ `"No sponsorship"` come from the listing; `"Employer sponsors visas"` is the register's
answer when the listing is silent, worded to say whose property it is. A false
`sponsor_licensed` still earns **no** chip (absence must read as "unknown"), which is why
the negative fires on the STATEMENT and never on the register. The employer's own sentence
renders under the card as `The listing itself says: "…"` — a claim this consequential
should be checkable in one glance, and "No sponsorship" costs the candidate a role if we
got it wrong. All of it still gated on the candidate's own filter being on.
`VisaSponsorToggle`'s note now carries both caveats plus an **info dot** opening a panel
that states plainly what each badge means and what is and isn't likely to be sponsored in
practice (pay near the Skilled Worker threshold, short contracts, small never-sponsored
employers vs permanent roles at larger employers and shortage occupations). It is the only
place the UI explains any of this; keep it if the semantics change.

**`fetch_ats` now carries the employer name**, threaded from `select_ats_batch_for_run`'s
`(company, vendor, token)` through `gather_jobs`, which used to discard it. Without it
every ATS row identifies its employer by an opaque token (`tiger-analytics`) — that is
what the card renders and what every company-keyed consumer sees. **Note the ceiling this
runs into**: `seed_ats.py` writes `(token, vendor, token)`, so `CompanyATS.company` IS the
token for 1,818 of the 1,820 registry rows and only direct-employer-crawled rows carry a
real name. So this fix delivers no measurable lift *today* (ATS rows stay at 4.7%); it is
correct, it fixes the card, and it grows with the crawl. The real unlock would be
backfilling `CompanyATS.company` from each vendor's own board metadata (Greenhouse and
SmartRecruiters both expose a company name). Safe for identity: `identity_hash` uses the
canonical-URL branch for ATS rows, so no store row is orphaned — but `_family_key`/
`_dup_key` change shape, so decided-family suppression briefly misses against Role rows
saved under the old token form.

`Role.sponsor_licensed` and `Role.sponsor_statement` answer **two different questions and
must not be collapsed** — the employer's licence, and what this listing says about this
vacancy (`sponsor_statement_quote` carries the sentence). `sponsor_licensed` is
**three-state and the NULL matters**: True/False once checked,
NULL when the listing named no employer. The card badges only a positive match — "not on
the register" is not the same as "does not sponsor" for an agency posting, and an absent
badge reads correctly as "unknown" where a "Not a sponsor" badge would not. Both are
stamped on
every run, not only when the filter is on — but **shown only while the candidate's own
sponsors-only filter is on** (`RoleCard.useSponsorFilterOn`, reading the same
`visa_sponsor_only` attribute `VisaSponsorToggle` writes). Keep the two apart: stamping
always is what lets a user switch the filter on and have their existing rows already
answered, while a candidate who does not need a visa should not get a chip answering a
question they never asked, in the same row as facts about the job. The hook reads
`useProfiles()` + `useAttributes()` inside `RoleCard` rather than being threaded down —
there are twelve call sites across `/search` and `/my-roles`, and TanStack dedupes the
query to one request however many cards mount.

Discovery adapts when the filter is on (see the discovery section): a sponsor-priority
tier ahead of all three existing tiers in `select_ats_batch_for_run` (measured: sponsors in
the first 10 boards go 3 → 10), deeper Reed/Adzuna paging, and `SPONSOR_TERMS_PER_RUN`
employer-scoped search terms (`"<role> <employer>"`) drawn from sponsors that have already
surfaced for this profile, deduped by normalised key so two slots don't both go to
"Davies" and "Davies Group".

**Not doing: a sponsor-seeded domain crawl.** The register carries no domains, so it would
have to guess them. The charity crawl finds a supported ATS on 1.3% of *known-good*
domains; guessed domains would be materially worse.

**The salary floor** (`visa_sponsor_min_salary`, single-value like `commute_miles`/
`max_listing_age`, default `config.DEFAULT_VISA_SPONSOR_MIN_SALARY` = 41700, "0" = no
floor). The Skilled Worker route is not one cutoff -- GBP 41,700 is the general
going-rate threshold for a standard applicant, but several categories are sponsorable
well below it: new entrants (under 26, a recent Student/Graduate visa switcher, or
training toward a professional qualification) at ~70% of the going rate for up to four
years, PhD holders, roles on the Immigration Salary List, and a specific health/
education occupation table -- down to roughly GBP 31,300-37,500 depending which
applies. The Health and Care Worker visa is a separate route governed by none of these
figures. None of those categories (age, visa-switch history, PhD subject, occupation
code, ISL membership) are things this app reliably knows about the candidate or a
listing, so rather than guess, the floor is just a candidate-editable number --
`VisaSponsorToggle`'s picker offers the standard rate plus the other named tiers as
presets, or a free-entry field, and defaults to the standard rate so a candidate who
does nothing gets the conservative reading.

Enforced in `engine._filter_by_sponsor` as a second, separate test **after** the
register/statement check (so it applies uniformly to a register match or a listing that
says "offered" -- both are meant to be a real, sponsorable vacancy). Deliberately does
**not** inherit the sponsor filter's own "drop on unknown" inversion: salary data is
sparse (`_filter_by_salary`'s own note), so a job with no parseable stated salary is
always KEPT, and only a CONFIRMED annual max below the floor is dropped -- the annualised
comparison via `salary.to_annual` is the same one `_filter_by_salary` already uses.
Counted separately in the funnel (`sponsor_filter_below_salary_floor`, Settings ›
run-funnel) so this cost stays as visible as the blank-company one above it.

### Commute distance (`backend/app/services/geo.py`)

Until this existed the app had exactly **two** location concepts — "same country" and
"the candidate's city name appears as a substring" (`_filter_by_local_place`) — and
nothing between them. Commute distance is the first filter a real seeker applies and
was the one thing the app could not express: a Newcastle role and a next-street role
were indistinguishable at every stage. It also fixes the display side of the same gap
— board location fields routinely carry a bare postcode (`B706AW`, `LS101EY`,
`GU98AD`) which rendered verbatim on a card and simply read as broken.

`backend/app/uk_geo_gen.py` is a committed plain-data module (2,917 outward codes,
~10k place names) generated by `scripts/gen_uk_geo.py` from ONS postcode centroids via
api.postcodes.io — the same dev-time-generator / plain-data-module posture as
`scripts/gen_countries.py`. **No API and no LLM at runtime**: every lookup is a dict
hit and the distance is haversine. Outward-code precision only (the `B70` of
`B70 6AW`); a commute radius is chosen in tens of miles, so centroid error is inside
the noise, and the full 1.8M-row ONSPD is neither committable nor needed.

Two things in the generator are load-bearing and easy to reintroduce as bugs.
(1) The crawl walks `/outcodes/{oc}/nearest`, whose **default** radius is a few km —
at the default the breadth-first walk closes over a subset of the country and
terminates at 1,005 of ~3,000 outcodes; `radius=25000` is what makes it spread.
(2) Place names repeat across the UK and **averaging their points is wrong**: a first
cut took the mean of every outcode naming "Farnham" and landed 34 miles from the
Surrey town, in a field, because Essex/Dorset/North Yorkshire each have one. Names are
therefore single-link **clustered** (`_cluster`); a name with one cluster goes in
`PLACE_COORDS`, a name with several goes in `PLACE_VARIANTS` with the county/district a
listing would write after it, and `geo._pick_variant` disambiguates on that qualifier,
falling back to the largest variant only on a **strict plurality** and returning None
on a tie rather than coin-flipping a listing tens of miles.

**Unresolved is a first-class answer and the common one.** "UK", "Remote" and every
non-UK location resolve to None, and None always means *no distance information*,
never *far*. Scope is UK-only (it is the ONS dataset); a profile based elsewhere gets
no chip and no distance filter rather than wrong answers.

Wiring:
- `commute_miles` is a single-value attribute (miles as a string, `"0"` = no limit,
  `config.DEFAULT_COMMUTE_MILES` = 30 when unset). It **rides the Location row's own
  Hard/Soft** rather than carrying its own, since it answers the same question, and is
  only ENFORCED at `location_scope="local"` — National/International have explicitly
  opted out of narrowing by place. `LocationPicker` shows it only at Local scope.
- `engine._filter_by_local_place` gained a radius argument and now has **two ways in**,
  either of which keeps a job: the original name match, or within the radius. Distance
  is only ever an additional way IN — radius 0 or an unresolvable location on either
  side leaves the original behaviour exactly as it was, so **no job that passes today
  can be dropped by this**. Note the local asymmetry: an unresolvable listing location
  is dropped here (that is what "Local" already did), whereas everywhere else in the
  pipeline unknown means no penalty.
- `engine._annotate_geo` stamps `_distance_miles`/`_location_label` onto the candidate
  dicts **once**, after the filters, and `_role_location_fields` copies them at both
  Role-persist sites exactly as `_role_date_fields` copies the listing dates. Done this
  way because neither persist site has the profile in scope. It runs at **every** scope,
  not just local: a distance is worth showing on a card even when it isn't filtering.
- `Role.distance_miles` / `Role.location_label` are nullable and usually null.
  `location_label` sits *alongside* `location` rather than overwriting it, so what the
  board actually said is never lost. The card renders `location_label || location`, and
  0 miles renders as "Nearby" — outcode centroids can't tell same-town from
  same-street and shouldn't pretend to.
- Deliberately NOT wired into any prompt. `rank_gate`'s GEOGRAPHY rule and the judge's
  LOCATION disqualifier are unchanged, so no cache version needed bumping.

### Salary normalisation (`backend/app/services/salary.py`)

Pay reached the app as free text and stayed that way — `"£28,505 to £34,613"`,
`"£30,000 (rising to £45,000)"`, `"up to 70k"`, `"£200 per day"`, `"GBP 28800 - 48000
per year"` — so nothing could compare two listings, and the candidate's salary floor
was enforced against a raw `salary_max` from a source that may have meant *per hour*.
JSearch alone returns HOUR/DAY/WEEK/MONTH/YEAR, so a £25/hour role (≈£48,750 a year)
was hard-dropped at discovery for a candidate with a £30,000 floor.

`parse_salary` returns `{min, max, period, currency}` in the **stated period's own
units, never annualised** — `to_annual` (1950 h/yr, 260 d/yr) is applied only for
coarse comparison and the card's toggle, and a converted figure is always rendered
with `~` and "(stated a year)" because the conversion assumes full-time hours the
listing never stated. Currency is recorded but **never converted** (an FX rate is not
something to fetch per search); that coarseness is pre-existing and now the only unit
assumption left.

**The single most important rule: `text` must be a salary FIELD, never a job
description.** Run over the snippets of a live 8,050-row store the parser "found" pay
in 4,389 of them, and a 15-listing audit found 12 wrong — employee counts ("270+
locations and 4,000+ employees" → £4,000/yr), requisition numbers ("Requisition
Number: 51630" → £51,630/yr), years of experience ("5 years … at least 3 years" →
£3–6/hr), signing bonuses ("$1,000 new hire bonus" → $1,000/day). The plausibility
bounds cannot save you: they reject impossible *salaries* and cannot tell a salary
from any other number of similar size. So `_jobseen_salary_fields` and
`_filter_by_salary` pass structured source figures **only**, and
`scripts/backfill_role_geo_salary.py` deliberately does not backfill `jobs_seen`.
Percentages are stripped before any number is read, or "up to 12% bonus" parses as £12.

Persistence, and a latent bug it fixed: `salary_min/max/period/currency` are now
stored on **both** `Role` and `JobSeen`. The store side matters more than it looks —
the pipeline's candidates come from `jobs_seen`, not from the fresh-discovery dicts,
so the structured salary every board API returns was read once by the discovery-time
filter and then thrown away. `full_auto._listing_salary_suffix`, which feeds
`screen_gate`'s salary axis and `rank_gate`'s HARD DOWNGRADE (e), was therefore
rendering **empty for effectively every candidate ever gated**. It now also states the
period, without which "Salary: 25-32" from an hourly listing reads as catastrophically
underpaid. Sources gained the fields they already had and were dropping:
`salary_currency` (Reed=GBP, USAJobs=USD), `salary_period` (Adzuna="year" — its
figures are documented as annualised; JSearch/USAJobs pass theirs through). Note this
changes prompt CONTENT without a `screen_v`/`rank_v` bump — `gate_cache` keys on job
id + profile signature, not on the listing block, so already-cached rows keep their
old verdicts and only new/changed ones see the better prompt.

The card renders the normalised figure in the user's chosen unit, falling back to
`salary_text` verbatim whenever the parse found nothing ("Competitive", "Negotiable",
"National Minimum Wage" state no figure and showing what the employer wrote beats
showing nothing). `SalaryPeriodToggle` (Yearly/Hourly) is shared by `/search` and
`/my-roles` through a module-level store in `lib/salary.ts` read via
`useSyncExternalStore` — not a context, which would have to be threaded through two
page trees to move one enum — and is hidden unless something on screen actually has a
parsed salary. **`lib/salary.ts`'s `ANNUAL_MULTIPLIER` must stay in sync with
`services/salary.py`'s**, same as `WORK_TYPE_VALUES` is mirrored in `LocationPicker`.

### Direct-employer discovery (UK charity vertical)

`backend/app/services/direct_employer.py` + `scripts/gen_uk_charity_seed.py` +
`scripts/crawl_direct_employers.py`.

**The problem it was built for.** The ATS registry is ~1,800 companies and
overwhelmingly US-headquartered: on a measured live run it produced 4 of 85 surfaced
roles (4.7%) while the term-based board APIs produced 79 (93%), and the single largest
entry in the store is `gh:spacex` at 1,943 rows that the country filter then discards.
More ATS *vendors* does not fix that — vendor coverage isn't what's missing, UK employer
coverage is.

**The seed list is the easy half and it's real.** The Charity Commission register
(`txt non code/publicextract.charity.json`, 507MB, gitignored) yields **8,191 unique UK
charity domains** at £1m+ income after filtering to Registered + non-linked + usable
website. `scripts/gen_uk_charity_seed.py` streams it line-by-line (the extract is a JSON
array formatted one object per line; `json.load` would need several GB) and writes the
committed plain-data module `backend/app/uk_charity_gen.py` — same dev-time-generator /
committed-artifact posture as `gen_uk_geo.py` and `gen_countries.py`. The income floor is
the load-bearing filter: 103,155 registered charities have a website but only 8,437 report
over £1m, and income is the dataset's only proxy for "employs anyone".

**The crawl deliberately does not parse jobs.** The obvious reading — a bespoke HTML job
parser per site — is thousands of layouts, permanently breaking, and produces exactly the
text-starved rows the pipeline spent months fixing. Most employers don't host vacancies
themselves; they link out to a hosted ATS. So the crawl reads a careers page, works out
*which ATS the employer uses*, live-validates the token, and writes `(company, vendor,
token, keyword="charity nonprofit voluntary")` into `company_ats`. From there nothing
changes: `fetch_ats` returns clean, text-complete postings on every run. One crawl
converts an employer into a permanent discovery source rather than into one scrape that
rots. Token extraction is shared with `harvest_ats_tokens` via
`full_auto.ATS_TOKEN_PATTERNS`/`ats_tokens_in`, so the two can't drift on what a valid
token looks like.

**The measured result, which does not support the original strategic framing.** Over 150
top-income domains: **2 boards found (1.3%)**, 124 `no_ats`, 23 `unreachable`, 1
robots-blocked. A random sample across the income range found **0 of 72**. Extrapolated,
the whole 8,191-domain list is worth roughly **~110 boards** — worth having (it is
UK-relevant where the current registry is not, and it costs no API credits and no
search-time latency) but it is not the path that fixes the 4.7% problem. Two reasons it
lands where it does: big charities' careers pages are JS-rendered, so there is no link to
follow without a browser; and the ones that do use a hosted ATS mostly use enterprise
platforms we can't read. Note one of the two hits, **Marie Curie, is a SmartRecruiters
board — reachable only because of the vendor added alongside this**.

**The misses are the more durable output.** A `no_ats` row records *which* unsupported
platform it saw (`_FOREIGN_ATS_HOSTS`), so the failures accumulate into a ranked,
measured case for which vendor to integrate next instead of an undifferentiated pile —
read it with `scripts/crawl_direct_employers.py --misses`. First 150 domains:
Workday 5, current-vacancies 2, Pinpoint 2, then a tail of singletons (SuccessFactors,
iCIMS, Jobtrain, Teamtailor, PeopleHR…). **There is no single vendor that unlocks
meaningful UK charity coverage** — that is the finding, and it is the thing to re-check
before anyone proposes "add more ATS vendors" again.

**A crawled board was nearly unreachable until `select_ats_batch_for_run` grew a third
tier.** That function used to split the registry into "keyword matches the profile" and
"everything else", where the match was one boolean over search terms *and* sectors
merged — so a board tagged `data analyst` and one tagged `charity nonprofit` were
equally preferred for a charity-sector data analyst. The tier is then rotated in
insertion order, and crawled rows are the newest, so they sat at the back: measured on
the live store, a freshly-crawled charity board landed at **index 283 of a 285-row
preferred list**, ~7 runs of rotation before the 40-slot batch would reach it, ranked
behind 283 boards matching only the generic word "data". A SECTOR word is much stronger
evidence that a company is in the candidate's field than a role-shape word like
"data"/"analyst", which matches employers in every industry — so sector matches now get
their own tier ahead of term-only matches, each still rotating independently so nothing
is starved. Without this the crawl's output is real but effectively invisible, which is
worth remembering before judging the yield numbers above.

**Board count is not the metric; `crawl_status()["yield"]` is.** A board contributing 400
listings that all die at the embedding pre-filter is worth less than one contributing 3
that get surfaced, so the status output joins the charity-tagged tokens back onto
`jobs_seen`/`Role` and reports discovered → gated → shown → saved/applied. Deliberately a
read-side join over existing columns rather than a new funnel counter: it costs the
search path nothing, which is the right trade for a feature with this measured yield.

**Politeness and scheduling.** This is the only part of the app that fetches arbitrary
third-party sites that never asked to be crawled, so it honours `robots.txt` (fails open
on a missing one, records `blocked_by_robots` rather than proceeding) and sends an honest,
identifiable User-Agent. The ~14% `unreachable` rate is mostly Cloudflare returning
403/202 to that UA; it is left as-is on purpose rather than disguised. There is **no
in-process scheduler**: the recheck window is 120 days, so a timer thread inside the
single-instance box whose SQLite file is the app's only store is infrastructure risk with
no matching payoff. The schedulable units are `scripts/crawl_direct_employers.py --limit N`
(resumable; re-running picks up where it stopped) and `POST /admin/crawl` for an external
scheduler. Scheduled discovery/embed *pre-runs* were considered and not built — they save
~1.5 min off a ~4 min run whose perceived wait is already solved by progressive paint
(first cards at ~7s), and the crawler turned out not to need the scheduler that would have
justified them.

### Ghost-listing detection (`backend/app/services/ghost.py`)

A ghost listing is an advert with no real vacancy behind it — already filled, a
standing CV-collection pipeline, a cancelled req never taken down. The candidate can't
tell from the page, and it costs them an application.

**The scoring shape is a count of NAMED RULES, not a weighted score**, for the reason
`dynamic_hard_drop_threshold` counts soft-axis failures and `fit_level` was rewritten
(v16) to derive mechanically off a checklist: there is nothing to fit weights to (24
feedback rows, and zero terminal application outcomes ever recorded), and a chip reading
"Possible ghost listing" is an accusation about a named employer — `0.71` can't be shown
to anyone, "posted 8 months ago, and the text says it's a talent pool" can. A count can't
say 400 days is worse than 95, so rules come in two tiers exactly as `screen_gate` does:
**decisive** ones are individually sufficient for `high`, **ordinary** ones must agree in
pairs (`ordinary == 1` → `medium`). There is deliberately **no `low`** — a value covering
~90% of rows would get rendered and train the reader to ignore the chip.

| rule | tier | fires when |
|---|---|---|
| `stated_age_absurd` | decisive | definite `posted_at` ≥ 365d (subsumes the next, counted once) |
| `pipeline_language` | decisive | title/body announces a talent pool or speculative application |
| `takedown_repost` | decisive (dormant) | a `repost_key` sibling has `dead_at` and this row's `first_seen` is later |
| `stated_age_extreme` | ordinary | definite `posted_at` ≥ 90d |
| `stale_untouched` | ordinary | `posted_at_approx` and ≥ 180d |
| `date_refreshed` | ordinary (dormant) | `posted_days + 14 < observed_days` |
| `evergreen_observed` | ordinary (dormant) | `seen_days ≥ 30` at density ≥ 0.6 |
| `repost_burst` | ordinary (dormant) | ≥3 rows, ≥45d `posted_at` span, ≥2 distinct `first_seen` days, **not agency** |

**Invariants, all load-bearing:**
- **Every rule fires on POSITIVE evidence; none fires on missing data.** A listing with
  no `posted_at` produces no signal — the same discipline `_listing_age_tag` follows
  ("silence is not evidence of age"). This is the *only* thing that makes the card's
  absent-badge read as "nothing fired" rather than "unknown", and the /search "none
  flagged" line honest. Note the semantics are **inverted from `sponsorChip`**, which
  badges only positives precisely because *its* absence must read as unknown. Add a rule
  that fires on absence and both of those become lies.
- **Never a hard drop.** `GHOST_SELECTION_PENALTY` (8.0) rides `_selection_score` only —
  never `_rank_score` — under the same rule as `RICH_TEXT_SELECTION_BONUS` /
  `UNVERIFIED_RANK_PENALTY` / `SOFT_VIOLATION_SELECTION_PENALTY`. Set *below* the
  soft-violation penalty (12.0) because a stated preference being missed is firmer
  evidence than an inference from a date. Unlike `dead_reason`, a suspicion must be undoable.
- **Signals are persisted alongside the verdict** (`Role.ghost_level`/`ghost_signals`,
  `JobSeen.ghost_signals`), not re-derived on read: `dead_at` is stamped once, `seen_dates`
  truncates, and a repost group's membership changes as rows arrive, so a verdict
  re-derived from a later store is not the same verdict.
- **No LLM, no network, no DB in the rules.** Same bar as `_pool_quality_prescreen`.
- **Thresholds are imported from `full_auto`, never restated** — the `SOFT_GATE_AXES` lesson.

**Calibration is measured against roles the user engaged with, never the store-wide hit
count** (`scripts/backtest_ghost_rules.py`, read-only, free). Most of the store is US ATS
rows the country filter discards, so a rule can fire thousands of times and never reach a
card. Bar: **zero applied-to roles flagged `high`**. Current: 0 of 6 applied, 0 of 2 saved,
0 of 88 ever-shown flagged high (2 medium, both genuinely 3+ months old).

Two measurements that shaped the rules and should not be undone:
- **`pipeline_language` is TWO patterns, title-broad and body-narrow.** One broad pattern
  over title+body over-fired badly: 22 of 57 hits were one staffing agency
  (`blue-united-sourcing`) whose boilerplate says "Talent Network" atop *every* posting,
  including "Registered Nurse (RN) - ER" — specific, real, fillable vacancies. A pipeline
  ad announces itself in its **title**; the same words in the body are a call-to-action
  appended to a real posting. Splitting took it 57 → 30 with every remaining hit genuine.
- **All three `repost_burst` guards are load-bearing.** Simulated at day 40 over 4,000
  candidates: all guards → **1** fire; without the agency guard → **98**; without the
  first-seen-days guard → **82**; without the span guard → **15**; with none → **595**.
  `noir|.net developer` alone is 88 rows / 88 distinct URLs / 6 distinct `posted_at`
  inside 3 days — one recruiter spraying geographic variants, which is not reposting.

**`ghost.is_agency` is three-state and its structural half detects a POSTING PATTERN, not
an employer type.** Name-regex alone caught 271 of 1,547 companies but only 5.9% of rows
and missed Noir/Hays/Robert Half/Michael Page/Adecco/Randstad outright, so there are three
tests (name regex, `_KNOWN_AGENCIES` measured list, structural few-titles-many-locations),
any sufficient — now 9.1% of rows. The structural half's only unique hit is `howdens
joinery` (15 rows, 4 titles, 15 locations), a genuine multi-site retailer — and
suppressing `repost_burst` for it is the **correct** outcome reached through a slightly
wrong name, because one depot role across 15 towns has the identical signature to agency
spray. Safe only because agency status may only ever *modify* which rules apply, is never
itself ghost evidence, and **never reaches a card** (the judge's own WISH-LIST rule treats
agency-posted as a reason to be more generous).

**UI**: `high` is pulled out of the ranked lists into a collapsed count-plus-link section
on `/search` (after `unreviewed`, before `crossed`) carrying the **full** action row —
the user overrules us, not the other way round; `medium` stays inline with a chip and the
page never re-sorts (`fit_rank` is positional from the judge). A `§ghost` marker in
`_compose_analysis` renders the fired rules as sentences, because the chip alone is
unexplainable. **Any new section must be added to the "Showing N results" sum** at
`search/page.tsx` — it's summed from the exact rendered buckets, and the comment there
records the bug that caused.

### Scheduled observation & liveness re-check (`backend/app/services/observe.py`)

The longitudinal half of ghost detection, and **the reason this had to ship before the
rules that read it**: a week not observed is permanently lost.

- **`run_pass`** (`scripts/observe_listings.py`, `POST /admin/observe`) — discovery +
  sighting upsert only. **Its entire safety contract follows from one fact: it never
  writes `jobs_seen`.** That alone means no row marked `enriched`, no `Role`, no
  embeddings, no gate/rank/judge. It also never creates a `SearchRun`, which is precisely
  why it can't consume `MAX_SEARCHES_PER_DAY` (`_searches_today` counts `SearchRun`).
  `profile_id = -1` keeps it off any user's source-rotation cursor. `OBSERVE_SOURCES`
  defaults to reed+adzuna — **zero OpenAI spend**. Terms are the union of every profile's
  active target roles **plus** the titles of listings already under observation: without
  that carry-forward a listing drifting out of the term rotation vanishes from the window,
  and a gap is indistinguishable from the ad coming down.
- **`recheck_liveness`** (`scripts/recheck_liveness.py`, `POST /admin/recheck`) — the
  high-value half. `dead_at` was set on **3 of 9,998 rows** and `last_verified_at` on
  0.36%, because the existing machinery only ran over an interactive search's judge pool.
  Reuses `engine._classify_listing` verbatim (documented DB-free for exactly this).
  Plain GETs, fail-open, **no browser escalation** (unattended, nobody waiting). A
  confirmed death fans out to `jobs_seen` so the pipeline's existing `dead_reason IS NULL`
  selectors exclude it for free, then `_auto_hide_dead_roles` retires the card. First real
  pass: **20 of 60 oldest tracked listings were already dead.**
- **`ListingObservation` is global and profile-independent** (precedent: `JobEmbedding`).
  Two reasons, and *not* de-duplication — all 9,998 identity hashes sat under exactly one
  profile, so that argument doesn't survive the data. (1) `_store_age_days` is per-profile
  and gates every observation rule, so a user signing up next month would silently be
  blind to the feature for 30 days with no error anywhere. (2) The crawl has no profile,
  and `JobSeen`'s non-nullable FK would force a sentinel row that pollutes every
  profile-scoped query.
- **`JobSeen.source_ref` / `full_auto._board_ref`** exist for observation CONTINUITY, not
  dedup. `identity_hash` is `sha1(_canonical_url(url))`, so a re-slug or an added tracking
  parameter mints a new identity and **restarts `first_seen` at zero** — silently
  corrupting the exact data the longitudinal rules need. Measured: 91.4% URL coverage, and
  two listings had already done this in a 12-day store (Reed job 57106990 appeared as both
  "lead-software-engineer" and "lead-oracle-applications-engineer"). Read the vendor before
  trusting it as a vacancy key: reed/adzuna ids identify a **listing** (a repost gets a new
  number), while a greenhouse `gh_jid` identifies the employer's own **requisition** and
  persists while the req is open — the closest thing here to ground truth.
  Never retrofit it into `identity_hash`: that re-keys the store and orphans every cached
  embedding, verdict and `gate_cache` entry.

**DAILY IS A FUNCTIONAL REQUIREMENT.** `EVERGREEN_SEEN_DENSITY` divides days-seen by
days-since-discovery, so a missed day inflates the denominator while the numerator stands
still — gaps don't delay the signal, they *suppress* it. There is still no in-process
scheduler (same reasoning as `crawl_direct_employers.py`). Watch
`distinct_observation_days` in `GET /admin/observe`: a crawl that has silently stopped
looks healthy in every other field.

**How it is actually scheduled in production: supercronic, in the Fly machine.** The
Dockerfile installs it (SHA1-pinned) and the CMD runs it alongside uvicorn; `crontab` at
the repo root is the schedule. Four things about that arrangement are load-bearing and
easy to undo by accident:
- **`exec uvicorn`, not plain `uvicorn`.** With two commands in the `sh -c` the shell no
  longer execs, so `sh` stays PID 1 — and a non-interactive shell installs no SIGTERM
  handler, which for PID 1 means the kernel discards the signal. Fly's shutdown would be
  ignored for the whole of `kill_timeout` and end in SIGKILL, tearing the machine down
  mid-write and unmounting `/data` dirty. That is precisely what `fly.toml`'s
  `kill_timeout = "25s"` exists to prevent, so this is the one-word difference between the
  setting working and being decorative.
- **Two crontab entries, never `&&`.** They are independent jobs and the recheck is the
  more valuable of the two (a live pass found 20 of the 60 oldest tracked listings already
  dead). Chained, a transient board-API failure in the observation pass — which exits
  non-zero on `ok: false` — silently skips the recheck for that day too.
- **`.gitattributes` pins `crontab`/`Dockerfile` to LF.** This machine has
  `core.autocrlf=true` and `fly deploy` builds from the WORKING TREE, so a checkout would
  otherwise hand supercronic `python scripts/observe_listings.py\r`, which fails with a
  "not found" naming a command that plainly exists — and fails silently, since supercronic
  itself keeps running.
- **`crontab` must be committed.** It is read from the image at `/app/crontab`, and
  supercronic exits immediately if the file is missing while the container stays healthy
  and the health check keeps passing. Nothing anywhere reports "cron is not running".

Concurrent SQLite access from the cron process and the API is safe because
`database.py::_sqlite_pragmas` puts every connection in WAL mode.

**Do NOT also schedule this from GitHub Actions.** A runner has no access to the Fly
volume the SQLite file lives on, so a workflow pointed at `scripts/observe_listings.py`
writes to an empty throwaway database at best. It also cannot import the app at all
unless it installs `backend/requirements.txt` (SQLAlchemy lives there, not in the root
`requirements.txt`).

> ⚠ **`JobSeen.repost_key` declares `index=True` and the index did not exist**, because
> `_migrate_columns` only does `ALTER TABLE ADD COLUMN` and `create_all` never revisits an
> existing table. `index=True` takes effect only on a database built from scratch *after*
> the column was declared — check this for any column added that way.
> `_migrate_ghost_indexes` now creates it.

### Application-outcome feedback (the only ground truth)

`Role.application_status` gained `offer` and `no_response`, plus `response_at` (stamped
once on the first non-`pending` transition, never overwritten — same invariant as
`dead_at`, because `applied_at → response_at` is the measurement). Logged to `EventLog`
as `application_outcome` so `/admin/analytics` can roll it up across users.

The field had **never once been used past `pending`** in its entire life. That was never a
control problem — nobody had a reason to navigate to `/my-roles` → Applied and report back.
So the prompt lives on **`/search`**, where every session starts, and `offer` exists because
a form whose only outcomes are negative is a form nobody fills in.

**Two caveats that must travel with this data.** `no_response` is a **biased** label for
ghosting — most applications get no reply for ordinary reasons — so it is only ever read as
a *rate across many rows conditioned on a fired signal*, never as proof about one listing;
and it is not final (the other controls stay live so a late reply can correct it). And at
~2 applies/month a usable base rate is years away: it is collected now because it cannot be
reconstructed later, and the rule thresholds stay hand-set and named.

### Ghost-listing evidence (the recording layer)

`JobSeen.seen_dates` / `dead_at` / `repost_key` are the **write-now-read-later** columns the
rules above consume. They predate the rules deliberately: a ghost listing can only be
identified from a history of observations, and **that history cannot be reconstructed after
the fact** — every week it isn't recorded is permanently lost, while the scoring and the UI
can be built whenever.

- `seen_dates` — the distinct UTC dates this identity has been observed, as
  days-since-epoch integers, comma-separated. `seen_days` is the *count* of exactly
  these and remains the fast path; this is the observation window's **shape**, which
  the count destroys. Text on the existing row rather than a sightings table on
  purpose: a row per observation is ~3,000 inserts per run (six runs a day are
  allowed) for data whose whole value is longitudinal. Written via `_append_sighting`,
  which is idempotent per day, so unlike the `seen_days` increment beside it (whose
  ORDER MATTERS trap is still there) it doesn't depend on being sequenced against
  `last_seen`. A gap means "not seen", never "not live" — same lower-bound caveat as
  `seen_days`.
- `dead_at` — when we first confirmed the listing gone, closing the bracket
  `first_seen` opens. `dead_reason` already recorded *that* it died; without a
  timestamp there was no way to ask how long any listing actually stayed up, which is
  the central question. Stamped once at every death-detection site
  (`_persist_dead_scrapes`, `_persist_enrich_dead`, and now `observe.recheck_liveness`),
  never overwritten. `_migrate_dead_at_backfill` repairs rows carrying a `dead_reason`
  with no `dead_at` — the live store had one, and `takedown_repost` keys on this column,
  so such a row would have been skipped forever.
- `repost_key` — normalised company+title via `_family_key`, so "the same vacancy
  re-advertised" means what it already means elsewhere in the module. **Not** a dedupe
  key: a repost is a distinct listing with its own dates, and the signal is precisely
  how many there are and how far apart. A live store already shows one recruiter's
  ".NET Developer" 34 times.

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

### Auth & self-serve sign-up

**Sign-up is self-serve and instant.** There is no approval step, no waiting list and no
manual credential hand-off: a visitor signs in on the homepage, answers two questions, and
is in the product. That is a product decision the code is built around — nothing in the
auth path is allowed to gate a new account on an owner action.

**THREE self-serve paths, offered side by side on the homepage** — Google, Apple, and
email+password — plus the legacy hand-assigned credentials behind the footer link. Google
alone silently lost everyone who doesn't have (or won't use) a Google account, and lost
them at the *first* screen, so they never reached a page that could count them: the cost
of that is unmeasurable by construction, which is itself the argument for not running it.
`GET /admin/signups` now reports `by_method`, which is the only way that decision ever
gets checked.

All three create the account on first sight and mint the same Bearer token every other
route already expects, so authentication still changes at exactly one seam and
`deps.current_user_id()` is still the single chokepoint. All three responses go through
`auth_router._login_out` — shared so a field added to `LoginOut` cannot be wired to two
paths and missed on the third, which for `needs_survey` would mean a whole provider's
users skipping the survey gate with nothing anywhere reporting it.

For the two PROVIDERS, sign-up and sign-in are **the same call**: the browser can't know
which it is, and making the user pick the right button only ever produces a wrong answer
and a confusing error. `is_new` in the response tells the frontend which happened. For
**email that is inverted** — `/auth/email/register` and `/auth/email/login` are separate,
because the user knows perfectly well whether they have registered before, and merging
them would let a mistyped password on an existing account silently create a SECOND,
empty account whose owner then finds a blank profile and no explanation.

Verification (`services/auth.py::verify_google_id_token`) delegates signature/issuer/
expiry to Google's own `tokeninfo` endpoint rather than validating RS256 against their
JWKS locally. That is one outbound call per sign-in, which is the wrong trade on a hot
path and the right one here — sign-in happens roughly once per user per month
(`TOKEN_MAX_AGE_SECONDS`), and local verification means owning JWKS fetching, key
rotation and caching: three places to get subtly wrong in the one part of the app where a
subtle mistake is an authentication *bypass*. **Two things tokeninfo does not check and
this function therefore must**, both load-bearing:
- **`aud` must equal `GOOGLE_CLIENT_ID`.** tokeninfo will happily validate a token Google
  minted for a *different application*. Without this comparison, an ID token obtained from
  any other Google-integrated site would authenticate here. This is the single most
  important line in the auth path.
- **`email_verified`.** An unverified address must not be stored as if confirmed — it is
  shown in `GET /admin/signups` as a contact address for someone who never proved they own
  it. An unverified token still authenticates (the `sub` is real); the email is dropped.

An unset `GOOGLE_CLIENT_ID` **disables** Google sign-in (503) rather than falling back
to an unverified path, and `GET /auth/config` reports that so the frontend renders a "sign-in
unavailable" note instead of a button that 503s. The frontend reads the client id from that
endpoint rather than from `NEXT_PUBLIC_GOOGLE_CLIENT_ID`: both are public, but the env var
is baked in at BUILD time while the backend reads it at RUN time, so a frontend built
before the credential existed would otherwise render a permanently broken button against a
perfectly configured server. The env var stays as a fallback for an unreachable API. The
same run-time-not-build-time rule applies to `APPLE_CLIENT_ID`, and to whether the email
form renders at all.

**Apple** (`POST /auth/apple`, `services/auth.py::verify_apple_id_token`) differs from
Google in four ways that all follow from Apple giving less:
- **Verification is LOCAL**, because Apple publishes no tokeninfo equivalent. PyJWT
  (`pyjwt[crypto]`, added to `backend/requirements.txt`) does RS256 against Apple's JWKS
  plus `exp`, and — the load-bearing part — `audience=APPLE_CLIENT_ID` and
  `issuer=https://appleid.apple.com` as *arguments*, not as post-hoc comparisons. The
  `aud` pin is the same single most important line as on the Google path: Apple will sign
  a valid token for any Services ID. Unit-checked against forged tokens (wrong aud, wrong
  iss, expired, `alg=none`) — all rejected.
- **`PyJWKClient` is created once and reused**, because it caches Apple's signing keys. A
  fresh client per sign-in would fetch the JWKS every time.
- **The browser flow is the POPUP** (`usePopup: true`), not Apple's default redirect. The
  redirect flow POSTs a form back to `redirectURI`, which would mean a second, quite
  different session mechanism (cookie or URL fragment) beside the Bearer token everything
  else uses, plus a full page reload mid-signup. The popup hands the page a signed
  `id_token` in a promise — byte-for-byte the shape the Google button already produces —
  so it joins at exactly the same seam. `redirectURI` is still required by Apple even in
  popup mode and must be a registered Return URL on the Services ID.
- **`APPLE_CLIENT_ID` is the Services ID, not the App ID**, and there is **no client
  secret and no .p8 key**: those are only needed for the server-side authorization-code
  exchange, which this does not do.
Two Apple facts callers must handle rather than assume away: `email` is frequently Apple's
private relay address (real and deliverable — not a placeholder), and **there is no name
claim, ever**. Apple returns the name once, in the authorization RESPONSE on first sign-up
only, outside the token; it rides in on the request body, is written only when creating
the account, and is display-only — identity is the verified `sub` alone.

**`User.google_sub` / `User.apple_sub` are the join keys, never `email`.** A Google
Workspace address can be reassigned to a different person after an employee leaves; keying
on it would hand the new holder the old holder's job search. Email is stored for display
and the admin list only. Both columns carry UNIQUE indexes created by
`database._migrate_signup_indexes` — not by `index=True` on the model, which
`ALTER TABLE ADD COLUMN` cannot apply to an existing table (the same trap the
`jobs_seen.repost_key` note documents). Two rows for one provider account would silently
split a person's profiles and history in two, so those indexes are an invariant, not an
optimisation.

**Accounts are NEVER linked across providers**, and that is a security decision, not a
missing feature. Someone who registers with a password and later signs in with Google gets
two separate accounts. Linking them on email would mean an *unverified* registration for
`victim@example.com` capturing the account the victim later reaches through their verified
Google or Apple identity — an account takeover with a signup form as the only tool
required. What stops the two being created in the first place is registration returning
**409 on an email already in use by any account**. That does let someone probe whether an
address is registered; that is an accepted and near-universal property of signup forms, and
a far smaller problem than the one it prevents.

**`User.auth_provider` (`"google" | "apple" | "email" | NULL`) exists because an email
account and a legacy account are otherwise byte-identical in shape** — both have a real
password hash and no subject id. `_needs_survey` used to key on `google_sub`, which was
equivalent only while Google was the sole self-serve path; keyed that way, every email
signup would silently inherit the legacy survey exemption. NULL still means legacy, which
`ALTER TABLE ADD COLUMN` gives every pre-existing row for free.
`database._migrate_auth_provider` backfills `"google"` from a non-null `google_sub` — it
cannot guess wrong, since only the Google path has ever written that column — and leaves
everything else NULL. Do **not** extend it to stamp a provider on NULL rows: NULL is also
what `beta_started_at` uses to exempt the original cohort from the 7-day window, and the
two exemptions travel together.

`PASSWORD_MIN_LENGTH` (8)
is the only password rule — composition rules measurably push people toward shorter, more
guessable passwords, and this account holds a CV and a list of job adverts, not a payment
method. There *is* an upper bound (1024 chars), which is not cosmetic: pbkdf2 hashes
whatever it is handed, so an unbounded password field is a free CPU-exhaustion lever
against an unauthenticated endpoint. It is enforced by `auth.validate_password`, which is
split out from `validate_email_and_password` **so the reset route enforces the identical
rule** — two copies of a password policy is how a reset endpoint quietly ends up accepting
a 3-character password. Set `EMAIL_SIGNUP_ENABLED=0` to hide the form and 503 the
endpoints.

### Transactional email (Resend) — verification and password reset

Email+password used to have no verification email and no reset, because the deployment had
no mail service. Both now exist, through **Resend** (`services/mailer.py`, `RESEND_API_KEY`).
The reset is the more important half: without it a forgotten password was **permanent
lockout with no recovery path in the product at all**, which is the concrete sense in which
the email path was not really a peer of the two provider buttons.

**Two messages exist and there will never be a third without a reason written down**:
confirm-your-address and reset-your-password. Each is the direct consequence of an action
the recipient took seconds earlier, which is what keeps this out of consent/unsubscribe
territory — there is no list and nothing to opt out of.

**Sending can never fail a request.** Every mailer entry point returns a bool and swallows
its own errors, and every send goes through `BackgroundTasks`. The endpoints that send are
the endpoints that create accounts and accept sign-ins, and the documented invariant is
that nothing in the auth path blocks a new user — so a Resend outage must degrade to "no
email arrived", never to "sign-up is down". **An unset `RESEND_API_KEY` disables sending
and prints the link to the console** rather than erroring, so a dev box with no Resend
account still has a completely working sign-up flow.

**`EMAIL_VERIFICATION_REQUIRED` defaults OFF**, and that is the same invariant again: a
hard gate makes an undelivered email (wrong address, spam folder, unverified sending
domain, Resend outage) indistinguishable from a broken account. Unverified users are signed
in and nagged by `VerifyEmailBanner` until they click. Flip it to 1 only once deliverability
has been *observed* — `GET /admin/analytics`' `verifications` counter against email signups
is the only end-to-end evidence that mail is actually landing, since Resend accepting a send
says nothing about it reaching an inbox.

**The tokens are stateless — no issued-links table** — because each carries what makes it
self-invalidating:
- **verification** carries the address it was issued for, so it cannot confirm an account
  whose email later differs, and re-clicking is idempotent (people forward these to
  themselves and mail clients pre-fetch them; "already used" reads as breakage).
- **reset** carries a **fingerprint of the password it was minted against**, so completing
  a reset changes the fingerprint and every outstanding link for that account dies at the
  same moment. Single-use falls out of the construction rather than out of a `used_at`
  column somebody has to remember to check, and "I reset my password, now revoke the
  emails" is handled for free. The cost, which is the correct trade for someone resetting
  *because* they think a stranger has their password: a reset cannot be undone by re-using
  the previous link.

Distinct itsdangerous `salt=` values per purpose are what stop one being replayed as the
other — a verification link accepted as a password reset would be full account takeover via
an old email. Checked by test: both directions are rejected.

**Both links SIGN THE USER IN** when consumed (`/verify-email`, `/reset-password` return a
full `LoginOut`). Mail is usually opened on a different device from the one the account was
created on, so ending at "confirmed — now go and sign in" strands exactly the people who
clicked. The link is proof of mailbox control, which is what every password-reset flow
already treats as sufficient to take over an account, so this grants a link-holder nothing
new. A successful reset also sets `email_verified` for the same reason.

**`POST /auth/password/forgot` always answers identically**, registered or not, sent or not
— it is not permitted to become a membership oracle. (The registration 409 leaks the same
fact and is unavoidable, since a signup form must refuse a duplicate somehow; this route has
no such constraint, so it leaks nothing.) The frontend must keep saying *"if there's an
account for that address"*; a UI that says "sent!" re-introduces the oracle the backend went
to trouble to avoid. It silently does nothing for a Google/Apple account — granting one a
password would create a second credential for an identity meant to have exactly one, i.e.
the cross-provider linking the `User` model refuses on takeover grounds.

Relatedly, the unverified check in `email_login` runs **after** the password check, never
before, or the endpoint would tell an anonymous caller "that address exists but isn't
confirmed" for any address they typed.

**Rate limiting is TWO limits, not one, and this was a real bug caught in testing.**
`EMAIL_SEND_MAX_PER_HOUR` (5) is per ADDRESS — nobody legitimately needs six reset links to
one mailbox in an hour. `EMAIL_SEND_MAX_PER_HOUR_PER_CLIENT` (30) is per IP and must stay
far looser, because **an IP is not a person**: a shared office connection or a mobile
carrier's CGNAT puts many unrelated users behind one address, and using the tight number
there refuses the fifth person on that network to forget their password because of four
strangers. The limiter is consumed *before* any DB lookup, so a throttled caller and an
unregistered address stay indistinguishable.

**`_post` uses httpx, not the `resend` SDK.** httpx is already a pinned direct dependency
precisely so the auth path doesn't inherit another package's dependency tree; `POST
https://api.resend.com/emails` with a Bearer key is the whole API surface used here, and an
SDK for one JSON POST would add a supply-chain surface to the one part of the app where a
surprise is an authentication problem. Swapping to `import resend` later is confined to
that one function.

**`AUTH_SECRET` now signs reset links too.** On the dev default it is regenerated per
process, so every outstanding link dies on restart — harmless locally, a stream of "this
link is invalid" reports in production. It was already required to be stable; it is now
required for a second reason.

`EMAIL_FROM` must be on a domain verified in the Resend dashboard. The default
(`onboarding@resend.dev`) is Resend's shared test sender and is deliverable **only to the
address that owns the Resend account** — right for the operator's own testing, silently
wrong for everyone else, which is why it is named in the deployment checklist rather than
assumed. `APP_BASE_URL` is the FRONTEND origin the links point at, defaulting to the first
`FRONTEND_ORIGINS` entry; set it explicitly when several origins are allowed, because the
first one wins and a link to the wrong one is a dead link in someone's inbox.

`POST /login` is scoped to **`auth_provider IS NULL`**. An email signup does have a real
password hash and its username is derived from the email's local part, so without that
scoping `jay@example.com` could also sign in there as `jay`. Same credential, so not a
weakening — but it would make `/login` a quiet second front door to a path that has its
own endpoint, and any rate-limiting, lockout or audit added to one would then silently not
cover the other.

**Legacy hand-assigned credentials still work** (`scripts/gen_beta_users.py`, `POST /login`,
the `/login` page) purely so the first beta cohort isn't locked out of their own profiles
and saved roles. Not linked except from the homepage footer. A Google account can never be
reached through `/login` whatever password is sent: those rows store an **empty**
`password_hash`, and a pbkdf2 hex digest is always 64 chars, so `verify_password` can never
match it. `password_hash`/`salt` stay NOT NULL rather than becoming nullable because SQLite
cannot drop a NOT NULL constraint with ADD COLUMN — the empty-hash sentinel is both
migration-free and safer than a NULL some future comparison might read as "no password
required".

**The two sign-up questions** (`SignupSurvey`: Q1 `priority`, single select from
`config.SIGNUP_PRIORITY_CHOICES`; Q2 `used_ai_tool`, yes/no) are asked on `/welcome`,
AFTER the account exists — questions on the sign-up form are friction at the exact moment
there is least patience for it, and they'd be asked of people who never finish. Answers are
stored as the raw option slug so re-wording a label later can't invalidate answers already
collected; only the frontend holds the labels.

**The survey gate is enforced in the CLIENT, deliberately** (`app/providers.tsx`). A
backend that 403'd every request until the survey was answered would also reject the survey
submission itself, and would surface as an auth failure that `api.ts`'s 401 handling reads
as a logged-out session — turning two optional-in-spirit questions into a lockout. So the
server reports `needs_survey` on every sign-in route and on `/me`, the gate routes on it,
and a user with devtools can skip it. `GET /admin/signups` reports `answered: false` rows
rather than filtering them out precisely so skipping — or a broken gate — stays visible; a
filtered list would make a broken gate look like low sign-up volume. `needsSurvey()` in
`lib/auth.ts` is a localStorage CACHE of the server's answer, refreshed from `/me` once per
mount, or a second device would silently skip the survey forever.

Every SELF-SERVE account is asked (all three providers — see `auth_provider` above), and
only those. A legacy beta account predates the survey and its holder has usually already
answered by other means; asking on next login would read as the app breaking, not as
onboarding. `/admin/signups`' `survey_outstanding` is computed over the same set for the
same reason — it used to be `method == "google"`, which after this would have silently
ignored Apple and email signups, making a broken gate on those paths look like nobody
using them.

`GET /admin/signups` (ADMIN_TOKEN header, same guard as `/admin/analytics`) is the **only**
place anyone learns who has arrived — with no approval step in the flow, nothing else
reports it. `signup` and `signup_survey` are also `EventLog` types, counted separately from
`login` in `/admin/analytics` so "how many new people" needs no subtraction of one series
from another.

**Deployment checklist — Google** (all three, or Google sign-in is dead in prod):
`GOOGLE_CLIENT_ID` set on the backend host; the site's origin added to *Authorised
JavaScript origins* on that OAuth client in Google Cloud Console; the frontend's origin in
`FRONTEND_ORIGINS`. There is no client *secret* anywhere — GSI hands the browser a signed
ID token directly and the backend only verifies it, so no auth-code exchange and no
confidential credential is involved.

**Deployment checklist — Apple.** Everything here is on Apple's side and needs a paid
Apple Developer Program membership; until it exists the button simply doesn't render (the
component returns null on `apple_enabled: false`, deliberately, rather than showing a
"temporarily unavailable" note beside two working alternatives). (1) An App ID. (2) A
**Services ID** with Sign in with Apple enabled — its identifier is `APPLE_CLIENT_ID`.
(3) The site's domain registered on that Services ID **and the origin added as a Return
URL**, or Apple rejects the popup before it opens. Again no client secret and no .p8 key,
for the reason in the Apple bullet above.

**Deployment checklist — email sign-up itself.** Nothing. It is on by default
(`EMAIL_SIGNUP_ENABLED`), which is the point: it is the path that needs no third-party
account, so requiring an env var to enable it would leave the default deployment offering
exactly the two providers it exists to supplement.

**Deployment checklist — Resend (the mail behind it).** The sign-up path works without any
of this; what stops working is verification and password reset (links get printed to the
server console instead of sent). (1) A Resend account and `RESEND_API_KEY`. (2) **A verified
sending domain**, added under Resend → Domains with its DKIM/SPF DNS records published —
this is the step that actually takes time, and until it is done `EMAIL_FROM` can only be
`onboarding@resend.dev`, which Resend delivers **only to the address that owns the account**.
Every other recipient is rejected with a 403 that `mailer._post` logs verbatim. (3)
`EMAIL_FROM` on that domain. (4) `APP_BASE_URL` set to the real site origin if
`FRONTEND_ORIGINS` lists more than one. (5) A stable `AUTH_SECRET` — it signs reset links,
so a per-restart value invalidates every link in flight. Optional: `EMAIL_REPLY_TO` (a real
inbox, since the From address usually isn't one), and `EMAIL_VERIFICATION_REQUIRED=1` once
the `verifications` counter shows mail is landing.

### The open beta: a fixed 7-day window

`User.beta_started_at` is day 0. **Everything else is derived from it at read time**
(`services/beta.py`) — no expiry is ever stored, so changing the window length in config
applies immediately to everyone already inside it.

**NULL means NO WINDOW: never expires, never asked the wrap-up survey.** That is the
legacy exemption and it is free — `ALTER TABLE ADD COLUMN` gives every pre-existing
account NULL, so the original testers were untouched by the switch with no backfill
(verified: 51 accounts, all NULL). `beta_started_at` is stamped in exactly one place, the
`is_new` branch of `POST /auth/google`. Do not "fix" `_migrate_columns` to stamp a date —
that starts a 7-day clock on the existing cohort.

**Two gates off that one clock, and they are independent.** Collapsing them re-creates
the problem the split exists to solve:

* **The wrap-up survey gate** — `EXIT_SURVEY_AFTER_DAYS` (4). From day 4, the next time
  the user loads the app they are held on `/exit-survey` until they answer, then released
  back into the app for their remaining days. Client-side, exactly like the sign-up
  survey. Triggering on day 4 rather than day 7 is the whole point: **day 7 would require
  the user to log in on one specific day**, which is the single most likely way to collect
  nothing at all.
* **The lapse gate** — `BETA_WINDOW_DAYS` (7). Server-side, a real 403 from
  `require_active_beta`, on the `_auth` dependency list in `main.py` covering profiles,
  attributes, families, onboarding, search and settings.

`needs_exit_survey` deliberately does **not** consult `expired`. A user who never logs in
between day 4 and day 7 hits both gates at once and must still get the survey — that is
precisely the person the day-4 trigger exists to catch. `/exit-survey` therefore renders
three states off `/me`: unanswered (→ back to `/start`), answered+expired (the "access has
ended" screen), unanswered+expired (the form, then that screen).

**Three things must stay reachable when expired**, or the survey becomes unanswerable by
the group it exists to ask: the `auth_router` (public + self-guarding — `/me`,
`/signup/survey`, `/exit/survey`), the `feedback` router (authed but not beta-gated, so an
answer already typed is never lost to a window that lapsed mid-interaction), and `admin`.
The survey gate is client-side for the reason the `auth_router` docstring already gives: a
server that 403s until a survey is answered also 403s the submission, and surfaces as an
auth failure the frontend reads as a logged-out session.

`require_active_beta` raises a **dict** detail (`{"code": "beta_expired", "message": ...}`),
not a string. `api.ts` needs to tell it from an ordinary 403 to route to the survey instead
of showing a raw error, and a code in the body avoids a custom response header (which would
also need adding to the CORS `expose_headers`). `handleBetaExpired` **must not**
`clearAuth()` — the user has to stay signed in to answer — and latches, because several
queries can 403 before the navigation lands and `location.pathname` is still the old route
for all of them.

`POST /admin/users/{id}/beta` (`extend`/`restart`/`clear`) is the escape hatch and is
**required**, not a nicety: lapsing is a real 403 across every data router, so without it
there is no way to give a tester more time or reopen an account to chase a bug they
reported. `clear` sets NULL, i.e. makes them exempt like the original cohort.

### Beta feedback: one store, three surfaces

`FeedbackResponse` (`feedback_responses`) holds every answer from all three surfaces —
sign-up, the two in-run prompts, and the wrap-up survey — as
`user_id, profile_id, run_id, surface, question_id, answer, created_at`. One store so the
admin readout is one query and can filter by question or by user with no new tooling; a
table per surface means a new table, endpoint and report per question added.

`question_id` is a stable slug and `answer` is the raw value (JSON-encoded for
multi-select), never a normalised enum — re-wording a question must not invalidate answers
already collected. Labels live in the frontend, same contract as
`SIGNUP_PRIORITY_CHOICES`. Written through `analytics.record_feedback`, which sits beside
`log_event` but **does** raise where `log_event` swallows: a dropped analytics row is
invisible, a dropped answer shows the user a thank-you for something that did not happen.

**Sign-up answers are dual-written.** `SignupSurvey` stays the source of truth (it *is* the
survey gate, and `/admin/signups` reads it); `POST /signup/survey` also upserts the two
mirrored rows, and `_migrate_signup_feedback_backfill` mirrors rows collected before the
store existed.

The two in-run prompts (`routers/feedback.py`):

* **`results_quality`** — fired by the first cross or apply on a run, and rendered **in
  place of** the "got a bug or an idea" box in `SearchFeedbackBox`; two feedback asks
  stacked is how both get ignored. Held back until the user's second completed run
  (`RESULTS_PROMPT_MIN_RUNS`) so their first search is not interrupted. Scoped per run, so
  a later run can legitimately ask again.
* **`setup_ok`** — fired when a CV/notes parse *succeeds*, shown beside the run button at
  the bottom of `/onboarding`. Scoped per user, asked once ever; `run_id` is NULL because
  no run exists yet.

**`GET /feedback/due` exists so the trigger rules live server-side.** The client knows a
cross happened but cannot know the user already answered on another device, and a prompt
that reappears after you have answered it reads as the app losing your input.

Both prompts keep the answer and the free-text detail as **two rows** (`<id>` and
`<id>_detail`), so a No with no typed detail still leaves a usable signal instead of an
abandoned prompt recording nothing.

Wrap-up Q2 (`EXIT_SURVEY_FEATURE_CHOICES`) carries a `"none"` option that is **not**
padding: with a plain checkbox group, zero ticks cannot be told from "hasn't answered", and
the gate reads presence-of-any-row to decide when to release the user. Q1 (free text) is
deliberately optional — a required free-text box on a blocking page is where people bail or
type "n/a", and an answer nobody means looks like signal in the readout.

### Reading the beta back: `scripts/admin_fetch.py`

`GET /admin/analytics` gained `runs`, `run_stats` and `feedback`; `/admin/signups` gained
the per-account window fields. `scripts/admin_fetch.py` is the CLI over both — before it,
the only documented way in was a hand-written `curl` returning several hundred lines of
JSON.

**Roles found per run** is reported as a distribution *and* an average, because "runs
average 7" and "one run found 12 and three found 1" are very different situations an
average cannot separate. It reads `SearchRun.result_count`, which the engine already writes
— do not count `Role` rows instead, they disagree in both directions (picks already saved
in an earlier run are counted but not re-persisted; retained quick-scored leftovers are
persisted but not counted). **Status is reported alongside it and must stay that way**:
`result_count` is only written on the `done` path, so a cancelled or errored run reads 0
and would otherwise look like a search that found nothing. `run_stats` averages `done` runs
only, and breaks out `runs_done_with_zero` — a run that completed normally and still
surfaced nothing is the failure worth seeing on its own.

`_EVENT_FIELDS` is now the single source for the per-user counters (`_COUNTER_FIELDS` is
derived from it), so adding an event type is one edit rather than three.

The script reconfigures stdout to UTF-8. It prints text people typed, which routinely
contains curly quotes and em dashes, and a Windows console defaults to cp1252 — the same
failure `full_auto.emit()` guards against, hitting exactly when someone has finally left
useful feedback.

### The public homepage

`frontend/app/page.tsx` is the landing page, the sign-up form and the app's entry point,
all one route. It replaced both the old `/login` screen and a **separate static
`index.html` that lived outside this repo** (a bundled artifact in the `4-in-1000` docs
folder, deployed to Netlify, posting emails to a Google Apps Script sheet). Merging them
was the point: marketing copy and the sign-up form are the same set of pages, so keeping
them in two codebases meant every copy change had to be made twice with a manual
access-granting step in between.

- **Logged out** → the landing page. **Logged in** → `/start`, which is the old `/`
  first-run router (no profile data → `/onboarding`, else `/search`). That logic had to
  move to its own route because `/` is now rendered OUTSIDE `ProfileProvider` (see
  `PUBLIC_ROUTES` in `providers.tsx`) and it needs `useProfiles`/`useAttributes`.
- The hero's **demo card is built from the app's own `.card`/`.tag`/`.verdict`/`.an-h`
  rules**, not bespoke landing markup, so the page cannot drift from the product it is
  advertising. Its buttons are `<span>`s (`.lp-static`) — a dead button that looks live is
  worse than an obviously static one.
- Landing/`/welcome`/`/login` styles are all `lp-`-prefixed in `globals.css` and use the
  app's existing tokens (one terracotta accent, same warm neutrals). They run wider and
  looser than the app, which is a dense 900px working surface.
- Copy carried over from the old page because it tested well: the **"We tested the leading
  matcher"** receipt and the four **"Why the tools you've tried don't solve this"** points.
  Deliberately gone: the "UK jobseekers · early access" pill and the "Built for UK
  jobseekers" footer — the tool is UK-optimised but saying so up front narrows the audience
  for no gain, and the waiting-list framing is simply no longer true.
- The `#join` box holds all three sign-up controls. `AppleSignInButton` and
  `EmailSignUpForm` each render as **nothing** when the server reports that method
  unconfigured, so the block degrades to exactly what it was before them. The email form
  **used to be collapsed behind a small link** on the theory that a form is more attention
  than a one-click button, so leading with it would make the fast paths look like the
  fallback. The theory was fine and the presentation still argued the opposite of what it
  should: this is the only path available to *everyone* (no third-party account required)
  and it was drawn as the least of three. It is now always visible, separated from the two
  buttons by an `.lp-or` divider — "or" rather than a bare rule, because a rule alone reads
  as a section break and invites what follows to be read as fine print. The Apple button is
  styled black-on-white, not Apple's black fill: this page's only strong colour is the
  terracotta accent, and a solid black button beside Google's outlined one reads as the
  primary action, which it isn't. Both are sized 320x44 to match what GSI renders at
  `size: large`, so the three stack as one column.

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
add/edit/delete building blocks for every attribute type; `LocationPicker` is a dedicated
component for the location/work-type/scope/country group specifically (not routed
through `AttributeRow` — see "Location scope & country filtering" above); `RoleCard` is
the shared result card across `/search` and `/my-roles`. TanStack Query handles server
state, polling, and cache invalidation on tick/cross.

**`PreferencesPanel` is the whole Preferences block, shared by `/dashboard` and
`/onboarding`.** Onboarding used to carry three of the seven controls (seniority, salary,
location) and `/dashboard` all seven, so **work style, maximum listing age, "roles below
your level" and visa sponsorship were invisible to a first-time user** — i.e. the settings
most able to make a first search return the wrong thing were exactly the ones nobody was
shown until they later wandered onto `/dashboard`. Onboarding also used a different
layout (`.row.pref`) for the three it did have. Extracting the block is what makes
"onboarding matches the profile page" structurally true rather than two lists that agree
until the next edit; it reads its own attributes via `useAttributes` rather than taking
props, since TanStack dedupes the query and both call sites already hold it cached.

**`/search`'s "Showing N results" counts THIS RUN's ranked picks, with everything else
counted separately.** One combined number conflated two different things, and the gap is
large enough to read as a bug: a live run reported "Showing 21 results" over 12 ranked
picks + 4 still-`new` rows from an earlier run + 2 already-saved + 3 quick-scored-only.
Nothing was miscounted — all 21 render, each under its own labelled section — but the
headline claimed the search had found 21 roles when it had found 12. It now reads
"Showing 12 results from this search · 9 more below". Both numbers are still summed from
the exact buckets rendered below, never derived by subtraction, so neither can drift from
what is on screen — the invariant the older note in that file already protects. **Any new
section must be added to one of the two sums.**
