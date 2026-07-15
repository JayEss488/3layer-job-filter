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
  country, location_scope, custom, avoid, must_have) is one typed row with a `weight` and
  a `confirmed`
  flag, not a blob — see `config.ATTRIBUTE_TYPES`/`ATTRIBUTE_DIRECTION`. `target_role`
  (what they want) is deliberately kept separate from `past_role` (what they've done) so
  the engine can weight them differently. `avoid`/`must_have` are the candidate's own
  hard filters (auto-filled from the CV by `parsing.py`'s `_parse_prompt`, editable as
  chips below target roles): unlike the soft, LLM-derived `requirements` list
  (`profile_intel.py`), they drive an *unconditional* drop at the cheap gate
  (`hard_gate_ok`) and a DISQUALIFIER rule at the final judge — see the pipeline section.
  **Some attribute types are deliberately UI-hidden but still live**: `skill`,
  `sector_target`, and `custom` no longer render on the dashboard/onboarding pages (the
  profile shifted from "match on paper" to "the candidate wants this"), but they still
  auto-fill from the CV and still feed the engine — `skill` the evidence tiers and CV
  text, `sector_target` one of only two embedding pre-filter signals
  (`snapshot._BASE_EMPHASIS`), `custom` the "Constraints" CV line. Don't delete them as
  dead code because no UI references them.
- **`Role`**: one row per listing surfaced to the user in a given search run, with a
  lifecycle `status` (`new → saved/crossed/ignored → applied → deleted`) driving both the
  UI tabs and re-search semantics (only `crossed` roles are pruned before each new run,
  resetting the "passed this session" list; `new`/inbox roles persist indefinitely until
  the user acts on them, and saved/applied are never touched). `search_run_id` (FK to
  `SearchRun`, nullable — old rows predate the column) records which run produced a row.
  The `/search` page uses it to bucket a still-`new` role left over from an earlier run
  into its own "from earlier searches" section below the current run's picks, instead of
  interleaving every past run's unreviewed roles by `fit_rank` (each run numbers its own
  1..N, so ranks collide across runs). `saved` roles stay in the main section regardless
  of which run surfaced them — they're a completed decision, not pending review. The
  `/my-roles` "Inbox" tab (every `status=new` role, any run) is deliberately unaffected —
  this is `/search`-page-only display grouping of the same underlying rows, not a new
  status or a data change.
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
  daily search cap. Also the per-run diagnostics store, all written at the end of
  `run_search_task`: `phase_timings` and `funnel_counts` (JSON, ints/bools only — read by
  `GET /settings/run-funnel`), plus `snapshot_samples` (JSON
  `{stage: {"count": N, "samples": [{title, company, url}]}}`, read by
  `GET /settings/snapshot` and rendered by the Settings page's bottom "Snapshot" panel).
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
   (`TARGET_POOL` = 90) → **one merged six-axis screen per cluster**
   (`full_auto.screen_gate`, a cached cheap-model call judging sector, seniority,
   candidate-specific requirements, core-skills overlap, salary, and work arrangement in
   one response — grown from an original three-axis sector+seniority+requirements design;
   `SOFT_GATE_AXES` in both `full_auto.py` and `engine.py` is the single source of truth
   for which axes count, so the two modules can't quietly disagree on how many there
   are): `sector_ok` and `hard_gate_ok` are the two unconditional hard drops.
   `hard_gate_ok` enforces the candidate's OWN stated non-negotiables — the `avoid` and
   `must_have` attribute chips (see the data model above) — and is false only when the
   listing *clearly* involves an avoid item or clearly can't satisfy a must-have,
   defaulting to true when the listing is silent, so a thin listing isn't dropped for
   merely failing to confirm. Unlike a sector or soft-axis drop, a `hard_gate_ok` failure
   is also excluded from the `MIN_RESULTS` floor backfill below: resurfacing a job the
   candidate explicitly said to avoid would defeat the point of asking. The other five
   axes are individually
   soft — a job failing only *one* of them still proceeds to `rank_gate` (which gives it
   a real per-job fit score) and reaches the final judge carrying it as a hint, same as
   before. How many soft failures earns a hard drop is now **dynamic per gate round**
   (`full_auto.dynamic_hard_drop_threshold`, shared by `screen_gate`'s own diagnostic log
   and `engine.py`'s actual decision so the two can't disagree): normally 2+ failures
   (two independent clear-mismatch signals agreeing is confident enough to skip paying
   for `rank_gate`/the expensive judge on it, without the risk either signal carries
   alone), but if **over half** of a round's in-sector candidates pass every soft axis
   clean, that's a sign the round is thin on genuine mismatches rather than that
   everyone really fits, so the threshold tightens to 1+ for that round. Added after a
   live run showed `86 in-sector, 72 pass all soft axes, 1 hard-dropped` — a fixed 2+
   floor was barely discriminating on a round that clean. The soft-fail check itself
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
   own share before fair-allocate ever runs. Raised 40 → 55 after a live run showed
   `85 gate survivors -> 85 judge-eligible` — literally nothing scored below the old
   floor, making it a no-op; `rank_gate`'s own log line now also reports the batch's
   min/max/avg score so a floor that's silently toothless again is visible without a
   gate_cache query. The rank-side `MIN_RESULTS` floor backfill (below) still guarantees
   a cluster with any gate survivors reaches the judge, so raising the cutoff can't
   starve a cluster to zero, only make it lean harder on that backfill → optional
   Phase 5 full-page scrape (skipped
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
   to see why anything was excluded). The judge is deliberately structured as an explicit
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
   per job. **Any edit to these prompts must bump `FINAL_EVAL_PROMPT_VERSION`** (now 7)
   or every already-persisted verdict is served stale forever. The
   LOCATION/VISA/RELOCATION disqualifier rule
   applies the same on-site-unless-stated-otherwise classification as the gate's
   work-arrangement axis above before judging eligibility, rather than treating an
   unstated work arrangement as automatically compatible. Whether the judge is actually
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
   to a final cap (`FINAL_PICKS` = 12) → persisted as `Role` rows.
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
3. Cluster/stream identity is used internally (gate routing, per-cluster LLM calls) but
   is **not** currently exposed as UI grouping — by design, not an oversight; it only
   ever surfaces as an optional "Matched via: X track" clause in `ai_analysis` when more
   than one cluster exists.

Key cost/reliability guards layered into this pipeline (tune via env vars, see
`config.py` / top of `full_auto.py`):
- `MAX_SEARCHES_PER_DAY` (default 6) — global daily search cap, shared across all
  profiles (not per-profile).
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
- `JUDGE_POOL` (default 40, `engine.py`) / `RANK_REJECT_SCORE_FLOOR` (default 55,
  `engine.py`) — the cheap rank stage's cap and absolute per-job score cutoff, see
  pipeline step 2. `full_auto.rank_gate`'s fail-open path (its `llm()` call erroring,
  e.g. an intermittent permission/rate error on `MID_MODEL`) retries once on the same
  model after a short backoff, then falls back to `CHEAP_MODEL`, before giving up; a
  job that still has no real score after all of that is tagged `_rank_gate_failed` and
  bypasses `RANK_REJECT_SCORE_FLOOR` entirely in `engine._gate_rank_refill_cluster`
  rather than being compared against it — the fallback neutral score (50) sits below
  the floor (55), so without this bypass "fail-open" was silently rejecting almost
  everything in an affected batch instead of letting it through.
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
