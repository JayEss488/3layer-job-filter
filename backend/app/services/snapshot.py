"""Builds the engine's input contract from normalised profile_attributes.

This is half of the clean boundary around the search engine: the rest of the app
deals in attribute rows; the engine receives the dict shape full_auto.py expects,
with attribute weights translated into ranking emphasis."""
import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (
    DEFAULT_COMMUTE_MILES,
    DEFAULT_MAX_LISTING_AGE_DAYS,
    DEFAULT_VISA_SPONSOR_MIN_SALARY,
    MAX_ROLE_CLUSTERS,
    PINNED_ROLE_MULT,
    WORK_TYPE_VALUES,
    enforcement_for,
)
from ..models import Profile, ProfileAttribute
from .families import cluster_target_roles, list_families, ordered_for_engine
from .llm import llm_json
from .profile_intel import candidate_requirements_display, read_cached_intel

_WORK_TYPES = WORK_TYPE_VALUES


def _parse_salary_floor(values: list[str]) -> int:
    """Lower bound of a stated salary range (e.g. '70000-90000' -> 70000).
    Returns 0 when no range is stated or the floor is 0 (slider default)."""
    for v in values:
        nums = re.findall(r"\d+", (v or "").replace(",", ""))
        if nums:
            return int(nums[0])
    return 0


def _parse_max_listing_age_days(values: list[str]) -> int:
    """First row's value as a whole number of days, or
    DEFAULT_MAX_LISTING_AGE_DAYS when the candidate has never set the
    preference. 0 is a real, meaningful value (the candidate's own "No limit"
    choice) and must not be treated the same as "no row at all"."""
    if not values:
        return DEFAULT_MAX_LISTING_AGE_DAYS
    try:
        return max(0, int(str(values[0]).strip()))
    except (TypeError, ValueError):
        return DEFAULT_MAX_LISTING_AGE_DAYS


def _parse_commute_miles(values: list[str]) -> int:
    """The candidate's commute radius in miles, or DEFAULT_COMMUTE_MILES when
    they've never set it. 0 is a real value (their own "No limit" choice) and
    must not be confused with "no row at all" -- same rule as
    _parse_max_listing_age_days above."""
    if not values:
        return DEFAULT_COMMUTE_MILES
    try:
        return max(0, int(str(values[0]).strip()))
    except (TypeError, ValueError):
        return DEFAULT_COMMUTE_MILES


def _parse_visa_sponsor_only(values: list[str]) -> bool:
    """Whether to restrict results to licensed visa sponsors.

    Note the asymmetry with the two parsers above: here "no row at all" and a
    falsey row mean the same thing (off), because the preference has no
    meaningful third state. Written as an explicit truthy-value check rather
    than bool(values) so a stale "false" row left behind by a UI change reads as
    off rather than as on."""
    return bool(values) and str(values[0]).strip().lower() in ("true", "1", "yes")


def _parse_visa_sponsor_min_salary(values: list[str]) -> int:
    """The minimum annual salary (GBP) a role must clear to count as sponsorable,
    or DEFAULT_VISA_SPONSOR_MIN_SALARY when the candidate has never set it. 0 is
    a real value (the candidate's own "no floor" choice, e.g. relying on a
    reduced-rate category this app doesn't try to detect) and must not be
    confused with "no row at all" -- same rule as _parse_max_listing_age_days /
    _parse_commute_miles above."""
    if not values:
        return DEFAULT_VISA_SPONSOR_MIN_SALARY
    try:
        return max(0, int(str(values[0]).strip()))
    except (TypeError, ValueError):
        return DEFAULT_VISA_SPONSOR_MIN_SALARY


def _parse_allow_overqualified(values: list[str]) -> bool:
    """Whether the candidate is open to roles pitched below their own seniority.

    Same shape and same reasoning as _parse_visa_sponsor_only above: no row and a
    falsey row both mean off, and off is the default."""
    return bool(values) and str(values[0]).strip().lower() in ("true", "1", "yes")


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
    """Values annotated with their stated proficiency and, for skills, evidence
    origin, e.g. 'Python (Expert)' or 'SQL (Proficient, AI-assisted)'."""
    out = []
    for a in group:
        parts = [p for p in (a.proficiency, a.evidence_origin) if p]
        out.append(f"{a.value} ({', '.join(parts)})" if parts else a.value)
    return out


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


# evidence_origin values that mean the skill was NOT exercised in paid/commercial
# work -- see config.EVIDENCE_ORIGIN_CHOICES. "Commercial" and unset are left
# unannotated (strong/neutral evidence).
_WEAK_EVIDENCE_ORIGINS = {"self-directed", "academic", "ai-assisted"}


def _evidence_tier(proficiency: str | None, evidence_origin: str | None) -> str | None:
    """Coarse evidence-strength label combining depth (proficiency) and origin
    (evidence_origin), for the cheap gate/rank prompts -- these never see either
    field otherwise (only the expensive final judge does, via _labeled). Only
    annotates the weak cases (shallow depth or non-commercial origin); strong or
    unset evidence is left unannotated, same convention as _weight_tier."""
    tags = []
    if proficiency and proficiency.lower() in ("familiar", "one-time"):
        tags.append(proficiency.lower())
    if evidence_origin and evidence_origin.lower() in _WEAK_EVIDENCE_ORIGINS:
        tags.append(evidence_origin.lower())
    if not tags:
        return None
    return f"{'/'.join(tags)} evidence only"


def _evidence_tiers(group: list[ProfileAttribute]) -> dict[str, str]:
    """value -> weak-evidence tier, omitting strong/unset evidence entirely."""
    out: dict[str, str] = {}
    for a in group:
        tier = _evidence_tier(a.proficiency, a.evidence_origin)
        if tier:
            out[a.value] = tier
    return out


def _infer_region(skills: list[str], roles: list[str], location: str) -> dict:
    """One cheap call to infer sectors + Adzuna country code the engine needs but
    our schema doesn't store directly. Falls back to safe defaults."""
    data = llm_json(
        f"""Given this candidate, return JSON:
{{"sectors": ["2-4 industry domains e.g. software, fintech, marketing"],
  "adzuna_country_code": "the ISO 3166-1 alpha-2 country code (lowercase) for the candidate's country -- ANY country worldwide, e.g. gb, us, ca, au, de, fr, ae, sa, qa, ng, ke, ie, es, jp, br, in; pick the closest match to the Location field, default gb only if truly unknown"}}
Skills: {', '.join(skills) or 'n/a'}
Roles: {', '.join(roles) or 'n/a'}
Location: {location or 'United Kingdom'}"""
    )
    sectors = data.get("sectors") if isinstance(data.get("sectors"), list) else []
    cc = data.get("adzuna_country_code") or "gb"
    return {"sectors": sectors or ["general"], "adzuna_country_code": str(cc).lower()[:2]}


# Embedding pre-filter is cosine similarity, which rewards a tight, topical
# query -- so it's deliberately narrower than the full profile: target_role
# ONLY, the direct signal for "what job". skill/past_role/qualification/
# experience used to be blended in too, but a generic skill list matches
# broadly across unrelated postings, and free-text experience bullets
# (unbounded in count, sometimes full sentences) diluted the query further
# the richer a candidate's history was -- exactly backwards, since a
# well-documented candidate should score BETTER, not worse. sector_target
# (repeated) plus the LLM-inferred sector guess and seniority (appended once)
# used to be blended in here too, but neither is scoped by role_filter, so
# every cluster's text carried the exact identical sector/seniority tokens
# regardless of that cluster's own roles -- diluting each cluster's centroid
# with generic words instead of sharpening it, and actively working against
# the reason per-cluster scoping exists at all. A live diagnostic re-score
# (analyze_embedding_gate.py) confirmed two unrelated clusters sharing one
# identical sector/seniority tail and prompted dropping it. All of that
# detail still reaches the final AI judge in full (cv_text_base, which still
# gets "Sector interests: ..." and seniority) and sector_target still shapes
# the cheap gate/rank prompts -- only the cosine pre-filter stops using it.
_BASE_EMPHASIS = 3  # times a target_role repeats per unit of weight


def _declared_priority_multiplier(attr: ProfileAttribute) -> float:
    """The candidate's DECLARED priority for a target role: a bonus if they
    pinned it within its family. (A family's active/inactive tier used to feed
    a second multiplier here too -- dropped because inactive families are now
    excluded from clustering entirely, see _role_groups, so by the time a
    target role reaches this function its family is active by definition.)

    Deliberately a separate multiplier from `weight` (which is what tick/cross
    feedback LEARNED) rather than something that writes into it: the two answer
    different questions and multiply together, so pinning a role weights it up
    without erasing the fact that it's been crossed, and crossing a pinned role
    still suppresses it."""
    return PINNED_ROLE_MULT if attr.pinned else 1.0


def _weighted_text(
    g: dict[str, list[ProfileAttribute]], search_terms: list[str],
    role_filter: set[str] | None = None,
) -> str:
    """Weighted emphasis text driving the embedding pre-filter: repeat each
    target_role value roughly in proportion to its learned weight so feedback
    actually shifts results both up (sustained ticks) and down (sustained
    crosses). Previously this used `max(1, round(base * max(1, round(a.weight)) * mult))`
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
    embedding text instead of one blend of every target role the candidate has.

    target_role ONLY -- see the comment above _BASE_EMPHASIS for why
    sector_target/sectors/seniority were dropped from here."""
    emphasis: list[str] = []
    for a in g.get("target_role", []):
        if role_filter is not None and a.value.strip() not in role_filter:
            continue
        mult = _declared_priority_multiplier(a)
        count = max(0, round(_BASE_EMPHASIS * a.weight * mult))
        emphasis.extend([a.value] * count)
    return " ".join(emphasis) or " ".join(search_terms)


def cv_text_for_cluster(cv_text_base: str, cluster_roles: list[str]) -> str:
    """Scope the synthetic CV's target-roles line to one role cluster, so a
    per-cluster final-evaluation call judges fit against ONE coherent role
    identity instead of every field the candidate has ever listed."""
    if not cluster_roles:
        return cv_text_base
    return f"{cv_text_base}\nTarget roles: {', '.join(cluster_roles)}"


def _role_groups(
    db: Session, profile_id: int, g: dict[str, list[ProfileAttribute]],
) -> list[tuple[str, list[str]]]:
    """The profile's target roles as (label, roles) clusters -- one per ACTIVE
    family. An inactive family's target roles are dropped here, not just
    de-weighted: they never become a cluster, so the engine runs no discovery/
    gate/rank/judge for them at all (see families.ordered_for_engine and
    config.py's role-families section).

    Families ARE the clusters (see services/families.py). Two fallbacks keep a
    run from ever failing for want of family rows: a target_role with no family
    (nothing has called ensure_families for this profile yet) is grouped by the
    seeding clusterer in memory, without persisting; a profile with no target
    roles at all yields no clusters, exactly as before."""
    families = list_families(db, profile_id)
    family_ids = {f.id for f in families}
    by_family: dict[int, list[str]] = defaultdict(list)
    ungrouped: list[str] = []
    for a in g.get("target_role", []):
        if a.family_id in family_ids:
            by_family[a.family_id].append(a.value)
        else:
            ungrouped.append(a.value)

    # ordered_for_engine already filters out inactive families.
    groups = [(f.name, by_family[f.id]) for f in ordered_for_engine(families) if by_family[f.id]]
    groups.extend(cluster_target_roles(ungrouped))

    # Same cap the LLM path has always enforced -- discovery volume is fixed per
    # run, so more active clusters only thins each one's pool. ordered_for_engine
    # returns active families in display order, so whichever active family is
    # least-preferred gets merged away.
    if len(groups) > MAX_ROLE_CLUSTERS:
        overflow = [r for _, roles in groups[MAX_ROLE_CLUSTERS - 1:] for r in roles]
        groups = groups[:MAX_ROLE_CLUSTERS - 1] + [(groups[MAX_ROLE_CLUSTERS - 1][0], overflow)]
    return groups


def _resolve_enforcement(attr: ProfileAttribute) -> str:
    """This row's hard/soft, falling back to its type's pre-enforcement default.
    A null column is meaningful (never set), so this is the only correct way to
    read enforcement -- see config.enforcement_for."""
    return attr.enforcement or enforcement_for(attr.type, attr.value)


def _enforced(group: list[ProfileAttribute], level: str) -> list[str]:
    """Values in `group` whose enforcement resolves to `level`."""
    return [a.value for a in group if _resolve_enforcement(a) == level]


def _hard_axes(g: dict[str, list[ProfileAttribute]], work_types_hard: bool) -> list[str]:
    """screen_gate axis names the candidate promoted from soft to an
    unconditional drop, as `_`-prefixed attribute names matching
    full_auto.SOFT_GATE_AXES so engine.py can subtract them directly.

    An axis counts as hard only if the candidate actually stated the
    constraint -- a Hard toggle on a Seniority row they never picked, or a
    salary floor of 0, would otherwise hard-drop on an axis with nothing to
    judge against."""
    axes = []
    if g.get("seniority") and any(_resolve_enforcement(a) == "hard" for a in g["seniority"]):
        axes.append("_seniority_ok")
    if _parse_salary_floor(_values(g.get("salary", []))) and any(
        _resolve_enforcement(a) == "hard" for a in g.get("salary", [])
    ):
        axes.append("_salary_ok")
    if work_types_hard:
        axes.append("_work_arrangement_ok")
    return axes


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
    # Candidate's own requirement chips, split by the enforcement the candidate
    # chose per chip. HARD ones are unconditional drops at the cheap gate
    # (screen_gate's hard_gate_ok axis) and the final judge (a DISQUALIFIER
    # rule), LLM-adjudicated on a CLEAR violation only -- this is what every
    # avoid/must_have row did before enforcement existed, and still the default.
    # SOFT ones are stated preferences: they reach the same two places but only
    # as one ordinary soft-axis signal and a judge hint, never removing a listing
    # on their own.
    avoids = _enforced(g.get("avoid", []), "hard")
    must_haves = _enforced(g.get("must_have", []), "hard")
    soft_avoids = _enforced(g.get("avoid", []), "soft")
    soft_must_haves = _enforced(g.get("must_have", []), "soft")

    # Location: separate the place from the work-type tokens. They share the
    # `location` type but are different constraints with their own enforcement --
    # the place drives the fail-closed country/city prefilter, the work types
    # drive screen_gate's work_arrangement axis.
    loc_place, work_types = "", []
    location_hard, work_types_hard = True, False
    for a in g.get("location", []):
        if a.value.lower().strip() in _WORK_TYPES:
            work_types.append(a.value)
            work_types_hard = work_types_hard or _resolve_enforcement(a) == "hard"
        elif not loc_place:
            loc_place = a.value
            location_hard = _resolve_enforcement(a) == "hard"
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

    if scope == "international" or (scope == "national" and explicit_global) or not location_hard:
        # `not location_hard` is the third way to clear the filter, alongside
        # "International" scope and the Global chip: the candidate set the
        # Location row itself to Soft, i.e. "prefer here, don't reject on it".
        # This does NOT weaken the fail-closed default -- an untouched location
        # row still resolves to hard (config.ENFORCEMENT_DEFAULT) and still
        # filters to the inferred country; only an explicit choice waives it.
        #
        # explicit_global stands on its own here (not just as the fallback-scope
        # signal above) -- picking the "Global (no filter)" country chip while
        # scope="national" must clear the hard filter. This used to only be
        # consulted when scope was blank (line above), so choosing Global while
        # scope="national" was silently discarded and the fail-closed
        # inferred-country filter applied anyway (bug: log showed a `gb` filter
        # even with Global selected). Gated to scope=="national" specifically --
        # a stale "global" country row left over from a prior national
        # selection shouldn't also waive the country filter for "local" scope,
        # which narrows by country+city regardless of any country chip (chips
        # aren't even shown in the UI once scope leaves "national").
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
    # Read once here because it is needed twice below: in engine_profile (for the
    # free title prescreen and the cheap gate/rank prompts) and as a cv_text line
    # (the only route into the final judge's constant system prompt).
    allow_overqualified = _parse_allow_overqualified(_values(g.get("allow_overqualified", [])))

    # Salary floor for the hard prefilter: the lower bound of the stated range.
    # 0 (the slider's default min) means "no floor" -> the filter is a no-op.
    salary_floor = _parse_salary_floor(_values(g.get("salary", [])))

    # Maximum listing age (Dashboard "Maximum listing age" preference), single-
    # value like location_scope/seniority. 0 = "No limit", the candidate's own
    # opt-out -- the check is skipped entirely, same as an unset salary floor.
    # Enforcement defaults to Hard (config.ENFORCEMENT_DEFAULT) when no row
    # exists at all, matching "30 days, hard, by default".
    max_age_group = g.get("max_listing_age", [])
    max_listing_age_days = _parse_max_listing_age_days(_values(max_age_group))
    max_listing_age_hard = (
        _resolve_enforcement(max_age_group[0]) == "hard" if max_age_group else True
    )

    # Role clusters: one per ACTIVE role family the candidate defined (usually
    # one, but a candidate targeting genuinely different fields gets several --
    # see services/families.py); inactive families contribute no cluster at all
    # (see _role_groups). The engine scores, gates, and evaluates each cluster
    # independently. Each carries its own weighted_text (scoped to just that
    # cluster's roles) so its embedding isn't diluted by the candidate's other,
    # unrelated target roles, and `label` so logs and the "Matched via" note
    # name the family the candidate named rather than a role inside it.
    role_groups = _role_groups(db, profile_id, g)
    role_clusters = [
        {"label": label, "roles": grp,
         "weighted_text": _weighted_text(g, search_terms, role_filter=set(grp))}
        for label, grp in role_groups
    ]

    # Cached profile-intel header (see profile_intel.py) for the final judge's CV
    # text. Pure read, no LLM call -- {} until profile_intel has run at least once
    # for this profile (e.g. before the first search).
    intel = read_cached_intel(db, profile_id)
    header = intel.get("header") or ""
    # The candidate's own must_have/avoid chips, verbatim -- feeds screen_gate's
    # soft requirements_ok axis. No LLM re-derivation (see profile_intel.py's
    # module docstring for why the old TASK 3 was removed).
    requirements = candidate_requirements_display(db, profile_id)
    profile = db.get(Profile, profile_id)
    # Evidence-focused narrative brief -- profile.cv_summary, written by the
    # formation "understand" call (profile_intel.generate_understanding) in the
    # same pass as the header. Named projects/tools/outcomes the tier labels
    # elsewhere in engine_profile can't carry -- and, since skills/qualifications
    # are no longer extracted as their own chips, this is now the ONLY place that
    # concrete skill/qualification detail reaches the gates and the final judge.
    candidate_brief = (profile.cv_summary or "").strip() if profile else ""

    engine_profile = {
        # Scopes full_auto's shared rotation cursor (boards_cache.db is one file
        # for every profile) so concurrent profiles don't share one rotation position.
        "profile_id": profile_id,
        "sectors": region["sectors"],
        "seniority": seniority,
        "key_skills": skills[:10],
        "location": location,
        # Candidate-stated Remote/Hybrid/On-site preference(s), if any (see the
        # location-attribute split above). [] means the candidate hasn't stated
        # one -- _screen_prompt's WORK ARRANGEMENT axis treats that as "no
        # preference to conflict with", not as "candidate wants remote".
        "work_types": work_types,
        "adzuna_country_code": region["adzuna_country_code"],
        "country_codes": country_codes,  # [] means no hard filter (Global)
        "location_scope": scope,          # local | national | international
        # City narrowing rides on the same Soft opt-out as the country filter
        # above: a soft Location row means "prefer here", which can't also mean
        # "and reject anything outside this one city".
        "local_place": loc_place if (scope == "local" and location_hard) else "",
        # The candidate's stated place, ALWAYS -- unlike local_place, which is
        # deliberately blanked unless it's acting as a hard filter. This one is
        # the origin distances are measured FROM (services/geo.py), and a
        # distance is worth knowing at every scope: at National it's pure
        # information on the card, and at International it still tells a
        # candidate whether a role is down the road or across the country.
        # Blank when the candidate has typed no place at all -- note `location`
        # above falls back to "United Kingdom", which is country-level and
        # deliberately resolves to no coordinates.
        "origin_place": loc_place,
        # Commute radius in miles. Only ever ENFORCED at scope="local" (see
        # engine._filter_by_distance); 0 means the candidate turned the limit
        # off. Rides location_hard for the same reason local_place does -- a
        # Soft Location row means "prefer here", which cannot also mean "reject
        # anything past 30 miles".
        "commute_miles": _parse_commute_miles(_values(g.get("commute_miles", []))),
        "commute_hard": location_hard,
        "salary_floor": salary_floor,     # 0 means no salary floor
        # Candidate's own tolerance for an old listing (Dashboard "Maximum
        # listing age" preference). 0 means "No limit" -- the check is skipped
        # entirely, never a 0-day mismatch. When hard (the default), a listing
        # DEFINITELY known to be older is dropped outright at the gate, before
        # any LLM call; when soft it's only ever downgraded, at rank_gate/the
        # judge. See full_auto.py's listing_over_max_age/_listing_age_tag.
        "max_listing_age_days": max_listing_age_days,
        "max_listing_age_hard": max_listing_age_hard,
        # Restrict to employers on the Home Office licensed-sponsor register.
        # Always hard when on (there is no useful soft reading of "I need a
        # visa"), so unlike the constraints above it carries no _hard twin.
        # Read by engine._filter_by_sponsor and by full_auto's ATS batch
        # tiering / page depth / sponsor-scoped search terms.
        "visa_sponsor_only": _parse_visa_sponsor_only(
            _values(g.get("visa_sponsor_only", []))),
        # Minimum annual salary a role must clear to count as sponsorable. Only
        # ever enforced when visa_sponsor_only is on (engine._filter_by_sponsor);
        # 0 means the candidate turned the floor off. See config.ATTRIBUTE_TYPES'
        # visa_sponsor_min_salary entry for the reduced-rate categories this
        # deliberately does not try to detect on its own.
        "visa_sponsor_min_salary": _parse_visa_sponsor_min_salary(
            _values(g.get("visa_sponsor_min_salary", []))),
        # Open to roles pitched BELOW the candidate's own stated seniority.
        # Off by default. Read by engine._heuristic_prescreen (which stops
        # hard-dropping junior-marked titles for a senior profile) and by
        # full_auto's _screen_prompt/_rank_prompt/the judge, which stop treating
        # a lower-pitched role as a seniority mismatch. Never re-admits
        # apprenticeships or placement years -- those are excluded on eligibility,
        # not on level. Like visa_sponsor_only it is inherently one-directional
        # and carries no _hard twin: turning it on IS the softening.
        "allow_overqualified": allow_overqualified,
        "search_terms": search_terms,
        "role_clusters": role_clusters,   # list[{"roles": [...], "weighted_text": "..."}]
        # value -> priority label, used by the cheap gate/rank prompts (full_auto.py's
        # _screen_prompt/_rank_prompt) to annotate target roles/skills the candidate
        # has ticked or crossed on past results -- so that feedback also has some
        # influence on which roles reach the expensive judge, not just the embedding
        # pre-filter. Omits neutral-weight values entirely (see _weight_tier).
        "target_role_weight_tiers": _weight_tiers(g.get("target_role", [])),
        "skill_weight_tiers": _weight_tiers(g.get("skill", [])),
        # value -> weak-evidence label (shallow proficiency and/or non-commercial
        # evidence_origin), used by _screen_prompt/_rank_prompt alongside the
        # weight tiers above so weak/self-directed/AI-assisted skill evidence has
        # some influence at the cheap gate/rank stage, not only at the final judge.
        "skill_evidence_tiers": _evidence_tiers(g.get("skill", [])),
        # "" when the CV gave nothing concrete beyond the typed attribute rows,
        # or before any CV/text has ever been parsed.
        "candidate_brief": candidate_brief,
        # The candidate's OWN words about what they're looking for (profile.
        # intent_text -- the optional free-text box on /onboarding and
        # /dashboard). Read by full_auto._rank_prompt only. It reached the final
        # judge from the very start (via cv_text below, where it outranks every
        # other want-signal) but was invisible to the mid tier, so rank_gate
        # scored function fit against a bare list of target-role TITLES with no
        # access to the one place the candidate says what they actually mean by
        # them -- which is precisely the signal needed to tell an ambiguous
        # title's two professions apart. Deliberately NOT given to screen_gate:
        # its sector axis is scoped to job FUNCTION on purpose (see
        # _screen_prompt's ROLE FUNCTION FIT and CLAUDE.md's note on the
        # `sectors` guess that was removed from it), and feeding free-text
        # aspiration into that axis is the same drift that change fixed.
        # "" when the candidate hasn't written one -- which is now the default
        # after a short CV, since nothing is auto-drafted (see
        # profile_intel.generate_families).
        "intent_text": ((profile.intent_text or "").strip() if profile else ""),
        # Candidate-specific must-have/must-not-have bullets for screen_gate's
        # requirements_ok axis (see full_auto.py::_screen_prompt). [] means none
        # generated yet or none stated.
        "requirements": requirements,
        # Candidate's OWN explicit hard filters (chips they marked Hard). Unlike
        # the soft LLM-derived "requirements" above, these drive an unconditional
        # hard drop at screen_gate (hard_gate_ok) + the final judge, on a CLEAR
        # violation only. [] means the candidate stated none.
        "avoid": avoids,
        "must_have": must_haves,
        # The same chips marked Soft: real preferences, but ones the candidate
        # said not to reject on. They reach screen_gate's requirements_ok axis
        # (one ordinary soft failure, never a drop) and the final judge as
        # preferences rather than DISQUALIFIERS.
        "soft_avoid": soft_avoids,
        "soft_must_have": soft_must_haves,
        # Which of screen_gate's normally-soft axes the candidate promoted to an
        # unconditional drop. engine.py reads these to move an axis out of
        # SOFT_GATE_AXES for the run (see _hard_enforced_axes); full_auto's
        # _screen_prompt reads them only to tell the model the bar is strict.
        # Defaults preserve each axis's pre-enforcement behaviour -- see
        # config.ENFORCEMENT_DEFAULT.
        "hard_axes": _hard_axes(g, work_types_hard),
    }

    weighted_text = _weighted_text(g, search_terms)

    # Synthetic CV text for the expensive-AI final evaluation (engine reads a file).
    # cv_text_base omits the "Target roles" line -- see cv_text_for_cluster,
    # which appends it scoped to one role cluster at a time, instead of always
    # listing every target role the candidate has (which invites the judge to
    # weigh fit against all of them at once).
    #
    # Deliberately de-duplicated rather than concatenating every available
    # synthesis: the profile_intel `header` ("Looking for...") is an AI
    # paraphrase of the same background this whole function reads, and
    # profile.intent_text -- the candidate's OWN words, when they've given any --
    # is both more authoritative and richer, so header is only a fallback for
    # profiles with no intent_text yet. candidate_brief (profile.cv_summary,
    # placed right after skills where it's most useful and safest from
    # truncation, not at the end) is the one evidence-focused compression of the
    # raw CV text now -- it used to be duplicated with a second, separately-
    # generated compression here, which risked diluting the genuinely
    # load-bearing detail (concrete evidence origin -- see EVIDENCE STRENGTH in
    # full_auto.py) or losing it to _run_final_eval's char budget.
    if profile and profile.intent_text:
        cv_lines = [
            "What the candidate is looking for (in their own words): " + profile.intent_text.strip()
        ]
    elif header:
        cv_lines = [header]
    else:
        cv_lines = []
    if past_roles:
        cv_lines.append("Past roles: " + ", ".join(_labeled(g.get("past_role", []))))
    if qualifications:
        cv_lines.append("Qualifications: " + "; ".join(qualifications))
    if seniorities:
        cv_lines.append("Seniority: " + ", ".join(seniorities))
    # The judge's system prompt is a byte-identical constant across every profile
    # and run (that is what makes its 24h prompt-cache retention pay off), so a
    # per-profile preference has to ride here in the CV text instead -- same
    # mechanism as the "Maximum listing age"/"Location search scope" lines below.
    # DISQUALIFIER 1 keys off this exact wording. Only emitted when the preference
    # is on, so an unchanged profile's payload is unaffected.
    if allow_overqualified:
        cv_lines.append(
            "Open to more junior roles: yes -- the candidate will consider roles pitched below "
            "their stated seniority, so a more junior role is not a mismatch for them. This does "
            "NOT extend to apprenticeships or student placements, which they are ineligible for.")
    if skills:
        cv_lines.append("Skills: " + ", ".join(_labeled(g.get("skill", []))))
    if candidate_brief:
        cv_lines.append("Skill evidence detail: " + candidate_brief)
    # No "Sector interests: ..." line. sector_target used to reach the final judge
    # here and drove its (now removed) "sector_match" signal, whose only
    # user-visible effect was a "this is not within your stated clean-energy,
    # science, climate or nonprofit sector interests" item in `concerns` -- noise
    # on a card about whether the candidate can do the job, and one that also
    # silently blocked a "very_strong" grade. The candidate's own words
    # (intent_text, at the top of this block) already carry any genuine mission
    # preference, in a form they wrote and can edit.
    #
    # sector_target rows are NOT deleted and still auto-fill from the CV: they
    # remain a DISCOVERY-side signal only (services/harvest.py uses them to pick
    # ATS-harvest keywords, which is how a charity-sector candidate reaches
    # charity employers at all). Nothing the candidate reads is derived from them.
    if location or work_types:
        cv_lines.append(f"Location: {location} ({', '.join(work_types) or 'any'})")
    # GEOGRAPHY (the judge's LOCATION/VISA/RELOCATION disqualifier, part (a)) judges
    # physical feasibility purely from the place name above unless told otherwise --
    # which contradicts location_scope's own definition the moment scope isn't
    # "local": "national" means the candidate is deliberately searching country-wide,
    # not narrowed to their city (see snapshot.build_snapshot's scope handling), yet
    # without this line the judge had no way to know that and would flag an on-site/
    # hybrid role in a different city of the SAME country as "geographically
    # impractical" -- exactly the scope the candidate opted into. Kept as its own
    # line (not folded into "Location" above) so it reads as an instruction about how
    # to apply GEOGRAPHY, not as another fact about the candidate.
    _scope_note = {
        "local": f"the candidate is searching only near {location or 'their stated location'} "
                 "-- an on-site/hybrid role elsewhere is genuinely impractical for them.",
        "national": "the candidate has deliberately opted into a country-wide search, not "
                     f"narrowed to {location or 'their stated location'} -- an on-site/hybrid role "
                     "elsewhere in their OWN country must NOT be treated as geographically "
                     "impractical merely for being a different city or far away; only a role in a "
                     "different country, or one requiring an already-held visa/right-to-work the "
                     "candidate clearly lacks, fails GEOGRAPHY.",
        "international": "the candidate has deliberately opted into a worldwide search -- distance "
                          "or country alone is never grounds to fail GEOGRAPHY for them; only an "
                          "explicit visa/right-to-work requirement they clearly can't meet does.",
    }.get(scope)
    if _scope_note:
        cv_lines.append("Location search scope: " + _scope_note)
    # Its own line, separate from the place above, because the judge's
    # LOCATION/VISA/RELOCATION disqualifier runs two independent checks and was
    # conflating them: geography (can they physically take it) and stated
    # arrangement (is it the pattern they asked for). Folded into the Location
    # parenthetical, the work types read as a footnote to the place, and a
    # fully-remote listing -- geographically fine for everyone -- passed the
    # whole rule for a candidate who had ticked only On-site/Hybrid. The
    # Hard/Soft is stated here rather than left implicit so the judge knows
    # whether a mismatch excludes the role or only deprioritises it, matching
    # the HARD REQUIREMENTS / PREFERENCES split used for avoid/must_have below.
    if work_types:
        binding = (
            "binding -- a listing matching none of these is disqualified"
            if work_types_hard else
            "a preference -- weigh it, but it is not grounds to reject a role"
        )
        cv_lines.append(
            f"Work arrangement wanted ({binding}): {', '.join(work_types)}"
        )
    # Threading the actual day count + Hard/Soft through the CV text, rather
    # than the (cached, profile-independent) judge system prompt, is what lets
    # this preference reach the judge at all -- see full_auto.py's
    # _FINAL_EVAL_SYSTEM docstring on why it must stay identical across every
    # profile/run to keep OpenAI's 24h prompt-cache retention worth paying for.
    if max_listing_age_days:
        binding = (
            "a hard limit -- exclude a role CLEARLY older than this"
            if max_listing_age_hard else
            "a preference -- downgrade a role clearly older than this, never exclude it"
        )
        cv_lines.append(
            f"Maximum listing age ({binding}): {max_listing_age_days} days. Only apply this to a "
            "listing whose posting date is CLEARLY known -- an unknown or merely-approximate date "
            "is never penalised on this basis."
        )
    if customs:
        cv_lines.append("Constraints: " + "; ".join(customs))
    # Candidate's own hard filters, surfaced prominently so the judge's HARD
    # EXCLUSIONS/REQUIREMENTS disqualifier can enforce them. Kept out of
    # _weighted_text (embedding) on purpose -- see the intent_text note above.
    if must_haves:
        cv_lines.append("HARD REQUIREMENTS (a role must satisfy all of these): " + "; ".join(must_haves))
    if avoids:
        cv_lines.append("HARD EXCLUSIONS (reject a role that clearly involves any of these): " + "; ".join(avoids))
    # The same chips marked Soft. Worded so the judge weighs them without
    # treating them as DISQUALIFIERS -- the whole point of the Soft toggle is
    # that the candidate wants these considered, not enforced.
    if soft_must_haves:
        cv_lines.append("PREFERENCES (wanted, but NOT grounds to reject a role): " + "; ".join(soft_must_haves))
    if soft_avoids:
        cv_lines.append("DISLIKES (count against a role, but NOT grounds to reject it): " + "; ".join(soft_avoids))
    # Feedback the candidate typed on the /search page about recent RESULTS (not
    # what they want, which is intent_text/header above) -- e.g. "these are too
    # senior" or "stop showing sales roles". Surfaced to the judge as a steer
    # for this run, not enforced like a hard filter. Appended last (before
    # cv_text_for_cluster's per-cluster "Target roles" suffix) since it's the
    # most disposable line if _run_final_eval's char budget ever does truncate.
    if profile and profile.search_feedback:
        cv_lines.append(
            "Candidate's feedback on recent search results (take this into account this run): "
            + profile.search_feedback.strip()
        )
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
