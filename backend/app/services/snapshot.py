"""Builds the engine's input contract from normalised profile_attributes.

This is half of the clean boundary around the search engine: the rest of the app
deals in attribute rows; the engine receives the dict shape full_auto.py expects,
with attribute weights translated into ranking emphasis."""
import re
from collections import defaultdict
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Profile, ProfileAttribute
from .llm import llm_json
from .profile_intel import read_cached_intel

_WORK_TYPES = {"remote", "hybrid", "on-site", "onsite"}


def _parse_salary_floor(values: list[str]) -> int:
    """Lower bound of a stated salary range (e.g. '70000-90000' -> 70000).
    Returns 0 when no range is stated or the floor is 0 (slider default)."""
    for v in values:
        nums = re.findall(r"\d+", (v or "").replace(",", ""))
        if nums:
            return int(nums[0])
    return 0


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


def _labeled(group: list[ProfileAttribute]) -> list[str]:
    """Values annotated with their stated proficiency, e.g. 'Python (expert, 5+ years)'."""
    return [f"{a.value} ({a.proficiency})" if a.proficiency else a.value for a in group]


def _weight_tier(weight: float) -> str | None:
    """Coarse priority label derived from feedback weight (tick/cross history),
    for prompts sent to the cheap gate/rank models -- these only ever see a flat
    attribute list today, so a heavily-ticked or heavily-crossed value carries no
    more signal there than a never-touched one; the embedding pre-filter is the
    only thing weight currently influences. None means neutral -- don't annotate."""
    if weight >= 1.3:
        return "strongly preferred by candidate"
    if weight >= 1.15:
        return "preferred by candidate"
    if weight <= 0.6:
        return "candidate has shown disinterest -- deprioritize"
    if weight <= 0.85:
        return "lower priority for candidate"
    return None


def _weight_tiers(group: list[ProfileAttribute]) -> dict[str, str]:
    """value -> tier, omitting neutral-weight values entirely."""
    out: dict[str, str] = {}
    for a in group:
        tier = _weight_tier(a.weight)
        if tier:
            out[a.value] = tier
    return out


# Depth signal for skill emphasis (Expert/Proficient/Familiar/One-time) and the
# past_role employment-type flag (Informal = student club/volunteer/unpaid, not
# a paid job) -- how many extra times a value is repeated in the embedding text
# on top of the existing feedback-weight multiplier. Substring-matched so it
# still works with values the user typed by hand, not just the parser's output.
_PROFICIENCY_MULT = {
    "expert": 2.0,
    "proficient": 1.5,
    "familiar": 1.0,
    "one-time": 0.5,
    "informal": 0.5,
}


def _proficiency_multiplier(proficiency: str | None) -> float:
    if not proficiency:
        return 1.0
    text = proficiency.lower()
    for key, mult in _PROFICIENCY_MULT.items():
        if key in text:
            return mult
    return 1.0


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


@lru_cache(maxsize=256)
def _cluster_target_roles_cached(roles_key: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """LLM grouping of a role set into clusters, cached for this process's
    lifetime per unique (sorted, deduped) role set -- most profiles never
    change their target roles between searches, so this avoids re-paying the
    call every run. Falls open to a single cluster on anything malformed."""
    roles = list(roles_key)
    data = llm_json(
        f"""Group these job title strings into 1-3 clusters by underlying job
function / career path. STRONGLY prefer returning ONE cluster. Only split
into more when the titles are in genuinely unrelated professional fields
(e.g. "marketing coordinator" vs "nursing assistant") -- NOT for different
specializations, seniority levels, or sub-disciplines within the same
broader field. For example, "CAD Engineering Intern", "Electrical
Engineering Intern", and "Manufacturing Engineering Intern" are all
engineering and belong in ONE cluster, not three. When in doubt, merge
rather than split. Every title must appear in exactly one cluster.
Output ONLY JSON: {{"clusters": [["title a", "title b"], ["title c"]]}}
Titles: {', '.join(roles)}"""
    )
    clusters = data.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        return (tuple(roles),)
    seen: set[str] = set()
    out: list[tuple[str, ...]] = []
    for c in clusters:
        if not isinstance(c, list):
            continue
        members = tuple(r for r in c if isinstance(r, str) and r in roles and r not in seen)
        seen.update(members)
        if members:
            out.append(members)
    missing = [r for r in roles if r not in seen]
    if missing:
        out.append(tuple(missing))
    if not out:
        return (tuple(roles),)
    # The prompt asks for 1-3 clusters, but nothing enforces that on the model's
    # output -- merge any excess into the last cluster rather than let an
    # unbounded cluster count reach the pipeline's per-cluster fan-out. Kept low
    # deliberately: discovery volume is fixed per run (not scaled by cluster
    # count), so more clusters just thins each one's candidate pool.
    MAX_CLUSTERS = 3
    if len(out) > MAX_CLUSTERS:
        overflow = tuple(r for cluster in out[MAX_CLUSTERS:] for r in cluster)
        out = out[:MAX_CLUSTERS - 1] + (out[MAX_CLUSTERS - 1] + overflow,)
    return tuple(out)


def cluster_target_roles(target_roles: list[str]) -> list[list[str]]:
    """Group a profile's target roles into 1-3 clusters of similar job
    function. Downstream (engine.py) scores, gates, and evaluates each
    cluster independently, so a job matching ANY ONE of a candidate's
    distinct role interests can surface on its own merits, instead of being
    judged against a single blended average of all of them (which silently
    penalises candidates targeting more than one field). Fails open to one
    cluster -- today's behavior -- on 0-1 roles or any LLM/parsing issue."""
    roles = [r.strip() for r in target_roles if r and r.strip()]
    if len(roles) <= 1:
        return [roles] if roles else []
    try:
        clusters = _cluster_target_roles_cached(tuple(sorted(set(roles))))
    except Exception:
        return [roles]
    return [list(c) for c in clusters if c]


# Embedding pre-filter is cosine similarity, which rewards a tight, topical
# query -- so it's deliberately narrower than the full profile. target_role is
# the direct signal for "what job"; sector_target adds domain/mission context.
# skill/past_role/qualification/experience used to be blended in too, but a
# generic skill list matches broadly across unrelated postings, and free-text
# experience bullets (unbounded in count, sometimes full sentences) diluted
# the query further the richer a candidate's history was -- exactly backwards,
# since a well-documented candidate should score BETTER, not worse. All of
# that detail still reaches the final AI judge in full (cv_text_base) and
# still shapes the cheap gate/rank prompts (skill_weight_tiers) -- only the
# cosine pre-filter stops using it.
_BASE_EMPHASIS = {"target_role": 3, "sector_target": 2}


def _weighted_text(
    g: dict[str, list[ProfileAttribute]], sectors: list[str], seniority: str,
    search_terms: list[str], role_filter: set[str] | None = None,
) -> str:
    """Weighted emphasis text driving the embedding pre-filter: repeat each
    value roughly in proportion to its learned weight so feedback actually
    shifts results both up (sustained ticks) and down (sustained crosses).
    Previously this used `max(1, round(base * max(1, round(a.weight)) * mult))`
    -- the INNER max(1, round(weight)) alone already floored the effective
    weight at 1 for anything below ~1.5, and the OUTER max(1, ...) floored the
    final count too, so a value crossed all the way down to WEIGHT_MIN (0.1)
    still rounded to the same repeat count as a never-touched 1.0 default --
    crossing could stop future amplification but never actually suppress
    anything. Using the raw weight directly (no inner floor) and allowing the
    final count to reach 0 (no outer floor) lets sustained crosses genuinely
    drop a value out of the embedding text, symmetric with how sustained ticks
    push it up. When role_filter is given, only target_roles in that set are
    included -- this is what lets each role cluster get its own scoped
    embedding text instead of one blend of every target role the candidate has."""
    emphasis: list[str] = []
    for group_name in ("target_role", "sector_target"):
        base = _BASE_EMPHASIS[group_name]
        for a in g.get(group_name, []):
            if group_name == "target_role" and role_filter is not None and a.value.strip() not in role_filter:
                continue
            mult = _proficiency_multiplier(a.proficiency) if group_name != "target_role" else 1.0
            count = max(0, round(base * a.weight * mult))
            emphasis.extend([a.value] * count)
    emphasis.extend(sectors)
    emphasis.append(seniority)
    return " ".join(emphasis) or " ".join(search_terms)


def cv_text_for_cluster(cv_text_base: str, cluster_roles: list[str]) -> str:
    """Scope the synthetic CV's target-roles line to one role cluster, so a
    per-cluster final-evaluation call judges fit against ONE coherent role
    identity instead of every field the candidate has ever listed."""
    if not cluster_roles:
        return cv_text_base
    return f"{cv_text_base}\nTarget roles: {', '.join(cluster_roles)}"


def build_snapshot(db: Session, profile_id: int) -> dict:
    """Return everything the engine run needs, derived from the profile's memory."""
    g = _grouped(db, profile_id)

    target_roles = _values(g.get("target_role", []))
    past_roles = _values(g.get("past_role", []))
    skills = _values(g.get("skill", []))
    qualifications = _values(g.get("qualification", []))
    seniorities = _values(g.get("seniority", []))
    sector_targets = _values(g.get("sector_target", []))
    customs = _values(g.get("custom", []))

    # Location: separate the place from the work-type tokens.
    loc_place, work_types = "", []
    for v in _values(g.get("location", [])):
        if v.lower().strip() in _WORK_TYPES:
            work_types.append(v)
        elif not loc_place:
            loc_place = v
    location = loc_place or "United Kingdom"

    # Search terms sent to Reed/Adzuna/Google/JSearch: target roles only (what
    # they WANT). past_roles used to be appended here too, but that pulls in
    # discovery results matching what the candidate has DONE rather than what
    # they're looking for next -- past_role still feeds the embedding/CV text
    # (_weighted_text/cv_text_base), just not the literal board search query.
    search_terms = []
    for t in target_roles:
        if t not in search_terms:
            search_terms.append(t)
    search_terms = search_terms[:20] or ["jobs"]

    # Location scope: how far the candidate's stated location/country should be
    # trusted as a hard filter. Single-value, like seniority. No row yet ->
    # "national" (today's existing default), UNLESS the profile already has the
    # old "global" country chip set with no scope row, in which case treat that
    # as an explicit "international" opt-out -- this is a code-level fallback
    # for pre-existing profiles, not a migration/backfill.
    scope_values = [v.lower() for v in _values(g.get("location_scope", []))]
    scope = scope_values[0] if scope_values else ""

    # Country: explicit user selection (multi-choice) takes priority over the
    # LLM-inferred guess. Selecting "global" is an explicit opt-out of the hard
    # filter. But NO country row at all must NOT silently mean "Global" -- that
    # was root cause B (the filter sat inert by default, letting out-of-scope
    # roles through). Fail closed: default the filter to the location-derived
    # country unless the user explicitly chose "global" (or scope="international").
    selected_countries = [c.lower() for c in _values(g.get("country", []))]
    explicit_global = "global" in selected_countries
    country_codes = [c for c in selected_countries if c != "global"]

    if not scope:
        scope = "international" if explicit_global else "national"

    if scope == "international":
        region = _infer_region(skills, target_roles + past_roles, location)
        country_codes = []
    elif country_codes:
        region = {"sectors": _infer_region(skills, target_roles + past_roles, location)["sectors"],
                   "adzuna_country_code": country_codes[0]}
    else:
        region = _infer_region(skills, target_roles + past_roles, location)
        # No explicit country chip and scope isn't "international" ->
        # default the hard filter to the country we inferred from their location.
        country_codes = [region["adzuna_country_code"]]
    seniority = ", ".join(seniorities) if seniorities else "mid-level"

    # Salary floor for the hard prefilter: the lower bound of the stated range.
    # 0 (the slider's default min) means "no floor" -> the filter is a no-op.
    salary_floor = _parse_salary_floor(_values(g.get("salary", [])))

    # Role clusters: usually one, but a candidate targeting genuinely different
    # fields (e.g. "marketing" and "research") gets several. The engine scores,
    # gates, and evaluates each cluster independently. Each cluster carries its
    # own weighted_text (scoped to just that cluster's roles) so its embedding
    # isn't diluted by the candidate's other, unrelated target roles.
    role_groups = cluster_target_roles(target_roles)
    role_clusters = [
        {"roles": grp, "weighted_text": _weighted_text(g, region["sectors"], seniority,
                                                        search_terms, role_filter=set(grp))}
        for grp in role_groups
    ]

    # Cached profile-intel artifacts (see profile_intel.py): a header for the final
    # judge's CV text and a candidate-specific requirements checklist for the cheap
    # gate's flexible axis. Pure read, no LLM call -- {} until profile_intel has run
    # at least once for this profile (e.g. before the first search).
    intel = read_cached_intel(db, profile_id)
    header = intel.get("header") or ""
    requirements = intel.get("requirements") or []

    engine_profile = {
        # Scopes full_auto's shared rotation cursor (boards_cache.db is one file
        # for every profile) so concurrent profiles don't share one rotation position.
        "profile_id": profile_id,
        "sectors": region["sectors"],
        "seniority": seniority,
        "key_skills": skills[:10],
        "location": location,
        "adzuna_country_code": region["adzuna_country_code"],
        "country_codes": country_codes,  # [] means no hard filter (Global)
        "location_scope": scope,          # local | national | international
        "local_place": loc_place if scope == "local" else "",
        "salary_floor": salary_floor,     # 0 means no salary floor
        "search_terms": search_terms,
        "role_clusters": role_clusters,   # list[{"roles": [...], "weighted_text": "..."}]
        # value -> priority label, used by the cheap gate/rank prompts (full_auto.py's
        # _screen_prompt/_rank_prompt) to annotate target roles/skills the candidate
        # has ticked or crossed on past results -- so that feedback also has some
        # influence on which roles reach the expensive judge, not just the embedding
        # pre-filter. Omits neutral-weight values entirely (see _weight_tier).
        "target_role_weight_tiers": _weight_tiers(g.get("target_role", [])),
        "skill_weight_tiers": _weight_tiers(g.get("skill", [])),
        # Candidate-specific must-have/must-not-have bullets for screen_gate's
        # requirements_ok axis (see full_auto.py::_screen_prompt). [] means none
        # generated yet or none stated.
        "requirements": requirements,
    }

    weighted_text = _weighted_text(g, region["sectors"], seniority, search_terms)

    # Synthetic CV text for the expensive-AI final evaluation (engine reads a file).
    # cv_text_base omits the "Target roles" line -- see cv_text_for_cluster,
    # which appends it scoped to one role cluster at a time, instead of always
    # listing every target role the candidate has (which invites the judge to
    # weigh fit against all of them at once). Starts with the profile_intel
    # header (a distilled "Looking for X. Must have Y. Must not have Z."
    # synthesis) so it survives _run_final_eval's 5000-char truncation -- kept
    # alongside, not replacing, the verbatim intent_text line below; the
    # redundancy is harmless token cost, not a bug.
    cv_lines = [header] if header else []
    if past_roles:
        cv_lines.append("Past roles: " + ", ".join(_labeled(g.get("past_role", []))))
    if qualifications:
        cv_lines.append("Qualifications: " + "; ".join(qualifications))
    if seniorities:
        cv_lines.append("Seniority: " + ", ".join(seniorities))
    if skills:
        cv_lines.append("Skills: " + ", ".join(_labeled(g.get("skill", []))))
    if sector_targets:
        cv_lines.append("Sector interests: " + "; ".join(sector_targets))
    if location or work_types:
        cv_lines.append(f"Location: {location} ({', '.join(work_types) or 'any'})")
    if customs:
        cv_lines.append("Constraints: " + "; ".join(customs))
    # Extra unstructured context from the original CV, compressed once at upload
    # time (see parsing.py::summarize_cv_text) -- kept short and appended last
    # (before cv_text_for_cluster's per-cluster "Target roles" suffix) so it adds
    # nuance the typed attribute rows above necessarily lose (named projects,
    # leadership scope, domain nuance) without overwhelming the 5000-char budget
    # _run_final_eval truncates cv_text to, which would otherwise risk cutting off
    # that suffix.
    profile = db.get(Profile, profile_id)
    # The candidate's own free-text statement of what they want, given authoritative
    # weight -- this is the direct answer to "what roles is this person actually after",
    # which the typed target_role chips only approximate. Deliberately fed to the final
    # judge (via cv_text_base) but NOT into the cosine embedding text (_weighted_text):
    # a free paragraph would re-diffuse the very centroid the clean target roles sharpen.
    if profile and profile.intent_text:
        cv_lines.append(
            "What the candidate is looking for (in their own words): " + profile.intent_text.strip()
        )
    if profile and profile.cv_summary:
        cv_lines.append("Additional background context: " + profile.cv_summary)
    cv_text_base = "\n".join(cv_lines) or "General candidate."
    cv_text = (
        "\n".join([*cv_lines, f"Target roles: {', '.join(target_roles)}"])
        if target_roles else cv_text_base
    )

    return {
        "engine_profile": engine_profile,
        "weighted_text": weighted_text,
        "cv_text": cv_text,
        "cv_text_base": cv_text_base,
        "role_clusters": role_clusters,
        "skills": skills,
        "seniority_label": seniorities[0] if seniorities else None,
    }
