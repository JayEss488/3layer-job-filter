"""Profile FORMATION: the CV-upload / text-paste path that turns a raw document
into a full profile in one shot.

Three MID-model LLM calls run IN PARALLEL (they're independent -- all three read
the raw document and nothing else):
  1. extraction (parsing.extract_attributes)      -> structured attribute rows
  2. families   (profile_intel.generate_families) -> role families (label +
       titles) + a first-person intent draft
  3. summary    (profile_intel.generate_summary)  -> cv_summary + "Looking for"
       header. Skipped entirely for a short CV, whose raw text IS its summary.

This replaces the old serial chain of three STRONG calls (parse -> profile_intel
-> re-cluster). Running them concurrently is most of the win on top of the
per-call speedup from MID, and generating the families in the SAME call that
proposes the titles is what fixed the family over-split / over-merge (see
families.seed_families_from_groups).

Calls 2 and 3 used to be ONE "understand" call. It was the whole block's critical
path -- ~11.2s of an 11.3s parse, since it alone generated ~1300 output tokens
while extraction finished in ~4.6s and then idled. Families and summary share
nothing but the source document, so splitting them costs one extra copy of the CV
in input tokens and buys back roughly half the block's wall time. The task PROMPTS
were moved across verbatim; only the per-call task numbering and JSON field list
differ, so neither output's calibration changed.

All three calls are deliberately DB-free so they can run in separate threads with
no shared session; every write happens back on the caller's thread once they all
return, in one commit.
"""
from __future__ import annotations

import asyncio

from sqlalchemy.orm import Session

from ..config import CV_SHORT_WORD_THRESHOLD, CV_SUMMARY_RAW_MAX_CHARS
from ..models import Profile, ProfileAttribute
from .families import ensure_families, seed_families_from_groups
from .parsing import CVParseFailed, extract_attributes, persist_attributes
from .profile_intel import (
    clip_summary, generate_families, generate_summary, store_seeded_intel,
)


def _is_short_cv(text: str) -> bool:
    """A document short enough to read as its own summary -- see
    CV_SHORT_WORD_THRESHOLD. Empty text is NOT short (it has no summary at all)."""
    return 0 < len(text.split()) < CV_SHORT_WORD_THRESHOLD


async def run_formation_calls(
    text: str, intent_text: str | None
) -> tuple[dict, dict | None]:
    """Run the extraction + families + summary LLM calls concurrently, in worker
    threads. Returns (extract_data, understand_data), where understand_data merges
    the families and summary results into the single dict persist_formation
    expects.

    understand_data is None ONLY when the FAMILIES call failed -- that's the
    transient-failure signal persist_formation relies on to leave existing profile
    state untouched. A summary-call failure is not fatal on its own: the families
    still persist and cv_summary/header simply come back empty. Raises nothing."""
    short_cv = _is_short_cv(text)
    calls = [
        asyncio.to_thread(extract_attributes, text),
        # No auto-drafted intent statement off a short document -- see
        # generate_families' docstring. The onboarding/profile box stays empty and
        # optional instead, and is used verbatim if the candidate does fill it.
        asyncio.to_thread(
            generate_families, text, intent_text, want_intent_draft=not short_cv
        ),
    ]
    if not short_cv:
        # A short document reads as its own summary (persist_formation stores the
        # raw text verbatim), so don't spend the call at all.
        calls.append(asyncio.to_thread(generate_summary, text, intent_text))

    results = await asyncio.gather(*calls)
    extract_data, families_data = results[0], results[1]
    summary_data = results[2] if not short_cv else {}

    if families_data is None:
        return extract_data, None
    return extract_data, {**families_data, **(summary_data or {})}


def persist_formation(
    db: Session,
    profile_id: int,
    text: str,
    source: str,
    extract_data: dict,
    understand_data: dict | None,
) -> list[ProfileAttribute]:
    """Persist both calls' results in one transaction: attribute rows, cv_summary,
    the seeded role families (+ their target roles), a drafted intent_text (only
    when it was empty), and the profile-intel cache (so the pre-search regenerate
    is a no-op and doesn't reshuffle the freshly-seeded families).

    Raises CVParseFailed only when the EXTRACTION call itself failed (nothing to
    build a profile from). An understand-call failure is non-fatal: the extracted
    attributes still persist and the user can regenerate roles later."""
    if not extract_data:
        raise CVParseFailed(
            "The AI parsing call failed -- see server logs for the underlying error"
        )

    created = persist_attributes(db, profile_id, extract_data, source)

    if understand_data is not None:
        profile = db.get(Profile, profile_id)
        # cv_summary: the AI compression for a long CV; the raw text verbatim for
        # a short one (compressing it would only lose concrete detail for no space
        # saved -- see CV_SHORT_WORD_THRESHOLD).
        if _is_short_cv(text):
            profile.cv_summary = clip_summary(text, CV_SUMMARY_RAW_MAX_CHARS)
        else:
            profile.cv_summary = understand_data.get("cv_summary") or ""

        created += seed_families_from_groups(
            db, profile_id, understand_data.get("families") or []
        )

        if understand_data.get("intent_draft") and not (profile.intent_text or "").strip():
            profile.intent_text = understand_data["intent_draft"]

        db.flush()  # so store_seeded_intel's signature reflects the new roles/summary
        store_seeded_intel(db, profile_id, understand_data.get("header") or "")

    # Belt-and-braces: give any target_role that somehow arrived ungrouped a
    # family. A no-op in the normal seeded case (every seeded role already has
    # one), so it makes no LLM call there.
    ensure_families(db, profile_id)
    db.commit()
    for a in created:
        db.refresh(a)
    return created
