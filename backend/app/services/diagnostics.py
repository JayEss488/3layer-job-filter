"""CV-parse timing diagnostic: run the REAL formation pipeline once against a
throwaway profile, wall-timing each stage and capturing the underlying LLM calls,
then tear the throwaway profile back down. Powers the Settings "CV parse timing"
panel (routers/settings.py).

It calls the genuine formation path a CV upload uses -- extract_text_from_upload,
then the two parallel MID calls (extraction + understand) via
formation.run_formation_calls, then formation.persist_formation -- so the numbers
are real, not a re-implementation that could drift. The two AI calls are measured
as one PARALLEL block (its real wall time), with each call's own duration/tokens
captured underneath, because that's how they run in production. It costs the same
two MID calls a normal upload does; that's the intrinsic cost of measuring, so
this is a user-pressed button, never run automatically. The background ATS harvest
a real upload also kicks off is excluded -- it runs off-request and isn't part of
the latency the user waits on.

The throwaway profile is created is_active=False and deleted in a finally, and any
probe orphaned by a hard crash of a prior run is swept before the next
measurement -- so it never accumulates."""
from __future__ import annotations

import asyncio
from datetime import datetime
from time import perf_counter
from typing import Callable, TypeVar

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import CURRENT_USER_ID, CV_SHORT_WORD_THRESHOLD
from ..models import Profile, Setting
from . import formation
from .llm import capture_llm_calls
from .parsing import CVParseFailed, extract_text_from_upload

T = TypeVar("T")

TIMING_RESULT_KEY = "cv_parse_timing_last"  # global Setting: last measurement, for GET
_PROBE_NAME = "__cv_timing_probe__"


def _timed(label: str, stages: list[dict], fn: Callable[[], T]) -> T:
    """Run fn, recording its wall time and every llm_json call it made as one
    stage entry. Appends only on success -- a raising stage aborts the whole
    measurement (see time_cv_parse's finally for cleanup)."""
    with capture_llm_calls() as calls:
        start = perf_counter()
        result = fn()
        seconds = perf_counter() - start
    stages.append({"name": label, "seconds": round(seconds, 3), "llm_calls": list(calls)})
    return result


def _sweep_stale_probes(db: Session) -> None:
    """Delete any probe profile (and its Setting rows) orphaned by a prior run
    that died before its finally ran. Setting has no FK to Profile, so its rows
    don't cascade -- clear them by hand, same as time_cv_parse does."""
    stale = db.execute(select(Profile).where(Profile.name == _PROBE_NAME)).scalars().all()
    for p in stale:
        db.execute(delete(Setting).where(Setting.profile_id == p.id))
        db.delete(p)
    if stale:
        db.commit()


def time_cv_parse(db: Session, filename: str, raw: bytes) -> dict:
    """Measure a full CV parse stage by stage. Raises CVParseFailed if the file
    yields no text or the extraction call fails (same contract the real parse-cv
    endpoint surfaces)."""
    _sweep_stale_probes(db)
    stages: list[dict] = []

    text = _timed(
        "Extract text from file",
        stages,
        lambda: extract_text_from_upload(filename or "", raw),
    )
    if not text.strip():
        raise CVParseFailed("Could not read any text from that file")

    probe = Profile(user_id=CURRENT_USER_ID, name=_PROBE_NAME, is_active=False)
    probe.cv_text = text[:40000]  # feeds store_seeded_intel's short_cv check, as in prod
    db.add(probe)
    db.flush()  # need probe.id below
    pid = probe.id

    try:
        # The two formation calls run concurrently in production; measure the
        # parallel block's real wall time, with each call's own duration/tokens
        # captured underneath (both worker threads append to the same trace list).
        with capture_llm_calls() as calls:
            start = perf_counter()
            extract_data, understand_data = asyncio.run(formation.run_formation_calls(text, ""))
            block_seconds = perf_counter() - start
        stages.append({
            "name": "Understand + extract (parallel AI calls)",
            "seconds": round(block_seconds, 3),
            "llm_calls": list(calls),
        })

        _timed(
            "Persist (DB writes + family seeding)",
            stages,
            lambda: formation.persist_formation(
                db, pid, text, "cv_parsed", extract_data, understand_data
            ),
        )
    finally:
        # Profile cascade covers attributes + families; Setting has no FK to
        # Profile (generic key/value table), so its profile-scoped rows -- which
        # store_seeded_intel writes -- must be cleared by hand.
        db.execute(delete(Setting).where(Setting.profile_id == pid))
        db.delete(probe)
        db.commit()

    words = len(text.split())
    return {
        "filename": filename or "",
        "measured_at": datetime.utcnow().isoformat() + "Z",
        "text_chars": len(text),
        "text_words": words,
        "generated_summary": words >= CV_SHORT_WORD_THRESHOLD,
        "total_seconds": round(sum(s["seconds"] for s in stages), 3),
        "llm_seconds": round(sum(c["duration_s"] for s in stages for c in s["llm_calls"]), 3),
        "stages": stages,
    }
