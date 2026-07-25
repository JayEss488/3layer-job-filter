"""Profile intelligence -- two related jobs live here:

1. generate_families() / generate_summary(): the CV-upload SEED path (both called
   in parallel with the parsing extraction call by services/formation.py). Two MID
   calls: one produces the candidate's role FAMILIES (label + titles) directly plus
   a first-person intent draft; the other produces the cv_summary and the
   "Looking for..." header (that whole call is skipped for a short CV -- see
   CV_SHORT_WORD_THRESHOLD -- where the raw text already reads as its own
   summary). Generating families and titles together in one pass is what replaced
   the old "flat target-role list, then a separate re-cluster call" two-step that
   over-split / over-merged them; the families and the summary, by contrast, share
   nothing but the source document, so splitting THOSE apart costs only one extra
   copy of the CV in input tokens and takes the ~1300-token combined generation off
   a single call's critical path (measured: 11.2s for the merged call, vs. ~6s for
   the slower of the two halves).

2. ensure_profile_intel(): the cached REGENERATE path (profile edits, the
   pre-search top-up in engine.py, the manual regenerate). One MID call that
   re-derives a flat target-role list + header + intent draft from the profile's
   current state, which families.ensure_families then slots into the EXISTING
   families. Mirrors harvest.py's Setting-table signature-hash pattern: it skips
   the LLM call entirely when nothing it depends on has changed. On the seed
   path, formation calls store_seeded_intel() to write a matching signature so
   this stays a no-op and doesn't reshuffle the just-seeded families.

The cv_summary is the evidence-focused brief the gates and final judge read
(named projects/tools/outcomes, plus -- now that skills/qualifications are no
longer extracted as chips -- the concrete skill/qualification detail itself). The
candidate-specific requirements checklist that used to be a TASK here is gone,
replaced by candidate_requirements_display() below, which reads the
must_have/avoid attribute rows parsing.py extracts directly."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import CV_SHORT_WORD_THRESHOLD, CV_SUMMARY_MAX_CHARS, MAX_ROLE_CLUSTERS
from ..models import Profile, ProfileAttribute, Setting
from .llm import MID_MODEL, llm_json

PROFILE_INTEL_VERSION = 9
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
    cv_word_count = len((profile.cv_text or "").split()) if profile else 0
    return {
        "profile": profile,
        "by_type": _grouped_values(db, profile_id, _BACKGROUND_TYPES),
        "pinned": _pinned_target_roles(db, profile_id),
        "all_target_roles": _all_target_roles(db, profile_id),
        # A CV/notes document short enough to already read like a summary gets
        # no separate AI-written "Looking for" header (TASK 2) below -- it would
        # just restate the raw text with no compression benefit (cv_summary IS
        # the raw text in this case -- see parsing.py). Empty cv_text (a profile
        # built entirely from typed attributes, no document ever uploaded) is
        # NOT "short": the header is the only narrative signal such a profile
        # has, so it must still be generated.
        "short_cv": bool(cv_word_count) and cv_word_count < CV_SHORT_WORD_THRESHOLD,
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
        "confirmed_target_roles": sorted(v.strip().lower() for v in ctx["pinned"]),
        "all_target_roles": sorted(v.strip().lower() for v in ctx["all_target_roles"]),
        "short_cv": ctx["short_cv"],
        **{t: sorted(v.strip().lower() for v in by_type.get(t, [])) for t in _BACKGROUND_TYPES},
    }, sort_keys=True)
    return hashlib.sha256(basis.encode()).hexdigest()


_TARGET_ROLES_TASK = f"""TARGET ROLES
Silently identify the candidate's genuinely distinct job-function interest(s). Most
candidates have one; some genuinely have two or three (e.g. "data analysis" AND
"policy research" as separate career paths) -- only call out more than one when the
interests are in truly different professional fields, never for different
specialisations, seniority levels, or sub-disciplines within the same broader field
(those all stay ONE interest, e.g. "marketing coordinator" and "brand manager" are
one interest, not two).
For EACH genuine interest, propose a real BREADTH of
distinct, meaningfully different, board-standard job titles that fit within this role family
-- covering different specialisations, closely-adjacent titles, and seniority phrasings that
plausibly apply to this candidate, not just one or two safe picks. Don't
pad with near-duplicates. Return 4 to 8 titles per interest. When the
candidate's academic background or stated aim names a specific discipline (e.g. a
degree in "Electrical and Electronic Engineering", or "seeking a role in X"),
include the discipline's own core job title (e.g. "Electrical Engineer") among the
titles for that interest, not ONLY entry-level modifier variants of it (Intern,
Technician, Placement, Graduate, Trainee).
{_TARGET_ROLE_GUIDANCE}"""

_HEADER_TASK = """HEADER ("LOOKING FOR")
Write a rich, narrative 2-4 sentence description (max 90 words), starting "Looking
for: ...", of what this candidate is looking for NEXT.

Go beyond a bare list of job
titles or sector names -- describe the SHAPE of what they want: level/seniority,
the sector(s) or cause area(s) (if any), and what kind of work actually draws on
their real strengths -- name the specific skill, tool, or type of experience
(events, writing, a self-directed project, a particular analytical or technical
ability) rather than a generic category word for it. If the background suggests
they'd suit an adjacent bridging role -- e.g. translating technical work for a
non-technical audience -- say so explicitly and name the evidence behind it (a
specific piece of writing, a project, a qualification). If the candidate values
breadth/generalist strengths over deep sector-specific experience, or vice versa,
say which and why, using what's actually in the background. Base this ONLY
on what the candidate actually said or the background clearly supports -- never
invent a sector, skill, or requirement that isn't evidenced. Do NOT restate or
recap the background summary -- this is about what they want next and why their
strengths fit it, not a second summary of their history. Do not restate
must-haves/exclusions here -- those are captured separately as structured chips
elsewhere in the profile."""


# The rich descriptive overview the final judge reads. Used to be produced by
# parsing.py's extraction call; now written here alongside the header (in the same
# formation "understand" call) so the two never overlap -- and, crucially, so the
# concrete skill/qualification/informal-experience detail that is no longer
# extracted as its own chips still reaches the judge through this text.
_SUMMARY_TASK = (
    "BACKGROUND SUMMARY\n"
    "Write a rich, descriptive overview (max 450 words) of the candidate for another AI "
    "that will later judge job-fit, built from the document above. Go beyond a skills "
    "inventory -- give a fuller picture covering whichever of these the document supports: "
    "(1) one sentence grounding seniority concretely -- qualification/stage plus real "
    "evidence of where the candidate actually is, not just a generic seniority word; "
    "(2) PAID work experience (employer/role and what they actually did, even if brief or "
    "seasonal); (3) named projects with concrete specifics (what was built, with what tools, "
    "what the outcome was) and, for each skill/tool the document names, the SPECIFIC project "
    "or context it came from; (4) leadership, society, volunteering, or other extracurricular "
    "involvement, named specifically; (5) any writing, creative, or communication work; "
    "(6) languages or other differentiators. Preserve any honesty caveat the candidate stated "
    "about their own work (e.g. 'AI-assisted', 'self-taught', 'not yet used professionally', "
    "'informal') verbatim in meaning -- never smooth one into a more confident-sounding claim. "
    "This is where the concrete skill and qualification detail now lives (they are no longer "
    "separate profile fields), so don't drop it. Be faithful to the source -- never invent or "
    "embellish a detail that isn't in the document. If the document gives nothing concrete "
    "beyond a bare skills/role list, return a short paragraph instead of forcing this structure.\n\n"
    "When trimming to fit the word limit, prioritize by relevance to what the candidate is "
    "stated to want next (their target direction/header), not by recency or the order things "
    "appear in the source -- an in-progress or unfinished project that is the single most "
    "relevant piece of evidence for their stated direction must not be dropped in favor of a "
    "smaller, less relevant one just because it's tidier to describe. If the source itself "
    "singles an experience out with its own emphasis or ranking language (e.g. calls something "
    "its 'strongest', 'proudest', or 'most significant' example of a skill), preserve that "
    "emphasis rather than flattening it into an undifferentiated list item alongside everything "
    "else. Preserve concrete quantitative evidence attached to a claim -- view counts, vote/result "
    "counts, audience size, or other named metrics -- and a short challenge-then-response "
    "narrative when the source describes one (e.g. a setback and how it was handled), rather "
    "than compressing either down to a bare noun phrase that loses the evidence."
)


_FAMILIES_TASK = f"""ROLE FAMILIES (the candidate's search streams)
Identify the candidate's genuine job-search direction(s), and for EACH one propose the
job titles to search for. A "family" is ONE coherent job search: roles the candidate would
apply to with the same CV, sharing the same core day-to-day work.

Group aggressively -- MOST candidates have exactly ONE family, so default to one:
- Create a second (or, rarely, third) family ONLY when the candidate clearly pursues
  genuinely DIFFERENT professions -- different day-to-day tasks, teams, and CV framing, where
  a listing ideal for one would be irrelevant to the other (e.g. "Communications/PR" vs
  "Editorial/proofreading" vs "Policy research"). At most 3 families; if the material genuinely
  spans more than 3 distinct professions, keep the 3 best-evidenced and drop the rest.
- The SAME family covers different seniorities (Junior/Senior/Lead), entry-level modifiers
  (Intern/Graduate/Trainee/Assistant/Associate), specialisations, and closely-adjacent titles
  of one function. For example "Data Analyst", "Operations Analyst", "Product Analyst", and
  "BI Analyst" are all ONE family (analysing data and reporting) -- a specialisation, or a
  different noun before "Analyst", is NOT a different profession. NEVER split a family by
  seniority or specialisation; judge by the actual work, not the label.

For EACH family:
- Give a short label naming the FIELD/function in 2-3 words (e.g. "Data Analytics",
  "Electrical Engineering"), not a seniority ("Graduate Analyst") and not a slashed compound.
- Propose 4-8 distinct, board-standard job titles that genuinely belong to it, covering its
  real breadth of specialisations and seniority phrasings. These become the literal search
  terms that seed discovery, so choose titles a job board would actually return good matches
  for; don't pad with near-duplicates, and never return a family with only one title.
- For an early-career/student candidate targeting a specific discipline, keep ONE family for
  that discipline but you may include a few closely-adjacent disciplines they'd plausibly
  accept (e.g. an electrical-engineering student: mostly electrical/electronics titles plus a
  couple of general engineering-intern titles), since early-career searches run broader.
{_CLEAN_TITLE_RULE}"""


def _intent_block(intent_text: str, what: str) -> str:
    """The candidate's own words, when they gave any. `what` names whichever
    output(s) the surrounding prompt is asking for, so each half of the split
    formation pair points this at its own task rather than at the other's."""
    return (
        "The candidate has also stated, in their own words, what they are looking for -- treat "
        f"this as the PRIMARY signal for the {what} below:\n"
        f'"{intent_text.strip()}"\n\n'
        if intent_text.strip() else ""
    )


def _families_prompt(
    text: str, intent_text: str, intent_missing: bool, want_intent_draft: bool = True
) -> str:
    """Half one of the formation pair: role families (+ their titles) and a
    first-person intent draft. Both are short outputs about what the candidate
    WANTS, which is why they stayed together while the long descriptive summary
    moved to its own call.

    want_intent_draft=False drops the intent task and its field entirely -- see
    generate_families for why the short-document path doesn't want one."""
    intent_task = (
        "Draft one 2-3 sentence first-person statement of what kind of roles, sectors, and "
        "level this candidate is after, based only on the document above."
        if intent_missing else
        'The candidate already has an intent statement -- return "" for this field, it will be ignored.'
    )
    tasks = [_FAMILIES_TASK]
    fields = ['"families": [{"label": "...", "roles": ["...", "..."]}]']
    if want_intent_draft:
        tasks.append(f"INTENT DRAFT\n{intent_task}")
        fields.append('"intent_draft": "..."')
    numbered = "\n\n".join(f"TASK {i} -- {t}" for i, t in enumerate(tasks, start=1))
    return f"""Candidate's CV / notes:
{text[:40000]}

{_intent_block(intent_text, "families")}{numbered}

Return ONLY JSON: {{{', '.join(fields)}}}"""


def _summary_prompt(text: str, intent_text: str) -> str:
    """Half two of the formation pair: the evidence brief the gates/judge read,
    plus the "Looking for..." header. Never built at all on the short-CV path --
    formation skips the whole call there (the raw text is stored verbatim as the
    summary), so this is strictly the long-document case."""
    tasks = [_SUMMARY_TASK, _HEADER_TASK]
    fields = ['"cv_summary": "..."', '"header": "..."']
    numbered = "\n\n".join(f"TASK {i} -- {t}" for i, t in enumerate(tasks, start=1))
    return f"""Candidate's CV / notes:
{text[:40000]}

{_intent_block(intent_text, "header")}{numbered}

Return ONLY JSON: {{{', '.join(fields)}}}"""


def generate_summary(text: str, intent_text: str | None) -> dict:
    """Pure MID LLM call producing the cv_summary + "Looking for..." header.
    DB-free so formation can run it in a thread alongside the families and
    extraction calls. Returns {} on a call failure -- unlike a families failure
    this is NOT fatal to the formation (the caller keeps the families and simply
    stores no summary), so it deliberately doesn't use None as a sentinel."""
    if not text or not text.strip():
        return {}
    data = llm_json(_summary_prompt(text, intent_text or ""), model=MID_MODEL)
    if not isinstance(data, dict):
        return {}
    return {
        "cv_summary": str(data.get("cv_summary") or "").strip()[:CV_SUMMARY_MAX_CHARS],
        "header": str(data.get("header") or "").strip(),
    }


def generate_families(
    text: str, intent_text: str | None, *, want_intent_draft: bool = True
) -> dict | None:
    """Pure MID LLM call producing the candidate's role families (label + titles)
    and a first-person intent draft. DB-free so formation can run it in a thread
    alongside the summary and extraction calls. Returns None on a call failure or
    when no usable families came back (a transient failure, distinguishable from a
    genuinely empty profile so the caller leaves existing state untouched).

    want_intent_draft=False (the short-document path -- see formation._is_short_cv)
    skips the draft altogether. A short CV gives the model too little to paraphrase
    honestly, so the draft comes back as a restatement of the extracted titles that
    then reaches the final judge as "the candidate's OWN words" (snapshot.py ranks
    intent_text above everything else) and quietly widens the search: a live run
    drafted "...with openness to roles involving automation" off a data/software CV,
    which is one of the things that let an industrial-controls role through as a top
    pick. An empty box the candidate can optionally fill is strictly better than a
    confident guess they never wrote."""
    if not text or not text.strip():
        return None
    intent_missing = not (intent_text or "").strip()
    data = llm_json(
        _families_prompt(text, intent_text or "", intent_missing,
                         want_intent_draft=want_intent_draft),
        model=MID_MODEL,
    )
    raw_families = data.get("families") if isinstance(data.get("families"), list) else []
    families: list[dict] = []
    for fam in raw_families:
        if not isinstance(fam, dict):
            continue
        label = str(fam.get("label") or "").strip()
        roles = [str(r).strip() for r in (fam.get("roles") or []) if str(r).strip()]
        seen: set[str] = set()
        deduped: list[str] = []
        for r in roles:
            if r.lower() not in seen:
                seen.add(r.lower())
                deduped.append(r)
        if label and deduped:
            families.append({"label": label, "roles": deduped[:TARGET_ROLE_HARD_CAP]})
    if not families:
        return None  # transient failure -- caller must leave state untouched
    return {
        "families": families[:MAX_ROLE_CLUSTERS],
        "intent_draft": str(data.get("intent_draft") or "").strip(),
    }


def _prompt(background: str, pinned: list[str], intent_missing: bool, short_cv: bool) -> str:
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
    # A short CV/notes document already reads like a summary on its own (see
    # parsing.py's CV_SHORT_WORD_THRESHOLD handling) -- a separately-generated
    # "Looking for" paraphrase of the same background would add compression with
    # no benefit, so skip TASK 2 and the "header" field entirely rather than pay
    # for a narrative restatement of text short enough to just read directly. The
    # same short-document flag also drops the INTENT DRAFT task, for the reason in
    # generate_families' docstring -- a guessed statement of what the candidate
    # wants reaches the judge as if they'd written it. This is the regenerate/edit
    # path; formation.py's first-parse path suppresses it the same way.
    tasks = [_TARGET_ROLES_TASK]
    if not short_cv:
        tasks.append(_HEADER_TASK)
        tasks.append(f"INTENT DRAFT\n{intent_task}")
    numbered_tasks = "\n\n".join(f"TASK {i} -- {t}" for i, t in enumerate(tasks, start=1))
    header_field = "" if short_cv else ', "header": "..."'
    intent_field = "" if short_cv else ', "intent_draft": "..."'
    return f"""Candidate background:
{background or 'No background on file yet.'}

{pinned_block}

{numbered_tasks}

Return ONLY JSON: {{"target_roles": ["..."]{header_field}{intent_field}}}"""


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
    data = llm_json(_prompt(background, pinned, intent_missing, ctx["short_cv"]), model=MID_MODEL)

    target_roles = data.get("target_roles") if isinstance(data.get("target_roles"), list) else []
    target_roles = [str(t).strip() for t in target_roles if str(t).strip()][:TARGET_ROLE_HARD_CAP]
    if not target_roles:
        return None  # transient call failure -- distinguishable from "genuinely no roles"

    return {
        "target_roles": target_roles,
        "header": str(data.get("header") or "").strip(),
        "intent_draft": str(data.get("intent_draft") or "").strip(),
    }


def _apply(
    db: Session, profile_id: int, data: dict, *, regenerate_roles: bool
) -> list[ProfileAttribute]:
    """Delete-unconfirmed/keep-confirmed/dedupe, same shape as the old
    regenerate_target_roles endpoint, plus autofilling intent_text only if empty.

    regenerate_roles=False (the automatic pre-search top-up -- see
    ensure_profile_intel) skips the target_role delete+insert entirely: once a
    profile has role families, those own the target-role list (seeded once,
    refreshed only by an explicit regenerate action -- see families.py's module
    docstring), so an automatic call here must never wipe them as a side effect
    of an unrelated signature change (e.g. deleting an unrelated family used to
    silently regenerate every OTHER family's roles too). The intent_draft
    autofill still runs either way -- it only ever fires once (intent_text
    empty), so there's nothing destructive to gate there."""
    new_attrs: list[ProfileAttribute] = []
    if regenerate_roles:
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


def candidate_requirements_display(db: Session, profile_id: int) -> list[str]:
    """The candidate's own must_have/avoid chip values, verbatim -- replaces the
    old TASK 3 LLM re-derivation (profile_intel used to ask the model to blindly
    re-guess this same signal from the raw CV text, never even shown the
    must_have/avoid rows parsing.py already extracts directly). Single source
    now for both the weak gate's soft requirements axis (snapshot.py) and the
    Memory-page "requirements the AI screens on" display (onboarding.py)."""
    by_type = _grouped_values(db, profile_id, ["must_have", "avoid"])
    return (
        [f"Requires: {v}" for v in by_type.get("must_have", [])]
        + [f"Avoid: {v}" for v in by_type.get("avoid", [])]
    )


def store_seeded_intel(db: Session, profile_id: int, header: str) -> None:
    """Write the profile-intel cache (header result + current-state signature)
    without running the generation call -- for the formation seed path, whose
    "understand" call already produced the header AND the target roles. Storing a
    matching signature makes the pre-search ensure_profile_intel (engine.py) and
    the next edit-free call a clean no-op, so they don't re-generate roles and
    reshuffle the freshly-seeded families. Must run AFTER all formation writes
    have flushed so the signature reflects them. Does not commit -- formation owns
    the transaction."""
    ctx = _context(db, profile_id)
    sig = _signature(ctx)
    result_json = json.dumps({"header": (header or "").strip()})
    result_row = _setting(db, profile_id, RESULT_KEY)
    if result_row is not None:
        result_row.value = result_json
    else:
        db.add(Setting(profile_id=profile_id, key=RESULT_KEY, value=result_json))
    sig_row = _setting(db, profile_id, SIG_KEY)
    if sig_row is not None:
        sig_row.value = sig
    else:
        db.add(Setting(profile_id=profile_id, key=SIG_KEY, value=sig))


def read_cached_intel(db: Session, profile_id: int) -> dict:
    """Pure read (no LLM) of the cached {"header": str, "header_locked": bool}
    -- called every build_snapshot run, zero cost when nothing has changed. {}
    if profile_intel has never run for this profile."""
    row = _setting(db, profile_id, RESULT_KEY)
    if not row or not row.value:
        return {}
    try:
        return json.loads(row.value)
    except (TypeError, ValueError):
        return {}


def set_header_locked(db: Session, profile_id: int, header: str) -> None:
    """Manual edit of the AI-generated header (Memory page's "What the AI reads
    about you" panel) -- persists it and marks it locked so the next
    ensure_profile_intel call (automatic or forced) preserves this text instead
    of silently overwriting it with a freshly regenerated one. Only a full CV
    re-parse (store_seeded_intel, which starts the cache fresh for a new
    document) clears the lock."""
    result_json = json.dumps({"header": header.strip(), "header_locked": True})
    result_row = _setting(db, profile_id, RESULT_KEY)
    if result_row is not None:
        result_row.value = result_json
    else:
        db.add(Setting(profile_id=profile_id, key=RESULT_KEY, value=result_json))
    db.commit()


def ensure_profile_intel(
    db: Session, profile_id: int, *, force: bool = False, regenerate_roles: bool = True
) -> list[ProfileAttribute]:
    """Regenerate header/intent-draft (and, when regenerate_roles=True, the flat
    target-role list) only when the profile's actual inputs changed since the
    last run (or force=True). Returns newly-created (unconfirmed) target_role
    rows -- [] when skipped, on failure, or when regenerate_roles=False.

    regenerate_roles=False is the automatic pre-search top-up (engine.py): it
    still refreshes the header (so a deleted/renamed family's stale mention
    doesn't linger -- see _all_target_roles) but never touches target_role
    rows, so it can't wipe or reshuffle a profile's existing role families as a
    side effect of an unrelated signature change (this used to happen: deleting
    one family invalidated the cache, which regenerated EVERY family's roles as
    a flat, family-unaware list and could reintroduce a just-deleted cluster).
    The explicit "Regenerate target roles" action (onboarding.py) keeps
    regenerate_roles=True (the default), preserving its original intentional
    wipe-and-reslot-into-families behaviour.

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

    new_attrs = _apply(db, profile_id, data, regenerate_roles=regenerate_roles)

    # A manually-edited header (see set_header_locked) is never silently
    # replaced by this regeneration, independent of whether roles were also
    # regenerated this call.
    existing = read_cached_intel(db, profile_id)
    if existing.get("header_locked"):
        result_json = json.dumps({"header": existing.get("header", ""), "header_locked": True})
    else:
        result_json = json.dumps({"header": data["header"]})
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
