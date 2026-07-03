"""Builds the engine's input contract from normalised profile_attributes.

This is half of the clean boundary around the search engine: the rest of the app
deals in attribute rows; the engine receives the dict shape full_auto.py expects,
with attribute weights translated into ranking emphasis."""
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ProfileAttribute
from .llm import llm_json

_WORK_TYPES = {"remote", "hybrid", "on-site", "onsite"}


def _grouped(db: Session, profile_id: int) -> dict[str, list[ProfileAttribute]]:
    attrs = db.execute(
        select(ProfileAttribute).where(ProfileAttribute.profile_id == profile_id)
    ).scalars().all()
    grouped: dict[str, list[ProfileAttribute]] = defaultdict(list)
    for a in attrs:
        grouped[a.type].append(a)
    # highest-weight first so emphasis flows through ordering
    for v in grouped.values():
        v.sort(key=lambda a: a.weight, reverse=True)
    return grouped


def _values(group: list[ProfileAttribute]) -> list[str]:
    return [a.value for a in group]


def _infer_region(skills: list[str], roles: list[str], location: str) -> dict:
    """One cheap call to infer sectors + Adzuna country code the engine needs but
    our schema doesn't store directly. Falls back to safe defaults."""
    data = llm_json(
        f"""Given this candidate, return JSON:
{{"sectors": ["2-4 industry domains e.g. software, fintech, marketing"],
  "adzuna_country_code": "2-letter lowercase code: gb,us,ca,za,au,de,fr,in,it,nl,at,pl,sg"}}
Skills: {', '.join(skills) or 'n/a'}
Roles: {', '.join(roles) or 'n/a'}
Location: {location or 'United Kingdom'}"""
    )
    sectors = data.get("sectors") if isinstance(data.get("sectors"), list) else []
    cc = data.get("adzuna_country_code") or "gb"
    return {"sectors": sectors or ["general"], "adzuna_country_code": str(cc).lower()[:2]}


def build_snapshot(db: Session, profile_id: int) -> dict:
    """Return everything the engine run needs, derived from the profile's memory."""
    g = _grouped(db, profile_id)

    target_roles = _values(g.get("target_role", []))
    past_roles = _values(g.get("past_role", []))
    skills = _values(g.get("skill", []))
    experience = _values(g.get("experience", []))
    seniorities = _values(g.get("seniority", []))
    customs = _values(g.get("custom", []))

    # Location: separate the place from the work-type tokens.
    loc_place, work_types = "", []
    for v in _values(g.get("location", [])):
        if v.lower().strip() in _WORK_TYPES:
            work_types.append(v)
        elif not loc_place:
            loc_place = v
    location = loc_place or "United Kingdom"

    # Search terms: target roles lead (what they WANT), past roles follow.
    search_terms = []
    for t in target_roles + past_roles:
        if t not in search_terms:
            search_terms.append(t)
    search_terms = search_terms[:14] or ["jobs"]

    # Country: explicit user selection (multi-choice) takes priority over the
    # LLM-inferred guess. "global" (or no selection) means no hard filter.
    selected_countries = [c.lower() for c in _values(g.get("country", []))]
    country_codes = [c for c in selected_countries if c != "global"]

    if country_codes:
        region = {"sectors": _infer_region(skills, target_roles + past_roles, location)["sectors"],
                   "adzuna_country_code": country_codes[0]}
    else:
        region = _infer_region(skills, target_roles + past_roles, location)
    seniority = ", ".join(seniorities) if seniorities else "mid-level"

    engine_profile = {
        "sectors": region["sectors"],
        "seniority": seniority,
        "key_skills": skills[:10],
        "location": location,
        "adzuna_country_code": region["adzuna_country_code"],
        "country_codes": country_codes,  # [] means no hard filter (Global)
        "search_terms": search_terms,
    }

    # Weighted emphasis text drives the embedding pre-filter: repeat each value
    # roughly in proportion to its learned weight so feedback actually shifts results.
    # target_role gets a baseline lead over skill/past_role so a first-ever search
    # (before any feedback has nudged weights) still embeds toward what the
    # candidate WANTS, not just what they've done/used.
    _BASE_EMPHASIS = {"target_role": 3, "skill": 1, "past_role": 1}
    emphasis: list[str] = []
    for group_name in ("target_role", "skill", "past_role"):
        base = _BASE_EMPHASIS[group_name]
        for a in g.get(group_name, []):
            emphasis.extend([a.value] * base * max(1, round(a.weight)))
    emphasis.extend(region["sectors"])
    emphasis.append(seniority)
    weighted_text = " ".join(emphasis) or " ".join(search_terms)

    # Synthetic CV text for the expensive-AI final evaluation (engine reads a file).
    cv_lines = []
    if past_roles:
        cv_lines.append("Past roles: " + ", ".join(past_roles))
    if seniorities:
        cv_lines.append("Seniority: " + ", ".join(seniorities))
    if skills:
        cv_lines.append("Skills: " + ", ".join(skills))
    if experience:
        cv_lines.append("Experience: " + "; ".join(experience))
    if target_roles:
        cv_lines.append("Target roles: " + ", ".join(target_roles))
    if location or work_types:
        cv_lines.append(f"Location: {location} ({', '.join(work_types) or 'any'})")
    if customs:
        cv_lines.append("Constraints: " + "; ".join(customs))
    cv_text = "\n".join(cv_lines) or "General candidate."

    return {
        "engine_profile": engine_profile,
        "weighted_text": weighted_text,
        "cv_text": cv_text,
        "skills": skills,
        "seniority_label": seniorities[0] if seniorities else None,
    }
