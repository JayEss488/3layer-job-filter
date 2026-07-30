"""Profile-derived ATS harvesting (upgrade workstream C).

Turns a profile's target roles / skills / sectors into keyword + adjacent-sector
phrases -- NOT company names, which an LLM would hallucinate or let go stale --
and runs them through the engine's existing SerpAPI `site:` search, which only
returns companies that actually have a live ATS board. Results land in the shared
`company_ats` store, so discovery (fetch_ats / gather_jobs) needs no changes.

The candidate's plain place name (see _location_text) rides along as a soft
query bias, not a hard filter -- a live test harvesting for a Dubai-based
profile with no location signal found 78 companies, every one a US/European
tech company, since nothing in the search favoured one with an actual regional
presence. See full_auto.harvest_ats_tokens' docstring for why this stays a bias
rather than a post-hoc geographic filter.

Deliberately DECOUPLED from search runs: harvesting spends a SerpAPI credit per
query and only changes when the profile's targets change, so it's triggered on
profile create / meaningful edit and skipped when the derived keywords are
unchanged since the last harvest. Fetching known boards still runs every search.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import WORK_TYPE_VALUES
from ..database import SessionLocal
from ..models import ProfileAttribute, Setting
from .llm import llm_json

HARVEST_KEYWORD_HASH = "harvest_keyword_hash"   # post-call: gates the SerpAPI spend
HARVEST_SIGNAL_HASH = "harvest_signal_hash"     # pre-call: gates the LLM call itself


def _signals(db: Session, profile_id: int) -> dict[str, list[str]]:
    attrs = db.execute(
        select(ProfileAttribute).where(ProfileAttribute.profile_id == profile_id)
    ).scalars().all()
    by_type: dict[str, list[str]] = {}
    for a in attrs:
        by_type.setdefault(a.type, []).append(a.value)
    return by_type


def _location_text(sig: dict[str, list[str]]) -> str:
    """The candidate's plain place name(s) (e.g. "Dubai"), stripped of the
    Remote/Hybrid/On-site work-type tokens that share the `location` attribute
    type -- see WORK_TYPE_VALUES. Used only as a soft query bias (see
    full_auto.harvest_ats_tokens' `location` param), never a hard filter, so an
    imprecise or multi-place result costs nothing worse than a slightly less
    targeted search."""
    places = [v for v in sig.get("location", []) if v.strip().lower() not in WORK_TYPE_VALUES]
    return ", ".join(places)


def _signal_digest(sig: dict[str, list[str]]) -> str:
    roles = sig.get("target_role", []) + sig.get("past_role", [])
    skills = sig.get("skill", [])
    sectors = sig.get("sector_target", [])
    location = _location_text(sig)
    basis = json.dumps({
        "roles": sorted(r.strip().lower() for r in roles),
        "skills": sorted(s.strip().lower() for s in skills),
        "sectors": sorted(s.strip().lower() for s in sectors),
        "location": location.strip().lower(),
    }, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()


def derive_keywords(sig: dict[str, list[str]]) -> list[str]:
    """LLM -> search keyword / sector phrases grounded in the profile. Kept to
    phrases (not company names) so the downstream `site:` search stays the thing
    that decides which real companies exist."""
    roles = sig.get("target_role", []) + sig.get("past_role", [])
    skills = sig.get("skill", [])
    sectors = sig.get("sector_target", [])
    if not roles and not skills and not sectors:
        return []
    # sector_target is the candidate's own stated vertical (e.g. "Animal welfare",
    # "Nonprofit organizations") -- without it, a profile whose target-role titles
    # are generic function names (Campaign Manager, Community Organizer, ...)
    # gives this call nothing sector-specific to work from, and it falls back to
    # generic marketing/campaign phrasing that collides with unrelated
    # marketing-SaaS companies instead of the candidate's actual field. A live
    # animal-welfare profile's harvest (roles/skills only) returned "campaign
    # management"/"campaign coordination" and found Customer.io, Vevo, TeamDynamix
    # -- zero animal-welfare orgs -- before sector_target was added here.
    data = llm_json(
        f"""A job seeker is targeting roles like: {', '.join(roles) or 'n/a'}.
Their skills: {', '.join(skills) or 'n/a'}.
Their target sector/field: {', '.join(sectors) or 'n/a'}.
Produce short SEARCH KEYWORDS and adjacent SECTOR phrases that companies hiring
for these roles would use on their job boards (e.g. "marketing automation",
"climate nonprofit", "data analyst", "science communication", "operations").
The target sector/field above is the most specific signal of what industry these
companies should be in -- weight it heavily so a generic role title (e.g.
"Campaign Manager", "Community Organizer") doesn't produce phrases that also
match unrelated industries (e.g. marketing-automation SaaS instead of nonprofit
advocacy) when a sector is stated.
Do NOT return company names -- only role/skill/sector phrases.
Return ONLY JSON: {{"keywords": ["...", ...]}} with 8-16 concise phrases."""
    )
    raw = data.get("keywords") if isinstance(data.get("keywords"), list) else []
    seen: set[str] = set()
    out: list[str] = []
    for k in raw:
        k = str(k).strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:16]


def _marker(db: Session, profile_id: int, key: str) -> Setting | None:
    return db.execute(
        select(Setting).where(Setting.profile_id == profile_id, Setting.key == key)
    ).scalar_one_or_none()


def _upsert(db: Session, profile_id: int, key: str, value: str) -> None:
    row = _marker(db, profile_id, key)
    if row is not None:
        row.value = value
    else:
        db.add(Setting(profile_id=profile_id, key=key, value=value))


def harvest_for_profile(profile_id: int, force: bool = False) -> dict:
    """Own-session entry point, safe to run in a FastAPI BackgroundTask.

    Two cache gates, checked in order (unless forced, e.g. a periodic top-up or
    a feedback-driven re-harvest):
    1. pre-call -- skip deriving keywords at all (the LLM call itself) when the
       profile's target_role/past_role/skill/sector_target/location signal
       hasn't changed since the last harvest.
    2. post-call -- skip the SerpAPI spend when the derived keyword set is
       unchanged even though the signal did (an LLM re-deriving the same
       keywords from a slightly different input is plausible and shouldn't
       still cost a web search)."""
    import full_auto as engine  # lazy, mirrors engine.run_search_task

    db = SessionLocal()
    try:
        sig = _signals(db, profile_id)
        roles = sig.get("target_role", []) + sig.get("past_role", [])
        skills = sig.get("skill", [])
        sectors = sig.get("sector_target", [])
        if not roles and not skills and not sectors:
            return {"harvested": 0, "skipped": "no-signals", "keywords": []}

        signal_digest = _signal_digest(sig)
        signal_marker = _marker(db, profile_id, HARVEST_SIGNAL_HASH)
        if not force and signal_marker is not None and signal_marker.value == signal_digest:
            return {"harvested": 0, "skipped": "unchanged-signal", "keywords": []}

        keywords = derive_keywords(sig)
        if not keywords:
            return {"harvested": 0, "skipped": "no-signals", "keywords": []}

        digest = hashlib.sha256(json.dumps(sorted(keywords)).encode()).hexdigest()
        marker = _marker(db, profile_id, HARVEST_KEYWORD_HASH)
        if not force and marker is not None and marker.value == digest:
            # Signal changed but derived the same keywords -- still advance the
            # signal marker so an unchanged repeat short-circuits pre-call next
            # time, but skip the SerpAPI spend as before.
            _upsert(db, profile_id, HARVEST_SIGNAL_HASH, signal_digest)
            db.commit()
            return {"harvested": 0, "skipped": "unchanged", "keywords": keywords}

        location = _location_text(sig)
        rows = engine.harvest_ats_tokens(keywords, location=location)  # writes to the shared company_ats

        _upsert(db, profile_id, HARVEST_KEYWORD_HASH, digest)
        _upsert(db, profile_id, HARVEST_SIGNAL_HASH, signal_digest)
        db.commit()
        return {"harvested": len(rows), "keywords": keywords}
    finally:
        db.close()
