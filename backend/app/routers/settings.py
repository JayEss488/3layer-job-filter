"""Settings endpoints. Currently: the per-source visibility toggle (workstream D)
that lets the user see each discovery source's last-run count and turn sources on
or off (e.g. disable an ATS vendor that's flooding the pool)."""
import asyncio
import json
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import SearchRun, Setting
from ..services.diagnostics import TIMING_RESULT_KEY, time_cv_parse
from ..services import engine as engine_svc
from ..services.moderation import get_blocked_domains, set_blocked_domains
from ..services.parsing import CVParseFailed
from ..services.sources import (
    get_full_scrape_enabled,
    set_disabled,
    set_full_scrape_enabled,
    source_funnel,
    sources_overview,
)

router = APIRouter(tags=["settings"])


class SourceOut(BaseModel):
    key: str
    label: str
    kind: str          # "api" | "ats"
    enabled: bool
    last_count: int    # rows this source contributed on the last run


class SourceToggleIn(BaseModel):
    disabled: list[str]  # canonical source keys to disable; all others enabled


class ScrapeSettingOut(BaseModel):
    enabled: bool


class ScrapeSettingIn(BaseModel):
    enabled: bool


class SourceStatOut(BaseModel):
    key: str
    label: str
    discovered: int  # all-time jobs discovered from this source
    gated: int         # survived the sector/seniority gates + embed-score cut
    shown: int         # made the final AI-picked shortlist
    selected: int      # user saved or applied


class RunTokenStageOut(BaseModel):
    """One LLM stage's token spend for a run (full_auto's _record_llm_usage,
    flattened into funnel_counts as tokens_{stage}_{...}).

    `cached_tokens` is the part of `prompt_tokens` OpenAI served from its prompt
    cache. Every prompt in this pipeline is a long fixed prefix (screen ~4.5k,
    rank ~2.5k, judge ~12k tokens) followed by a short variable payload, so a low
    hit rate here means that prefix is being re-billed in full on every call --
    the single largest avoidable cost in a run, and invisible before this."""
    stage: str
    calls: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_ratio: float | None = None   # cached/prompt; None when nothing sent


class RunFunnelOut(BaseModel):
    """Cross-stage funnel for the most recent finished search run -- the
    all-together counterpart to SourceStatOut's per-source, all-time view."""
    run_id: int | None = None
    finished_at: datetime | None = None
    entering: int = 0                    # raw_discovered
    passed_heuristic_embedding: int = 0  # candidate_queue_size
    passed_gates: int = 0                # gate_survivors_total
    final_judge: int = 0                 # final_strong + final_backup
    final_judge_rejected: int = 0        # final_fresh_judged - final_strong - final_backup
    judge_pool_size: int = 0             # initial judge pool, capped at JUDGE_POOL (engine.py)
    judge_dupes_suppressed: int = 0      # near-duplicate postings dropped pre-judge (engine.py::_suppress_judge_duplicates)
    shown: int = 0                       # final_picks
    examined: int = 0                    # rank_scored -- total examined by the cheap+mid gates
    # final_judge / examined -- a coarse "how niche is this profile" gauge, not
    # a pipeline health metric: a low ratio can equally mean the gates are too
    # strict OR the profile is a genuinely thin niche (see
    # tests/tier_analysis.py for telling those apart). None when nothing was
    # examined yet, rather than a misleading 0%.
    filtering_ratio: float | None = None
    token_usage: list[RunTokenStageOut] = []


class RunPhaseOut(BaseModel):
    """One timed phase of a search run, as recorded by engine.py's _lap()."""
    name: str
    label: str
    seconds: float


class RunClusterOut(BaseModel):
    """One role cluster's own funnel through a run. Every field is optional
    because a run can end before the judge stage fills the judge-side half in
    (an early return, a cancel), and because runs recorded before this panel
    existed have no cluster data at all."""
    idx: int = 0
    label: str = ""
    queue_len: int = 0
    examined: int = 0
    gate_survivors: int = 0
    hard_dropped: int = 0
    off_sector: int = 0
    hard_gate_dropped: int = 0
    rank_floor_rejected: int = 0
    judge_eligible: int = 0
    stop_reason: str = ""
    judged: int = 0
    judge_reused_from_cache: int = 0
    judge_strong: int = 0
    judge_backup: int = 0
    judge_disqualified: int = 0
    picks: int = 0
    fallbacks: list[str] = []


class RunCapsOut(BaseModel):
    """Today's live pipeline cap constants (engine.get_pipeline_caps) -- shown
    under the per-cluster table so a "stopped because: absolute pool cap" row
    can be checked against the actual number instead of the reader needing to
    know it from memory. Not stored per-run: these are just today's config."""
    rank_examine_budget: int = 0
    rank_target_pool: int = 0
    judge_pool: int = 0
    judge_pool_floor: int = 0
    rank_reject_score_floor: int = 0
    target_pool_per_round: int = 0
    min_results_floor: int = 0
    final_picks: int = 0


class RunTimingsOut(BaseModel):
    """Per-phase wall time for the most recent finished search run, plus each
    role cluster's own funnel. The counterpart to CvParseTimingOut for the search
    side: SearchRun.phase_timings has been written every run for a long time but
    was never exposed anywhere, so "where did the run's four minutes go?" could
    only be answered by watching the backend console live."""
    run_id: int | None = None
    finished_at: datetime | None = None
    total_seconds: float = 0.0
    phases: list[RunPhaseOut] = []
    clusters: list[RunClusterOut] = []
    caps: RunCapsOut = RunCapsOut()


# Human labels for engine.py's _lap() phase keys, in pipeline order. An unknown
# key (a phase added later, or an old run's retired one -- e.g. the separate
# "scrape"/"final_eval" laps that became one overlapped "scrape+judge") still
# renders, just under its raw name.
_RUN_PHASE_LABELS = [
    ("discovery", "Discovery (job board + ATS queries)"),
    ("category_expand", "Category-page expansion"),
    ("embed", "Embedding new listings"),
    ("score", "Cosine scoring against clusters"),
    ("enrich", "Fetching full descriptions (Reed)"),
    ("gate", "Cheap screen + rank gate (per cluster)"),
    ("judge_floor_topup", "Judge-pool floor top-up (extra gate+rank round)"),
    ("rank", "Duplicate suppression + fair-allocate to judge pool"),
    ("scrape", "Full-page scraping"),
    ("final_eval", "Final AI judge"),
    ("scrape+judge", "Full-page scrape + final AI judge (overlapped)"),
]


class SnapshotJobOut(BaseModel):
    title: str = ""
    company: str = ""
    url: str = ""
    # Only populated for stages that carry a per-item audit reason (currently
    # "rank_rejected" -- rank_gate's score + short note, see engine.py::_sample_stage).
    # "" everywhere else.
    note: str = ""


class SnapshotStageOut(BaseModel):
    stage: str          # machine key, as recorded by engine.py::_snap
    label: str          # human label for the panel
    count: int          # how many roles existed at this stage
    samples: list[SnapshotJobOut] = []


class SnapshotOut(BaseModel):
    """Per-stage sample roles for the most recent finished run -- the qualitative
    counterpart to RunFunnelOut's counts: which roles were actually at each stage,
    with URLs, so a weak stage can be spotted (or fed to an AI) rather than
    inferred from a drop in the numbers."""
    run_id: int | None = None
    finished_at: datetime | None = None
    stages: list[SnapshotStageOut] = []


# Pipeline order + labels for the Snapshot panel. Keys must match the stage names
# engine.py::_run_engine_pipeline passes to _snap(); a stage missing from a given
# run's payload (e.g. an early return) is simply skipped.
_SNAPSHOT_STAGES = [
    ("discovery", "Discovered (raw from sources)"),
    ("after_filters", "After blocklist / training / country / salary filters"),
    ("pool", "Candidate pool (fresh + resurfaced backlog)"),
    ("scored", "Embedded & cosine-scored"),
    ("heuristic_survivors", "Survived heuristic prescreen"),
    ("judge_eligible", "Survived cheap gate + rank floor"),
    ("rank_rejected", "Cut by the rank-score floor (below cutoff)"),
    ("judge_pool", "Selected for the judge (fair-allocated)"),
    ("scraped", "Full text ready (scraped or snippet)"),
    ("final_picks", "Final picks shown"),
]


class BlocklistOut(BaseModel):
    domains: list[str]


class BlocklistIn(BaseModel):
    domains: list[str]


class LlmCallOut(BaseModel):
    """One underlying model call captured during a timed parse stage."""
    model: str
    prompt_chars: int
    duration_s: float
    attempts: int
    ok: bool
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None


class ParseStageOut(BaseModel):
    name: str
    seconds: float
    llm_calls: list[LlmCallOut] = []


class CvParseTimingOut(BaseModel):
    """Per-stage wall time for a full CV parse (see services/diagnostics.py).
    Empty (measured_at=None) until Measure has been pressed at least once."""
    filename: str = ""
    measured_at: str | None = None
    text_chars: int = 0
    text_words: int = 0
    generated_summary: bool = False
    total_seconds: float = 0.0
    llm_seconds: float = 0.0
    stages: list[ParseStageOut] = []


@router.get("/settings/sources", response_model=list[SourceOut])
def list_sources(db: Session = Depends(get_db)):
    return sources_overview(db)


@router.put("/settings/sources", response_model=list[SourceOut])
def update_sources(body: SourceToggleIn, db: Session = Depends(get_db)):
    set_disabled(db, body.disabled)
    return sources_overview(db)


@router.get("/settings/source-stats", response_model=list[SourceStatOut])
def get_source_stats(db: Session = Depends(get_db)):
    return source_funnel(db)


@router.get("/settings/run-funnel", response_model=RunFunnelOut)
def get_run_funnel(db: Session = Depends(get_db)):
    """Cross-stage funnel (entering -> heuristic/embedding -> gates -> final
    judge -> shown) for the most recently finished search run, across any
    profile -- single-user prototype, see CLAUDE.md. funnel_counts is written
    every run (engine.py::_run_engine_pipeline) but wasn't previously exposed
    to the frontend anywhere."""
    run = db.execute(
        select(SearchRun)
        .where(SearchRun.status == "done", SearchRun.funnel_counts.isnot(None))
        .order_by(SearchRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not run:
        return RunFunnelOut()
    try:
        counts = json.loads(run.funnel_counts or "{}")
    except (ValueError, TypeError):
        counts = {}
    examined = counts.get("rank_scored", 0)
    shown_to_judge = counts.get("final_strong", 0) + counts.get("final_backup", 0)
    # Un-flatten the tokens_{stage}_{metric} keys engine.py wrote. Driven off the
    # keys actually present rather than a fixed stage list, so a stage added or
    # renamed in full_auto shows up here without a matching edit. Recovered by
    # stripping the known affixes, NOT by splitting on "_": both the stage names
    # ("rank_fallback") and the metric names ("prompt_tokens") contain
    # underscores, so any split-based parse mis-attributes one to the other.
    token_stages = sorted({
        k[len("tokens_"):-len("_calls")]
        for k in counts if k.startswith("tokens_") and k.endswith("_calls")
    })
    token_usage = []
    for stage in token_stages:
        prompt_tokens = counts.get(f"tokens_{stage}_prompt_tokens", 0)
        cached = counts.get(f"tokens_{stage}_cached_tokens", 0)
        token_usage.append(RunTokenStageOut(
            stage=stage,
            calls=counts.get(f"tokens_{stage}_calls", 0),
            prompt_tokens=prompt_tokens,
            cached_tokens=cached,
            completion_tokens=counts.get(f"tokens_{stage}_completion_tokens", 0),
            cache_hit_ratio=round(cached / prompt_tokens, 4) if prompt_tokens else None,
        ))
    return RunFunnelOut(
        run_id=run.id,
        finished_at=run.finished_at,
        entering=counts.get("raw_discovered", 0),
        passed_heuristic_embedding=counts.get("candidate_queue_size", 0),
        passed_gates=counts.get("gate_survivors_total", 0),
        final_judge=shown_to_judge,
        final_judge_rejected=(
            counts.get("final_fresh_judged", 0) - counts.get("final_strong", 0)
            - counts.get("final_backup", 0)
        ),
        judge_pool_size=counts.get("judge_pool_size", 0),
        judge_dupes_suppressed=counts.get("judge_dupes_suppressed", 0),
        shown=counts.get("final_picks", 0),
        examined=examined,
        filtering_ratio=round(shown_to_judge / examined, 4) if examined else None,
        token_usage=token_usage,
    )


@router.get("/settings/run-timings", response_model=RunTimingsOut)
def get_run_timings(db: Session = Depends(get_db)):
    """Per-phase wall time + per-cluster funnel for the most recently finished
    search run, across any profile -- same "last finished run" selection as
    get_run_funnel above. Pure read of what the run already recorded: no LLM
    cost, nothing re-computed."""
    run = db.execute(
        select(SearchRun)
        .where(SearchRun.status == "done", SearchRun.phase_timings.isnot(None))
        .order_by(SearchRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not run:
        return RunTimingsOut()
    try:
        timings = json.loads(run.phase_timings or "{}")
    except (ValueError, TypeError):
        timings = {}
    try:
        samples = json.loads(run.snapshot_samples or "{}")
    except (ValueError, TypeError):
        samples = {}

    known = [(key, label) for key, label in _RUN_PHASE_LABELS if key in timings]
    labelled = {key for key, _ in known}
    phases = [RunPhaseOut(name=key, label=label, seconds=float(timings.get(key) or 0.0))
              for key, label in known]
    # Anything _lap() recorded that this module doesn't have a label for yet --
    # shown under its raw key rather than silently dropped from the total.
    phases += [RunPhaseOut(name=key, label=key, seconds=float(value or 0.0))
               for key, value in timings.items() if key not in labelled]

    clusters = [RunClusterOut(**{k: v for k, v in c.items() if k in RunClusterOut.model_fields})
                for c in (samples.get("_clusters") or []) if isinstance(c, dict)]
    return RunTimingsOut(
        run_id=run.id,
        finished_at=run.finished_at,
        total_seconds=round(sum(p.seconds for p in phases), 2),
        phases=phases,
        clusters=clusters,
        caps=RunCapsOut(**engine_svc.get_pipeline_caps()),
    )


@router.get("/settings/snapshot", response_model=SnapshotOut)
def get_run_snapshot(db: Session = Depends(get_db)):
    """Sample roles per pipeline stage for the most recently finished run (any
    profile -- single-user prototype, see CLAUDE.md). Written by engine.py's
    _snap() into SearchRun.snapshot_samples as
    {stage: {"count": N, "samples": [{title, company, url}]}}."""
    run = db.execute(
        select(SearchRun)
        .where(SearchRun.status == "done", SearchRun.snapshot_samples.isnot(None))
        .order_by(SearchRun.finished_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not run:
        return SnapshotOut()
    try:
        payload = json.loads(run.snapshot_samples or "{}")
    except (ValueError, TypeError):
        payload = {}
    stages = []
    for key, label in _SNAPSHOT_STAGES:
        entry = payload.get(key)
        if not isinstance(entry, dict):
            continue
        stages.append(SnapshotStageOut(
            stage=key,
            label=label,
            count=entry.get("count", 0),
            samples=[SnapshotJobOut(**s) for s in (entry.get("samples") or [])
                     if isinstance(s, dict)],
        ))
    return SnapshotOut(run_id=run.id, finished_at=run.finished_at, stages=stages)


@router.get("/settings/scrape", response_model=ScrapeSettingOut)
def get_scrape_setting(db: Session = Depends(get_db)):
    return {"enabled": get_full_scrape_enabled(db)}


@router.put("/settings/scrape", response_model=ScrapeSettingOut)
def update_scrape_setting(body: ScrapeSettingIn, db: Session = Depends(get_db)):
    return {"enabled": set_full_scrape_enabled(db, body.enabled)}


@router.get("/settings/blocklist", response_model=BlocklistOut)
def get_blocklist(db: Session = Depends(get_db)):
    return {"domains": get_blocked_domains(db)}


@router.put("/settings/blocklist", response_model=BlocklistOut)
def update_blocklist(body: BlocklistIn, db: Session = Depends(get_db)):
    return {"domains": set_blocked_domains(db, body.domains)}


def _timing_setting_row(db: Session) -> Setting | None:
    return db.execute(
        select(Setting).where(Setting.profile_id.is_(None), Setting.key == TIMING_RESULT_KEY)
    ).scalar_one_or_none()


@router.get("/settings/cv-parse-timing", response_model=CvParseTimingOut)
def get_cv_parse_timing(db: Session = Depends(get_db)):
    """The most recent CV-parse timing measurement (global, single-user). Empty
    until Measure has run once -- pure read, no LLM cost."""
    row = _timing_setting_row(db)
    if not row or not row.value:
        return CvParseTimingOut()
    try:
        return CvParseTimingOut(**json.loads(row.value))
    except (ValueError, TypeError):
        return CvParseTimingOut()


@router.post("/settings/cv-parse-timing", response_model=CvParseTimingOut)
async def run_cv_parse_timing(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Measure a full CV parse stage by stage against a throwaway profile.
    Spends the same three STRONG-model calls a real upload does -- user-triggered
    only (see services/diagnostics.py). The result is stored as the global
    'last measured' so the panel shows it again on reload."""
    raw = await file.read()
    try:
        result = await asyncio.to_thread(time_cv_parse, db, file.filename or "", raw)
    except CVParseFailed as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    row = _timing_setting_row(db)
    payload = json.dumps(result)
    if row is not None:
        row.value = payload
    else:
        db.add(Setting(profile_id=None, key=TIMING_RESULT_KEY, value=payload))
    db.commit()
    return CvParseTimingOut(**result)
