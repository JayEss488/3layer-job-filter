"""Settings endpoints. Currently: the per-source visibility toggle (workstream D)
that lets the user see each discovery source's last-run count and turn sources on
or off (e.g. disable an ATS vendor that's flooding the pool)."""
import json
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import SearchRun
from ..services.moderation import get_blocked_domains, set_blocked_domains
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
    shown: int = 0                       # final_picks


class SnapshotJobOut(BaseModel):
    title: str = ""
    company: str = ""
    url: str = ""


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
    ("judge_pool", "Selected for the judge (fair-allocated)"),
    ("scraped", "Full text ready (scraped or snippet)"),
    ("final_picks", "Final picks shown"),
]


class BlocklistOut(BaseModel):
    domains: list[str]


class BlocklistIn(BaseModel):
    domains: list[str]


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
    return RunFunnelOut(
        run_id=run.id,
        finished_at=run.finished_at,
        entering=counts.get("raw_discovered", 0),
        passed_heuristic_embedding=counts.get("candidate_queue_size", 0),
        passed_gates=counts.get("gate_survivors_total", 0),
        final_judge=counts.get("final_strong", 0) + counts.get("final_backup", 0),
        final_judge_rejected=(
            counts.get("final_fresh_judged", 0) - counts.get("final_strong", 0)
            - counts.get("final_backup", 0)
        ),
        shown=counts.get("final_picks", 0),
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
