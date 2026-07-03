"""Per-source visibility toggle (upgrade workstream D).

One place that knows the full set of discovery sources, their toggle state, and
the last run's per-source counts. Toggles are global (single-user prototype) and
stored in the Setting table; the engine reads the disabled set before a run and
writes the per-source counts after it, so the settings screen can surface "97% of
discovery is Greenhouse" directly instead of it hiding in the debug JSON.
"""
from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import JobSeen, Role, Setting

# Canonical source registry. `key` is what gather_jobs filters on: API source
# `.name`s and ATS vendor names. `board_prefix` maps the raw `board` tag (which
# uses "gh" for greenhouse) back to the canonical key for counting.
SOURCES: list[dict] = [
    {"key": "reed",        "label": "Reed",             "kind": "api", "board_prefix": "reed"},
    {"key": "adzuna",      "label": "Adzuna",           "kind": "api", "board_prefix": "adzuna"},
    {"key": "google_jobs", "label": "Google Jobs",      "kind": "api", "board_prefix": "google_jobs"},
    {"key": "jsearch",     "label": "JSearch",          "kind": "api", "board_prefix": "jsearch"},
    {"key": "remotive",    "label": "Remotive",         "kind": "api", "board_prefix": "remotive"},
    {"key": "greenhouse",  "label": "Greenhouse (ATS)", "kind": "ats", "board_prefix": "gh"},
    {"key": "lever",       "label": "Lever (ATS)",      "kind": "ats", "board_prefix": "lever"},
    {"key": "ashby",       "label": "Ashby (ATS)",      "kind": "ats", "board_prefix": "ashby"},
    {"key": "workable",    "label": "Workable (ATS)",   "kind": "ats", "board_prefix": "workable"},
    {"key": "recruitee",   "label": "Recruitee (ATS)",  "kind": "ats", "board_prefix": "recruitee"},
    {"key": "personio",    "label": "Personio (ATS)",   "kind": "ats", "board_prefix": "personio"},
]

_PREFIX_TO_KEY = {s["board_prefix"]: s["key"] for s in SOURCES}
_VALID_KEYS = {s["key"] for s in SOURCES}


def canonical_key(board: str | None) -> str | None:
    """Normalise a raw board tag (e.g. 'gh:token123') to its canonical source
    key (e.g. 'greenhouse'). Mirrors the prefix-splitting engine.py already
    does in _board_breakdown."""
    if not board:
        return None
    prefix = board.split(":")[0]
    return _PREFIX_TO_KEY.get(prefix)

_DISABLED_KEY = "disabled_sources"
_COUNTS_KEY = "source_counts_last_run"
_FULL_SCRAPE_KEY = "full_scrape_enabled"


def _get(db: Session, key: str) -> Setting | None:
    return db.execute(
        select(Setting).where(Setting.profile_id.is_(None), Setting.key == key)
    ).scalar_one_or_none()


def _set(db: Session, key: str, value: str) -> None:
    row = _get(db, key)
    if row is not None:
        row.value = value
    else:
        db.add(Setting(profile_id=None, key=key, value=value))
    db.commit()


def get_disabled(db: Session) -> set[str]:
    row = _get(db, _DISABLED_KEY)
    if not row or not row.value:
        return set()
    try:
        return {k for k in json.loads(row.value) if k in _VALID_KEYS}
    except (ValueError, TypeError):
        return set()


def set_disabled(db: Session, keys: list[str]) -> set[str]:
    cleaned = sorted({k for k in keys if k in _VALID_KEYS})
    _set(db, _DISABLED_KEY, json.dumps(cleaned))
    return set(cleaned)


def counts_from_breakdown(breakdown: dict[str, int]) -> dict[str, int]:
    """Normalise a raw board breakdown ({'gh': 12, 'reed': 40, ...}) to canonical
    source keys ({'greenhouse': 12, 'reed': 40, ...})."""
    out: dict[str, int] = {s["key"]: 0 for s in SOURCES}
    for prefix, n in breakdown.items():
        key = _PREFIX_TO_KEY.get(prefix)
        if key:
            out[key] += n
    return out


def save_last_run_counts(db: Session, counts: dict[str, int]) -> None:
    _set(db, _COUNTS_KEY, json.dumps(counts))


def get_last_run_counts(db: Session) -> dict[str, int]:
    row = _get(db, _COUNTS_KEY)
    if not row or not row.value:
        return {}
    try:
        return dict(json.loads(row.value))
    except (ValueError, TypeError):
        return {}


def get_full_scrape_enabled(db: Session) -> bool:
    """Whether the pipeline reads each job's real page before final evaluation.
    Costs real time per search but lets the LLM see the actual requirements
    text (seniority, years, sector) instead of judging fit from a short
    API snippet. Defaults on."""
    row = _get(db, _FULL_SCRAPE_KEY)
    if not row or not row.value:
        return True
    return row.value == "1"


def set_full_scrape_enabled(db: Session, enabled: bool) -> bool:
    _set(db, _FULL_SCRAPE_KEY, "1" if enabled else "0")
    return enabled


def source_funnel(db: Session) -> list[dict]:
    """All-time, per-source funnel: how many jobs each source has discovered,
    how many made the top-25 shortlist pool, how many made the final AI-picked
    shortlist, and how many the user actually saved/applied to. Lets you see
    which sources are worth keeping before an API gets cut."""
    discovered: dict[str, int] = {}
    for source, count in db.execute(
        select(JobSeen.source, func.count(JobSeen.id)).group_by(JobSeen.source)
    ).all():
        key = canonical_key(source)
        if key:
            discovered[key] = discovered.get(key, 0) + count

    top25: dict[str, int] = {}
    for source, count in db.execute(
        select(JobSeen.source, func.count(JobSeen.id))
        .where(JobSeen.state.in_(["enriched", "shown"]))
        .group_by(JobSeen.source)
    ).all():
        key = canonical_key(source)
        if key:
            top25[key] = top25.get(key, 0) + count

    shown: dict[str, int] = {}
    for source, count in db.execute(
        select(JobSeen.source, func.count(JobSeen.id))
        .where(JobSeen.state == "shown")
        .group_by(JobSeen.source)
    ).all():
        key = canonical_key(source)
        if key:
            shown[key] = shown.get(key, 0) + count

    selected: dict[str, int] = {}
    for source, count in db.execute(
        select(Role.source, func.count(Role.id))
        .where(Role.status.in_(["saved", "applied"]))
        .group_by(Role.source)
    ).all():
        key = canonical_key(source)
        if key:
            selected[key] = selected.get(key, 0) + count

    return [
        {
            "key": s["key"],
            "label": s["label"],
            "discovered": discovered.get(s["key"], 0),
            "top25": top25.get(s["key"], 0),
            "shown": shown.get(s["key"], 0),
            "selected": selected.get(s["key"], 0),
        }
        for s in SOURCES
    ]


def sources_overview(db: Session) -> list[dict]:
    """Full list for the settings screen: each source with its toggle state and
    the count it contributed on the last run."""
    disabled = get_disabled(db)
    counts = get_last_run_counts(db)
    return [
        {
            "key": s["key"],
            "label": s["label"],
            "kind": s["kind"],
            "enabled": s["key"] not in disabled,
            "last_count": counts.get(s["key"], 0),
        }
        for s in SOURCES
    ]
