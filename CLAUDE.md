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
  otherwise hard-deleted; crossed → soft-deleted (keeps the FeedbackLog referent).
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
- **`CompanyATS`**: cached vendor/token registry for the ATS discovery tier (Greenhouse,
  Lever, Ashby, Workable, Recruitee, Personio), populated by `seed_ats.py` (curated,
  live-validated) and grown by `harvest.py`'s occasional `site:`-search harvest — this
  list itself is cached fine; it's the *job data fetched from* these companies each run
  that needed a TTL guard (see below).
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
   phase). Reed/Adzuna page depth is capped by `REED_PAGES_PER_TERM`/
   `ADZUNA_PAGES_PER_TERM` (env, default 1 — still 100/50 results per term; the
   fetchers' own `pages=3` defaults are the legacy path's behavior): a measured live run
   discovered 7,800 raw listings of which only ~100 were ever examined past the
   embedding stage, so pages 2–3 were pure latency. The fetchers emit a
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
   to its single best-scoring cluster → free heuristic prescreen (`_heuristic_prescreen`:
   title-regex drops obvious seniority mismatches — Director/VP for a junior, Intern for
   a senior — before any LLM spends a token) → adaptive strict/broadened pool per cluster
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
   (one soft-axis failure), and the judge only deprioritises. The mechanical filter is
   `rank_gate`'s ≤15 cap landing under `RANK_REJECT_SCORE_FLOOR` (50) — the same way the
   salary floor, also a Soft-by-default type, is enforced (rule e). Flipping Dashboard →
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
   `engine.py::_needs_full_scrape`) → **Phase 6 final LLM evaluation runs once per
   cluster**, each a **single** expensive call (`full_auto.final_evaluation_split`)
   returning a strict `strong` list, a lenient disqualifier-only `backup` list, and
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
   per job. **Any edit to these prompts must bump `FINAL_EVAL_PROMPT_VERSION`** (now 23)
   or every already-persisted verdict is served stale forever.
   **`fit_level` is derived mechanically from the model's own step-D requirements
   checklist and `concerns`, not from an overall impression, and is decoupled from which
   list the pick landed in** (v16). A live run graded 9 of 11 picks `strong` and 2
   `very_strong`, with zero `ok`/`stretch` — no discrimination at all — because nothing
   in the prompt tied the grade to anything: the model listed a decisive concern ("no
   hands-on exposure to PLCs, industrial control systems, robotics") and still returned
   `strong`. Two rules fix it. The **rubric** (in `_FINAL_EVAL_SCHEMA`) sets `very_strong`
   = all core requirements met + `sector_match` + no concern touching a core requirement,
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
   rubric thresholds. v18 adds the two remaining halves of that discipline. (a) Step D's
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
   false `sector_match` trade-off (step G) and a want-fit mismatch (step B).
   v23 also closed a **nice-to-have vocabulary hole**: "ideally", "preferably",
   "desirable", "a plus", "a bonus", "an advantage", "welcome", "would be great" were
   absent from QUOTE-THEN-CLASSIFY's SOFT list and from step D's rule (b), so
   "ideally Databricks or Snowflake" could be read as a bar — including a list of named
   tools introduced by one of those words, which is the form that misled. And the
   `not_selected` reason field carried none of the discipline steps C/D impose on
   `concerns`: it is now explicitly held to the same bar, and may never cite a
   nice-to-have or a trained-for ask as the reason a role was passed over ("out-competed"
   is the honest answer there).
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
   *Following* the tracking redirect was tried first and doesn't work — the land URL
   403s a plain HTTP client and is bot-walled behind the browser too. Pre-gate
   browser-scraping was considered and rejected separately: ~4-5s/page would add
   minutes per run.

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
- `RANK_EXAMINE_BUDGET` (240, later **320**) / `RANK_TARGET_POOL` (80) / `JUDGE_POOL` (40) /
  `RANK_REJECT_SCORE_FLOOR` (50), all `engine.py` — the four numbers that set what the
  cheap+mid stages cost and what the judge gets to choose from, see pipeline step 2.
  `RANK_EXAMINE_BUDGET` is the honest cost dial: it is roughly a 3x increase on what the
  old per-cluster caps summed to, and screen (CHEAP) + rank (MID) calls scale directly
  with it. Raised 240 → 320 after a live single-cluster run stopped on "absolute pool
  cap" at 240 examined / 69 gated / 9 judged against a 760-deep queue — the ceiling
  itself, not `MIN_RESULTS`/`JUDGE_POOL`/a thin queue, was the limiting factor.
  `full_auto.rank_gate`'s fail-open path (its `llm()` call erroring,
  e.g. an intermittent permission/rate error on `MID_MODEL`) retries once on the same
  model after a short backoff, then falls back to `CHEAP_MODEL`, before giving up; a
  job that still has no real score after all of that is tagged `_rank_gate_failed` and
  bypasses `RANK_REJECT_SCORE_FLOOR` entirely in `engine._gate_rank_refill_cluster`
  rather than being compared against it — the fallback neutral score (50) is now exactly
  AT the floor rather than below it, but the bypass stays: it must not depend on those
  two numbers happening to coincide.
- `GATE_FIRST_ROUND` (20) / `GATE_ROUND_SIZE` (80) / `full_auto._GATE_MAX_WORKERS` (4) —
  the latency side of that budget. Round COUNT, not batch size, is what costs wall time:
  a round is a blocking `screen_gate` call then a blocking `rank_gate` call, each of
  which fans its own `_GATE_BATCH`(20)-sized sub-calls over a `_GATE_MAX_WORKERS` pool.
  At 80/round an 80-candidate round is exactly 4 sub-calls in ONE wave, so tripling the
  intake costs roughly one extra round rather than three. **`GATE_FIRST_ROUND` stays
  small on purpose and should not be raised**: `report()` only fires once a whole round
  has gated *and* ranked, so that first round alone sets time-to-first-"Verifying…"-card.
  For the same reason `REED_ENRICH_PRE_GATE_CAP` (100, run-wide) caps the pre-gate Reed
  enrichment rather than letting it follow the examine budget out to 320 — it is
  blocking main-thread HTTP sitting directly in front of first paint (a live run
  enriched 45 in 4.1s), so it is sized to cover roughly the first two rounds and the
  deep tail rides its teaser. `_GATE_MAX_WORKERS` is the knob to turn back down if
  429s/401s appear.
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
add/edit/delete building blocks for every attribute type; `LocationPicker` is a dedicated
component for the location/work-type/scope/country group specifically (not routed
through `AttributeRow` — see "Location scope & country filtering" above); `RoleCard` is
the shared result card across `/search` and `/my-roles`. TanStack Query handles server
state, polling, and cache invalidation on tick/cross.
