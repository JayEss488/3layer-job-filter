"""Cached "profile intelligence": one LLM call per meaningful profile edit that
expands target roles, drafts a short CV header ("Looking for X. Must have Y. Must
not have Z.") for the final judge, a candidate-specific requirements checklist
for the cheap gate's flexible axis, and an evidence-focused analytical brief
(named projects/tools, not just tier labels) fed to the cheap/mid gates and the
final judge alike -- plus, only when empty, a starter draft of intent_text.

Mirrors harvest.py's Setting-table signature-hash pattern: skip the LLM call
entirely when nothing this call depends on has changed since the last run."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..models import Profile, ProfileAttribute, Setting
from .llm import STRONG_MODEL, llm_json

PROFILE_INTEL_VERSION = 4
SIG_KEY = "profile_intel_signature"
RESULT_KEY = "profile_intel_result"
TARGET_ROLE_HARD_CAP = 20

_BACKGROUND_TYPES = ["past_role", "skill", "qualification", "seniority", "sector_target", "custom"]

# Titles must be board-queryable: they become literal search terms and the cosine
# embedding anchor, so parenthetical asides / slashes / sector clauses inside a title
# both mangle board queries and diffuse the embedding centroid. Sector/mission context
# belongs in sector_target, never baked into the role title.
_CLEAN_TITLE_RULE = (
    "Each title must be a short, standard job title a job board would recognise (2-4 "
    "words, e.g. 'Policy Research Assistant', 'Data Analyst', 'Communications Officer'). "
    "NO parentheses, slashes, sector asides, or 'X / Y' compounds inside the title "
    "itself -- keep sector/mission/cause context out of the title (it is captured "
    "separately as sector_target)."
)

_TARGET_ROLE_GUIDANCE = (
    "Weigh the candidate's full picture -- leadership/project experience, applied "
    "use of tools, academic background, languages, communications/organisational "
    "work -- as heavily as any skills list. Do not default to generic titles that "
    "only match a skills-inventory section if stronger, more differentiated, "
    "better-evidenced titles fit their actual background. If the candidate has "
    "stated explicit sector, industry, or cause-targeting language (cover-letter "
    "angles, named industries/organisations they're drawn to), also propose titles "
    "reflecting those sectors specifically -- don't let a generic skills-first "
    "framing crowd those out. " + _CLEAN_TITLE_RULE
)


def _grouped_values(db: Session, profile_id: int, types: list[str]) -> dict[str, list[str]]:
    attrs = db.execute(
        select(ProfileAttribute).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type.in_(types),
        )
    ).scalars().all()
    by_type: dict[str, list[str]] = {}
    for a in attrs:
        by_type.setdefault(a.type, []).append(a.value)
    return by_type


def _pinned_target_roles(db: Session, profile_id: int) -> list[str]:
    return db.execute(
        select(ProfileAttribute.value).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type == "target_role",
            ProfileAttribute.confirmed.is_(True),
        )
    ).scalars().all()


def _all_target_roles(db: Session, profile_id: int) -> list[str]:
    """Every target_role value regardless of confirmed state -- used only in
    the signature (see _signature below), not the generation prompt itself.
    Deleting a whole role family (services/families.py::delete_family) mostly
    removes unconfirmed/ai_suggested rows, which _pinned_target_roles alone
    would never notice -- that left the cached header describing tracks the
    candidate had just deleted. Needs the full set so any target_role
    addition/deletion, confirmed or not, invalidates the cache."""
    return db.execute(
        select(ProfileAttribute.value).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type == "target_role",
        )
    ).scalars().all()


def _context(db: Session, profile_id: int) -> dict:
    """Everything the generation prompt (and its cache signature) depends on --
    computed once per call so signature-checking and generation never disagree
    about what "the current inputs" are."""
    profile = db.get(Profile, profile_id)
    return {
        "profile": profile,
        "by_type": _grouped_values(db, profile_id, _BACKGROUND_TYPES),
        "pinned": _pinned_target_roles(db, profile_id),
        "all_target_roles": _all_target_roles(db, profile_id),
        # Raw CV/notes text, not the compressed cv_summary -- only the analytical
        # brief task (TASK 5) needs this, for the concrete named-project/tool
        # detail that summarize_cv_text's 100-word compression already drops.
        "cv_text": ((profile.cv_text or "").strip()[:12000] if profile else ""),
    }


def _background_text(by_type: dict[str, list[str]]) -> str:
    parts = []
    if by_type.get("past_role"):
        parts.append("Past roles: " + ", ".join(by_type["past_role"]))
    if by_type.get("qualification"):
        parts.append("Qualifications: " + "; ".join(by_type["qualification"]))
    if by_type.get("skill"):
        parts.append("Skills: " + ", ".join(by_type["skill"]))
    if by_type.get("seniority"):
        parts.append("Seniority: " + ", ".join(by_type["seniority"]))
    if by_type.get("sector_target"):
        parts.append("Sector interests: " + "; ".join(by_type["sector_target"]))
    if by_type.get("custom"):
        parts.append("Other preferences: " + "; ".join(by_type["custom"]))
    return "\n".join(parts)


def _signature(ctx: dict) -> str:
    """Stable hash of exactly what the prompt below consumes -- when any of it
    changes the cache naturally misses and profile intel gets regenerated."""
    profile = ctx["profile"]
    by_type = ctx["by_type"]
    basis = json.dumps({
        "version": PROFILE_INTEL_VERSION,
        "intent_text": ((profile.intent_text or "").strip() if profile else ""),
        "cv_summary": ((profile.cv_summary or "").strip() if profile else ""),
        # Belt-and-suspenders: a fresh CV upload already changes cv_summary and
        # re-parsed attributes too, but hashing the raw text directly means a
        # changed cv_text alone is never missed even if those happen to collide.
        "cv_text": ctx["cv_text"],
        "confirmed_target_roles": sorted(v.strip().lower() for v in ctx["pinned"]),
        "all_target_roles": sorted(v.strip().lower() for v in ctx["all_target_roles"]),
        **{t: sorted(v.strip().lower() for v in by_type.get(t, [])) for t in _BACKGROUND_TYPES},
    }, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()


def _prompt(background: str, pinned: list[str], intent_missing: bool, cv_text: str) -> str:
    pinned_block = (
        "The candidate has already PINNED these target roles -- do not repeat or reword "
        "them, only propose complementary/additional titles:\n" + "\n".join(f"- {r}" for r in pinned)
        if pinned else "None pinned yet."
    )
    intent_task = (
        "Draft one 2-3 sentence first-person statement of what kind of roles, sectors, "
        "and level this candidate is after, based only on the background above."
        if intent_missing else
        'The candidate already has an intent statement -- return "" for this field, it will be ignored.'
    )
    brief_task = (
        f"""

TASK 5 -- ANALYTICAL BRIEF
Write a dense, evidence-focused brief (max 220 words) for another AI that will be
judging job-fit, using the ORIGINAL CV/notes text below (not the summarized
background above) -- it has detail a normalised skills list drops. Structure:
1. One sentence grounding seniority concretely -- qualification/stage plus real
   evidence of where the candidate actually is (e.g. "BSc Physics, First Class,
   July 2026 -- no full-time analyst role yet, but multiple completed data
   projects"), not just a generic seniority word.
2. One evidence bullet per skill/tool the CV text actually names, citing the
   SPECIFIC project, dataset, technology, or outcome involved (e.g. "SQL: wrote
   extraction queries via Python/psycopg2 against live production data
   (Supabase) -- AI-assisted, not from scratch, but functional and used in a
   real pipeline"). Skip a skill entirely if the source gives nothing concrete
   to cite -- do not pad with a generic restatement of the skill name.
3. One "Evidence style" line characterising the pattern across the evidence
   (e.g. self-directed personal projects with real data vs. coursework vs.
   professional/commercial work).
Be faithful to the source -- never invent or embellish a detail that isn't in
the CV text below. If the CV text is empty or gives nothing concrete beyond
what's already in the background above, return "" for this field.

Original CV/notes text:
{cv_text[:12000] or 'None on file.'}"""
        if cv_text else
        '\n\nTASK 5 -- ANALYTICAL BRIEF\nNo original CV/notes text on file -- return "" for this field.'
    )
    return f"""Candidate background:
{background or 'No background on file yet.'}

{pinned_block}

TASK 1 -- TARGET ROLES
Silently identify the candidate's genuinely distinct job-function interest(s). Most
candidates have one; some genuinely have two or three (e.g. "data analysis" AND
"policy research" as separate career paths) -- only call out more than one when the
interests are in truly different professional fields, never for different
specialisations, seniority levels, or sub-disciplines within the same broader field
(those all stay ONE interest, e.g. "marketing coordinator" and "brand manager" are
one interest, not two). For EACH genuine interest, propose a real BREADTH of
distinct, meaningfully different, board-standard job titles that fit it -- covering
different specialisations, closely-adjacent titles, and seniority phrasings that
plausibly apply to this candidate, not just one or two safe picks. Two or three
titles for a genuine interest is usually too narrow unless the candidate's own
background is itself that narrow -- don't under-generate out of caution. Still don't
pad with near-duplicates, and don't invent extra job-function interests just to
generate more titles. Never return more than 20 titles total, combined across all
interests -- treat that as a safety ceiling, not a target to reach.
{_TARGET_ROLE_GUIDANCE}

TASK 2 -- HEADER
One line (max 40 words), of the form "Looking for: <summary>. Must have: <X>. Must
not have: <Y>." Omit a clause entirely if the candidate stated nothing for it. Base
this ONLY on what the candidate actually said -- never invent a requirement.

TASK 3 -- CANDIDATE-SPECIFIC REQUIREMENTS
0-5 short bullets of the candidate's OWN hard must-haves/exclusions, distinct from
sector/domain fit and seniority level (both already screened separately elsewhere --
do not repeat those here). Only include something explicitly stated (e.g. "must be
fully remote", "no cold-calling/sales roles", "no relocation"). Empty list if none.

TASK 4 -- INTENT DRAFT
{intent_task}
{brief_task}

Return ONLY JSON: {{"target_roles": ["..."], "header": "...", "requirements": ["..."], "intent_draft": "...", "analytical_brief": "..."}}"""


def _generate(ctx: dict) -> dict | None:
    """One llm_json call. Returns None on failure or when there's nothing to go
    on -- callers must leave existing state untouched in that case, not "succeed"
    with an empty result."""
    profile = ctx["profile"]
    pinned = ctx["pinned"]
    background_parts = []
    if profile and (profile.intent_text or "").strip():
        background_parts.append(
            "The candidate has stated, in their own words, what they are looking for -- "
            "treat this as the PRIMARY signal:\n"
            f'"{profile.intent_text.strip()}"'
        )
    if profile and (profile.cv_summary or "").strip():
        background_parts.append("Background summary: " + profile.cv_summary.strip())
    background_parts.append(_background_text(ctx["by_type"]))
    background = "\n\n".join(p for p in background_parts if p)

    if not background.strip() and not pinned:
        return None  # nothing to derive anything from

    intent_missing = not (profile and (profile.intent_text or "").strip())
    data = llm_json(_prompt(background, pinned, intent_missing, ctx["cv_text"]), model=STRONG_MODEL)

    target_roles = data.get("target_roles") if isinstance(data.get("target_roles"), list) else []
    target_roles = [str(t).strip() for t in target_roles if str(t).strip()][:TARGET_ROLE_HARD_CAP]
    if not target_roles:
        return None  # transient call failure -- distinguishable from "genuinely no roles"

    requirements = data.get("requirements") if isinstance(data.get("requirements"), list) else []
    return {
        "target_roles": target_roles,
        "header": str(data.get("header") or "").strip(),
        "requirements": [str(r).strip() for r in requirements if str(r).strip()][:5],
        "intent_draft": str(data.get("intent_draft") or "").strip(),
        "analytical_brief": str(data.get("analytical_brief") or "").strip(),
    }


def _apply(db: Session, profile_id: int, data: dict) -> list[ProfileAttribute]:
    """Delete-unconfirmed/keep-confirmed/dedupe, same shape as the old
    regenerate_target_roles endpoint, plus autofilling intent_text only if empty."""
    db.execute(
        delete(ProfileAttribute).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type == "target_role",
            ProfileAttribute.confirmed.is_(False),
        )
    )
    kept = db.execute(
        select(ProfileAttribute.value).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type == "target_role",
        )
    ).scalars().all()
    kept_lower = {v.strip().lower() for v in kept}
    new_attrs: list[ProfileAttribute] = []
    for value in data["target_roles"]:
        if value.lower() in kept_lower:
            continue
        kept_lower.add(value.lower())
        attr = ProfileAttribute(
            profile_id=profile_id, type="target_role", value=value,
            source="ai_suggested", confirmed=False,
        )
        db.add(attr)
        new_attrs.append(attr)
    db.flush()

    if data.get("intent_draft"):
        profile = db.get(Profile, profile_id)
        if profile is not None and not (profile.intent_text or "").strip():
            profile.intent_text = data["intent_draft"]

    return new_attrs


def _setting(db: Session, profile_id: int, key: str) -> Setting | None:
    return db.execute(
        select(Setting).where(Setting.profile_id == profile_id, Setting.key == key)
    ).scalar_one_or_none()


def read_cached_intel(db: Session, profile_id: int) -> dict:
    """Pure read (no LLM) of the cached {"header": str, "requirements": [str],
    "analytical_brief": str} -- called every build_snapshot run, zero cost when
    nothing has changed. {} if profile_intel has never run for this profile."""
    row = _setting(db, profile_id, RESULT_KEY)
    if not row or not row.value:
        return {}
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return {}


def ensure_profile_intel(db: Session, profile_id: int, *, force: bool = False) -> list[ProfileAttribute]:
    """Regenerate target roles/header/requirements/intent-draft only when the
    profile's actual inputs changed since the last run (or force=True). Returns
    newly-created (unconfirmed) target_role rows -- [] when skipped or on failure.

    On a failed/empty generation, existing rows and the cached result are left
    untouched and the signature marker is NOT advanced, so it's retried on the
    next call instead of "succeeding" with nothing cached."""
    ctx = _context(db, profile_id)
    sig = _signature(ctx)
    sig_row = _setting(db, profile_id, SIG_KEY)
    if not force and sig_row is not None and sig_row.value == sig:
        return []

    data = _generate(ctx)
    if data is None:
        return []

    new_attrs = _apply(db, profile_id, data)

    result_json = json.dumps({
        "header": data["header"],
        "requirements": data["requirements"],
        "analytical_brief": data["analytical_brief"],
    })
    result_row = _setting(db, profile_id, RESULT_KEY)
    if result_row is not None:
        result_row.value = result_json
    else:
        db.add(Setting(profile_id=profile_id, key=RESULT_KEY, value=result_json))

    if sig_row is not None:
        sig_row.value = sig
    else:
        db.add(Setting(profile_id=profile_id, key=SIG_KEY, value=sig))

    db.commit()
    for a in new_attrs:
        db.refresh(a)
    return new_attrs
