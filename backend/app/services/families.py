"""Role families: the user-editable streams that ARE the engine's clusters.

The pipeline scores, gates, and judges each cluster independently, so a
candidate targeting genuinely different fields is judged fairly on each instead
of against a blend of both. Those clusters used to be re-derived on every run by
an LLM call over the flat target_role list. They are now rows the candidate owns
and edits (models.RoleFamily), and the multi-family LLM clustering below (see
_seed_new_families) survives only as the SEEDING step: it runs when a profile
has NO families at all yet -- a fresh CV parse, or a profile that predates this
table. Once a profile has any family, a later-ungrouped role (e.g. from a
profile-wide "regenerate target roles", which doesn't know about family
boundaries) is instead slotted into an EXISTING family via
_assign_into_existing_families -- family count/identity only ever changes
through an explicit user action (Add family / delete a family), never as a side
effect of tidying up ungrouped roles. See ensure_families for the branch point.

That swap is what makes the grouping stable and inspectable: previously two runs
could silently cluster the same roles differently (the cache is per-process), and
the candidate had no way to say "these two are one search, that one is separate."
"""
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import (
    FAMILY_REGEN_TARGET_COUNT,
    FAMILY_TIER_DEFAULT,
    FAMILY_TITLE_RESERVE_TARGET,
    MAX_ROLE_CLUSTERS,
)
from ..models import Profile, ProfileAttribute, RoleFamily
from .llm import CHEAP_MODEL, MID_MODEL, llm_json
from .profile_intel import _BACKGROUND_TYPES, _TARGET_ROLE_GUIDANCE, _background_text, _grouped_values


def _cluster_prompt(roles: list[str]) -> str:
    """Builds the clustering prompt as its own function (rather than inlined in
    the cached call below) so a diagnostic harness can print the exact text
    sent to the model without duplicating it -- see tests/profile_formation_harness.py."""
    return f"""Group these job title strings into clusters by underlying job function /
career path, AT MOST 3 clusters total. Split into separate clusters whenever two
titles represent unrelated roles. For example, "Communications Officer" and "Editorial Assistant" are
different and belong in
SEPARATE clusters. Only
merge titles into the same cluster when they are genuinely the same underlying
job function -- different seniority phrasings, entry-level modifiers (Intern,
Trainee, Graduate, Technician, Assistant, Associate, Junior, Executive,
Representative, Coordinator, Officer, Development), or near-duplicate wording of
the same role. An entry-level modifier is NOT a different function on its own --
judge by what the title is actually doing, not the seniority word attached to it.
For example, "CAD Engineering Intern", "Electrical Engineering Intern", and
"Manufacturing Engineering Intern" are all the same underlying engineering-intern
function and belong in ONE cluster, not three; likewise "Sales Development
Representative" and "Outbound Sales Executive" are both the same underlying
sales function (selling to new prospects) and belong in ONE cluster labeled
"Sales", not split into "Sales Development" and "Outbound Sales" just because one
title says "Development" and the other "Outbound".
If the titles genuinely span more than 3 distinct job functions, keep the 3
most substantively supported (most titles, or most central to the set) and ignore the rest.
Also give each cluster a short human label (2-3 words) naming the job function it
represents. Prefer the field to the level -- e.g. label a cluster
of "Electrical Engineering Intern" / "Engineering Technician" titles "Electrical
Engineering", not "Engineering Intern". Use a single clean name; use an umbrella term 
unless the roles are very niche e.g. "Data
Analytics" over "Data & BI Analytics".
Output ONLY JSON: {{"clusters": [{{"label": "...", "titles": ["title a", "title b"]}}]}}
Titles: {', '.join(roles)}"""


@lru_cache(maxsize=256)
def _cluster_target_roles_cached(roles_key: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """LLM grouping of a role set into clusters, cached for this process's
    lifetime per unique (sorted, deduped) role set. Falls open to a single
    cluster on anything malformed.

    This is now only the SEARCH-TIME FALLBACK for a target_role that somehow
    reaches the engine still ungrouped (snapshot._role_groups) -- the primary CV
    seed path no longer clusters at all: formation's understand call emits the
    families (label + titles) directly, decided with full context in one pass
    (see services/formation.py + profile_intel.generate_understanding), which is
    what fixed the over-split/over-merge the separate re-cluster step caused.
    Runs on MID_MODEL: a bounded, rarely-hit fallback doesn't warrant the strong
    tier now that it isn't the seed path."""
    roles = list(roles_key)
    data = llm_json(_cluster_prompt(roles), model=MID_MODEL)
    clusters = data.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        return ((_default_label(roles), *roles),)
    seen: set[str] = set()
    out: list[tuple[str, ...]] = []
    for c in clusters:
        if not isinstance(c, dict):
            continue
        titles = c.get("titles")
        if not isinstance(titles, list):
            continue
        members = tuple(r for r in titles if isinstance(r, str) and r in roles and r not in seen)
        seen.update(members)
        if members:
            label = c.get("label")
            label = label.strip() if isinstance(label, str) and label.strip() else _default_label(list(members))
            out.append((label, *members))
    missing = [r for r in roles if r not in seen]
    if missing:
        out.append((_default_label(missing), *missing))
    if not out:
        return ((_default_label(roles), *roles),)
    # The prompt asks for 1-3 clusters, but nothing enforces that on the model's
    # output -- reassign any excess by genuine job-function fit rather than
    # dumping it into whichever cluster happened to sort last (see
    # _consolidate_overflow), which used to be able to land e.g. a "Publishing
    # Assistant" title from an over-split science-communication cluster inside
    # an unrelated survivor like "Data Analytics" purely by position, both
    # polluting that family with an unrelated role and inflating its count well
    # past the intended 4-8.
    if len(out) > MAX_ROLE_CLUSTERS:
        out = _consolidate_overflow(out)
    return tuple(out)


def _assign_prompt(titles: list[str], targets: list[tuple[str, ...]], context_line: str) -> str:
    """Builds the assign-by-fit prompt as its own function (same reason as
    _cluster_prompt above) so a diagnostic harness can print the exact text
    sent to the model -- see tests/profile_formation_harness.py. Shared by
    _consolidate_overflow (assigning excess cluster titles to a surviving
    cluster) and ensure_families (assigning newly-ungrouped roles to an
    already-existing family) -- only the framing sentence differs between the
    two callers, the target-listing/output-format instructions are identical."""
    target_block = "\n".join(f'- "{label}": {", ".join(members)}' for label, *members in targets)
    labels_desc = "exact label" if len(targets) == 1 else f"of these {len(targets)} exact labels"
    return f"""{context_line}
{', '.join(titles)}

{target_block}

For EACH title above, assign it to whichever {labels_desc} it is the closest genuine
job-function match to -- every title must be assigned to exactly one of those labels,
even if the fit is imperfect (pick the closest one, never invent a new label or leave a
title unassigned).
Output ONLY JSON: {{"assignments": {{"title": "label", ...}}}}"""


def _assign_by_fit(titles: list[str], targets: list[tuple[str, ...]], context_line: str) -> dict[str, list[str]]:
    """Assigns each of `titles` to whichever of `targets` ((label, *members)
    tuples) it's the closest genuine job-function fit for, via one bounded
    cheap-model call. Falls back to the first (most-supported/primary) target
    on any call failure or an unmatched/malformed response -- every title is
    always placed somewhere, never dropped, and this never raises."""
    labels = [t[0] for t in targets]
    label_lookup = {label.strip().lower(): label for label in labels}
    fallback_label = labels[0]

    buckets: dict[str, list[str]] = {label: [] for label in labels}
    if len(targets) == 1:
        # Nothing to decide -- the only target is the answer for every title,
        # so skip the call entirely rather than ask the model a trivial question.
        buckets[fallback_label] = list(titles)
        return buckets

    data = llm_json(_assign_prompt(titles, targets, context_line), model=CHEAP_MODEL)
    assignments = data.get("assignments") if isinstance(data.get("assignments"), dict) else {}

    for title in titles:
        chosen = assignments.get(title)
        chosen_key = chosen.strip().lower() if isinstance(chosen, str) else None
        buckets[label_lookup.get(chosen_key, fallback_label)].append(title)
    return buckets


def _consolidate_overflow(out: list[tuple[str, ...]]) -> list[tuple[str, ...]]:
    """More than MAX_ROLE_CLUSTERS clusters came back despite the prompt asking
    for at most 3 -- keep the MAX_ROLE_CLUSTERS most-supported clusters (by
    member count, ties keeping the model's original order, matching the
    prompt's own "most substantively supported" instruction) and reassign the
    rest by genuine job-function fit via _assign_by_fit, rather than always
    folding overflow into whichever cluster happened to sort last."""
    ranked = sorted(out, key=lambda c: len(c) - 1, reverse=True)
    survivors = ranked[:MAX_ROLE_CLUSTERS]
    overflow = [r for cluster in ranked[MAX_ROLE_CLUSTERS:] for r in cluster[1:]]
    if not overflow:
        return survivors

    buckets = _assign_by_fit(
        overflow, survivors,
        "These job titles didn't fit into the 3 primary clusters below and need a home:",
    )
    return [(label, *members, *buckets[label]) for label, *members in survivors]


def _default_label(roles: list[str]) -> str:
    """Fallback family name when the model gave none: the first role title.
    Never empty -- RoleFamily.name is NOT NULL and the card renders it."""
    return (roles[0] if roles else "Target roles").strip() or "Target roles"


def cluster_target_roles(target_roles: list[str]) -> list[tuple[str, list[str]]]:
    """Group target roles into 1-3 (label, roles) clusters of similar job
    function, for SEEDING families only -- see the module docstring. Fails open
    to one cluster on 0-1 roles or any LLM/parsing issue."""
    roles = [r.strip() for r in target_roles if r and r.strip()]
    if not roles:
        return []
    if len(roles) == 1:
        return [(_default_label(roles), roles)]
    try:
        clusters = _cluster_target_roles_cached(tuple(sorted(set(roles))))
    except Exception:
        return [(_default_label(roles), roles)]
    return [(c[0], list(c[1:])) for c in clusters if len(c) > 1]


def _seed_new_families(db: Session, profile_id: int, ungrouped: list[ProfileAttribute]) -> None:
    """True first-time seed (the profile has no families at all yet): cluster
    the ungrouped roles from scratch and spawn a new RoleFamily per cluster --
    see cluster_target_roles."""
    by_name: dict[str, RoleFamily] = {}
    next_pos = 0
    for label, roles in cluster_target_roles([a.value for a in ungrouped]):
        family = by_name.get(label.lower())
        if family is None:
            family = RoleFamily(profile_id=profile_id, name=label,
                                 tier=FAMILY_TIER_DEFAULT, position=next_pos)
            next_pos += 1
            db.add(family)
            db.flush()  # need family.id below
            by_name[label.lower()] = family
        wanted = {r.lower() for r in roles}
        for attr in ungrouped:
            if attr.value.strip().lower() in wanted:
                attr.family_id = family.id
    # Any role the clustering somehow didn't place (it dedupes case-
    # insensitively, so exact-duplicate rows can fall through) still must not
    # be left invisible to the engine -- park it in the first family.
    leftovers = [a for a in ungrouped if a.family_id is None]
    if leftovers:
        home = next(iter(by_name.values()), None) or RoleFamily(
            profile_id=profile_id, name=_default_label([a.value for a in leftovers]),
            tier=FAMILY_TIER_DEFAULT, position=next_pos)
        if home.id is None:
            db.add(home)
            db.flush()
        for attr in leftovers:
            attr.family_id = home.id


def _assign_into_existing_families(
    db: Session, profile_id: int, existing: list[RoleFamily], ungrouped: list[ProfileAttribute]
) -> None:
    """The profile already has at least one family -- never spawn a new one as
    a side effect of tidying up ungrouped roles (a regenerate, a suggest-added
    role, or anything else that can leave a target_role without a family_id).
    Family count/identity only ever changes via an explicit user action (Add
    family / a family's own delete) -- this just slots new roles into whichever
    existing family they genuinely fit best, via _assign_by_fit."""
    if len(existing) == 1:
        # Nothing to decide -- the candidate's only stream is the answer.
        for attr in ungrouped:
            attr.family_id = existing[0].id
        return

    members = db.execute(
        select(ProfileAttribute).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type == "target_role",
            ProfileAttribute.family_id.in_([f.id for f in existing]),
        )
    ).scalars().all()
    members_by_family: dict[int, list[str]] = {f.id: [] for f in existing}
    for a in members:
        members_by_family[a.family_id].append(a.value)
    targets = [(f.name, *members_by_family[f.id]) for f in existing]

    titles = sorted({a.value.strip() for a in ungrouped if a.value.strip()})
    buckets = _assign_by_fit(
        titles, targets,
        "The candidate just added these new job titles to their search -- assign each to "
        "whichever of their EXISTING search streams below it best fits:",
    )
    family_by_name = {f.name: f for f in existing}
    value_to_family_id = {
        title.lower(): family_by_name[label].id
        for label, titles_in_bucket in buckets.items()
        for title in titles_in_bucket
    }
    fallback_id = existing[0].id
    for attr in ungrouped:
        attr.family_id = value_to_family_id.get(attr.value.strip().lower(), fallback_id)


def seed_families_from_groups(
    db: Session, profile_id: int, groups: list[dict]
) -> list[ProfileAttribute]:
    """Create RoleFamily rows + their target_role attributes directly from the
    formation understand-call's output ([{label, roles}]), returning the created
    target_role rows. The primary seed path now, replacing the old "generate a
    flat target_role list, then re-cluster it" two-step: the understand call
    already decided the grouping with full context in one pass, which is what
    removes the over-split / over-merge / single-role-family failures that
    re-clustering a flat list introduced.

    No-op (returns []) if the profile already has families -- a genuine re-parse
    keeps the user's edited families and lets ensure_families re-slot any new
    roles instead. Does not commit -- formation owns the transaction."""
    if list_families(db, profile_id):
        return []

    existing_targets = {
        v.strip().lower()
        for v in db.execute(
            select(ProfileAttribute.value).where(
                ProfileAttribute.profile_id == profile_id,
                ProfileAttribute.type == "target_role",
            )
        ).scalars().all()
    }
    created: list[ProfileAttribute] = []
    position = 0
    for group in groups[:MAX_ROLE_CLUSTERS]:
        roles = [str(r).strip() for r in (group.get("roles") or []) if str(r).strip()]
        if not roles:
            continue
        label = str(group.get("label") or "").strip() or _default_label(roles)
        family = RoleFamily(
            profile_id=profile_id, name=label,
            tier=FAMILY_TIER_DEFAULT, position=position,
        )
        position += 1
        db.add(family)
        db.flush()  # need family.id for the target_role rows below
        for r in roles:
            if r.lower() in existing_targets:
                continue
            existing_targets.add(r.lower())
            attr = ProfileAttribute(
                profile_id=profile_id, type="target_role", value=r,
                family_id=family.id, source="ai_suggested", confirmed=False,
            )
            db.add(attr)
            created.append(attr)
    db.flush()
    return created


def ensure_families(db: Session, profile_id: int) -> list[RoleFamily]:
    """Idempotently give every target_role a family. Safe (and cheap) to call
    on any read path: it does nothing at all -- no LLM call, no write -- once
    every target_role has a family_id, which is the steady state after the
    first call.

    Spawns new families via LLM clustering ONLY on a true first-time seed (the
    profile has no families yet at all -- a fresh CV parse, or a profile that
    predates this table). Once a profile has any family, later ungrouped roles
    (e.g. from a profile-wide "regenerate target roles", which doesn't know
    about family boundaries) are assigned into the EXISTING family/families
    instead -- see _assign_into_existing_families. This used to always re-
    cluster ungrouped roles from scratch regardless of whether families already
    existed, which could silently spawn a second family the candidate never
    asked for (the fresh clustering call has no memory of the original family,
    so its new label rarely matches the old one by name).

    Returns the profile's families in display order."""
    ungrouped = db.execute(
        select(ProfileAttribute).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type == "target_role",
            ProfileAttribute.family_id.is_(None),
        )
    ).scalars().all()

    if ungrouped:
        existing = list_families(db, profile_id)
        if existing:
            _assign_into_existing_families(db, profile_id, existing, ungrouped)
        else:
            _seed_new_families(db, profile_id, ungrouped)
        db.commit()

    return list_families(db, profile_id)


def list_families(db: Session, profile_id: int) -> list[RoleFamily]:
    """The profile's families in display order (position, then id as a stable
    tiebreak for rows seeded in one batch)."""
    return list(db.execute(
        select(RoleFamily)
        .where(RoleFamily.profile_id == profile_id)
        .order_by(RoleFamily.position, RoleFamily.id)
    ).scalars().all())


def _reconcile_prompt(
    old_name: str, new_name: str | None, remaining: list[str], cv_summary: str
) -> str:
    """Builds the reconciliation prompt as its own function (same reason as
    _cluster_prompt above) so a diagnostic harness can print the exact text sent
    to the model -- see tests/profile_formation_harness.py."""
    change_line = (
        f'The role family "{old_name}" was just DELETED.' if new_name is None
        else f'The role family "{old_name}" was just RENAMED to "{new_name}".'
    )
    return f"""{change_line}
Remaining role families for this candidate: {', '.join(remaining) or 'none'}.

Below is a background/evidence brief used for job-matching. Check ONLY whether it
references the old family name/theme above in a way that's now stale.
- If it does not reference that name/theme at all, return the text completely
  UNCHANGED, character for character.
- If deleted and referenced: remove just that reference (and any sentence that
  becomes meaningless without it) -- touch nothing else.
- If renamed and the theme is genuinely the same: swap the old name for the new
  one wherever referenced. If the new name reflects a meaningfully different
  theme, remove the stale reference instead of relabeling it.
Never invent content, never alter anything unrelated to this one name/theme.

Text:
{cv_summary}

Return ONLY JSON: {{"text": "..."}}"""


def _summary_is_raw_cv(profile: Profile) -> bool:
    """True when cv_summary is just the verbatim CV text (the short-CV path --
    see parsing/formation, which store the raw text as cv_summary rather than
    paying for a compression). cv_summary is truncated shorter than cv_text, so
    an uncompressed summary is always a leading slice of cv_text; an AI-written
    compression never is. Reconciling a family-name reference out of the
    candidate's OWN raw CV would be silently editing their document, and is
    pointless besides -- so the reconcile is skipped in that case."""
    summary = (profile.cv_summary or "").strip()
    cv_text = (profile.cv_text or "").strip()
    return bool(summary) and bool(cv_text) and cv_text.startswith(summary)


def reconcile_summary_after_family_change_bg(
    profile_id: int, old_name: str, new_name: str | None
) -> None:
    """Background-task entrypoint for the reconcile: the request's own db session
    is already closed by the time a FastAPI BackgroundTask runs (get_db yields
    then closes), so open a fresh session for it. This is what keeps a family
    rename/delete instant -- the cheap-model patch used to run synchronously on
    the request, adding a whole LLM round-trip to a click that should just remove
    a card. See routers/families.py."""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        reconcile_summary_after_family_change(db, profile_id, old_name, new_name)
    finally:
        db.close()


def reconcile_summary_after_family_change(
    db: Session, profile_id: int, old_name: str, new_name: str | None
) -> None:
    """Cheap-model patch of profile.cv_summary after a family rename/delete, so a
    stale family-name/theme reference doesn't linger in what the final judge
    reads. Never a full regeneration -- leaves the text UNCHANGED unless it
    actually references the old name/theme. Fails open: llm_json never raises,
    and an empty/malformed response just leaves cv_summary untouched.

    Runs off-request via reconcile_summary_after_family_change_bg so the family
    edit itself returns instantly. Skipped entirely when cv_summary is just the
    raw CV (see _summary_is_raw_cv) -- there's nothing to safely rewrite there.

    Not called for add_family (a new family's roles already reach the judge
    directly via that cluster's own "Target roles: ..." line -- see
    snapshot.py -- independent of this narrative text) or regenerate_family
    (that changes a family's roles, not its name)."""
    profile = db.get(Profile, profile_id)
    if not profile or not (profile.cv_summary or "").strip():
        return
    if _summary_is_raw_cv(profile):
        return
    remaining = [f.name for f in list_families(db, profile_id) if f.name != old_name]
    data = llm_json(
        _reconcile_prompt(old_name, new_name, remaining, profile.cv_summary),
        model=CHEAP_MODEL,
    )
    patched = str(data.get("text") or "").strip()
    if patched:
        profile.cv_summary = patched
        db.commit()


def regenerate_family(db: Session, family: RoleFamily) -> list[ProfileAttribute]:
    """Refresh ONE family's un-pinned target roles from its title + the
    candidate's background -- the per-family analogue of
    profile_intel.ensure_profile_intel's profile-wide regenerate, scoped to a
    single card instead of every target role at once.

    Pinned roles in this family are never touched (that's the whole point of
    pinning -- see RoleFamilyCard.tsx), and no other family's roles are read or
    written. On an empty/failed LLM response, existing roles are left exactly as
    they were rather than being cleared with nothing to replace them."""
    existing = db.execute(
        select(ProfileAttribute).where(
            ProfileAttribute.profile_id == family.profile_id,
            ProfileAttribute.type == "target_role",
            ProfileAttribute.family_id == family.id,
        )
    ).scalars().all()
    pinned = [a for a in existing if a.pinned]
    stale = [a for a in existing if not a.pinned]

    background = _background_text(_grouped_values(db, family.profile_id, _BACKGROUND_TYPES))
    pinned_block = (
        "Roles already pinned in this family -- do not repeat or reword them, only propose "
        "complementary/additional titles that also fit the theme below:\n"
        + "\n".join(f"- {a.value}" for a in pinned)
        if pinned else "None pinned in this family yet."
    )
    data = llm_json(
        f"""Candidate background:
{background or 'No background on file yet.'}

This is ONE role family (search stream) the candidate is running, titled "{family.name}"
-- propose NEW job titles that fit specifically THIS theme. The candidate may have other,
unrelated interests elsewhere; you are only shown this one family, so don't hedge toward a
generic blend, commit to this theme.

{pinned_block}

Propose {FAMILY_REGEN_TARGET_COUNT} distinct, meaningfully different, board-standard job
titles that genuinely fit this family's theme. Cover the real breadth of specializations,
seniority phrasings, and closely-adjacent titles that plausibly belong here -- a family
card with only one or two roles is usually too narrow, so don't settle for 2-3 safe picks
when the theme supports more. Still don't pad with near-duplicate titles just to hit the
count.
{_TARGET_ROLE_GUIDANCE}
Output ONLY JSON: {{"target_roles": ["..."]}}""",
        model=MID_MODEL,
    )
    proposed = data.get("target_roles") if isinstance(data.get("target_roles"), list) else []
    proposed = [str(t).strip() for t in proposed if str(t).strip()][:FAMILY_REGEN_TARGET_COUNT]
    if not proposed:
        return []  # transient call failure -- leave the family untouched

    kept_lower = {a.value.strip().lower() for a in pinned}
    new_attrs: list[ProfileAttribute] = []
    for value in proposed:
        if value.lower() in kept_lower:
            continue
        kept_lower.add(value.lower())
        attr = ProfileAttribute(
            profile_id=family.profile_id, type="target_role", value=value,
            family_id=family.id, source="ai_suggested", confirmed=False, pinned=False,
        )
        db.add(attr)
        new_attrs.append(attr)

    for attr in stale:
        db.delete(attr)

    db.commit()
    for a in new_attrs:
        db.refresh(a)
    return new_attrs


# Reed/Adzuna do literal keyword matching on a title, not semantic search --
# measured on a real profile (title_breadth_test2.py), short generic
# 2-word titles fuzzy-matched into unrelated professions ("Analytics Analyst"
# pulled "Analytical Chemist"/"Shift Analytical Chemist"; "MI Analyst" pulled
# a "Compliance Associate"; "CRM Analyst" pulled "Senior CRM Manager"/"Sales
# Support Admin"), while longer, more qualified phrasings stayed ~90%+
# on-topic ("Data Insight Analyst", "Junior Data Analyst", "Data & Insights
# Analyst"). This rule exists specifically to steer top_up_family_titles away
# from the precision cost that test found, not a general style preference.
_TITLE_SPECIFICITY_RULE = (
    "These titles are used as literal keyword search terms against job boards that do "
    "plain text matching, not semantic search -- a short, generic 1-2-word title (e.g. "
    "\"Analytics Analyst\", \"CRM Analyst\", \"MI Analyst\") measurably pulls in unrelated "
    "professions that happen to share a word (analytical chemistry, CRM management, "
    "compliance). Prefer longer, more specific, still board-standard phrasings that stay "
    "unambiguous as a search term (e.g. \"Data Insight Analyst\", \"Junior Data Analyst\")."
)


def top_up_family_titles(
    db: Session, family: RoleFamily, target: int | None = None
) -> list[ProfileAttribute]:
    """Additively top up ONE family's target roles toward a reserve pool
    (default FAMILY_TITLE_RESERVE_TARGET), without touching any existing
    title -- pinned or not. The ADDITIVE counterpart to regenerate_family
    above, which replaces every un-pinned title; this exists so a weak search
    run's wider discovery term window (full_auto.TERMS_PER_RUN_WIDE, see
    engine._weak_reference_run) has more titles to draw from, without
    disturbing titles the candidate or an earlier regenerate already chose.

    Idempotent and cheap once a family is at target: a single COUNT-shaped
    SELECT and an early return, no LLM call. Called on every search run
    (engine.run_search_task, before build_snapshot) and once in the
    background right after CV parsing (see top_up_new_families_bg) --
    both are safe to call repeatedly for exactly this reason."""
    target = target or FAMILY_TITLE_RESERVE_TARGET
    existing = db.execute(
        select(ProfileAttribute).where(
            ProfileAttribute.profile_id == family.profile_id,
            ProfileAttribute.type == "target_role",
            ProfileAttribute.family_id == family.id,
        )
    ).scalars().all()
    need = target - len(existing)
    if need <= 0:
        return []

    background = _background_text(_grouped_values(db, family.profile_id, _BACKGROUND_TYPES))
    existing_block = (
        "Titles already in this family -- do not repeat or reword them, only propose "
        "distinct ADDITIONAL titles that also fit the theme below:\n"
        + "\n".join(f"- {a.value}" for a in existing)
        if existing else "None yet."
    )
    data = llm_json(
        f"""Candidate background:
{background or 'No background on file yet.'}

This is ONE role family (search stream) the candidate is running, titled "{family.name}"
-- propose NEW job titles that fit specifically THIS theme, to ADD to the ones it already
has (not replace them). The candidate may have other, unrelated interests elsewhere; you
are only shown this one family, so don't hedge toward a generic blend, commit to this
theme.

{existing_block}

Propose up to {need} distinct, meaningfully different, board-standard job titles that
genuinely fit this family's theme and aren't already covered above. Cover seniority
phrasings and closely-adjacent specialisations that plausibly belong here. Don't pad with
near-duplicate titles just to hit the count -- fewer genuinely distinct titles is fine.
{_TITLE_SPECIFICITY_RULE}
{_TARGET_ROLE_GUIDANCE}
Output ONLY JSON: {{"target_roles": ["..."]}}""",
        model=MID_MODEL,
    )
    proposed = data.get("target_roles") if isinstance(data.get("target_roles"), list) else []
    proposed = [str(t).strip() for t in proposed if str(t).strip()][:need]
    if not proposed:
        return []  # transient call failure -- leave the family untouched

    kept_lower = {a.value.strip().lower() for a in existing}
    new_attrs: list[ProfileAttribute] = []
    for value in proposed:
        if value.lower() in kept_lower:
            continue
        kept_lower.add(value.lower())
        attr = ProfileAttribute(
            profile_id=family.profile_id, type="target_role", value=value,
            family_id=family.id, source="ai_suggested", confirmed=False, pinned=False,
        )
        db.add(attr)
        new_attrs.append(attr)

    if not new_attrs:
        return []
    db.commit()
    for a in new_attrs:
        db.refresh(a)
    return new_attrs


def top_up_new_families_bg(profile_id: int) -> None:
    """Background-task entrypoint for top_up_family_titles, run right after a
    CV parse (see routers/onboarding.py::_run_formation). The request's own
    db session is already closed by the time a FastAPI BackgroundTask runs,
    so this opens a fresh one -- same pattern as
    reconcile_summary_after_family_change_bg.

    Deliberately NOT called inline in formation.persist_formation: that
    function runs synchronously on the parse-cv/parse-text request, and
    CLAUDE.md documents real prior effort spent cutting formation latency --
    adding a blocking MID_MODEL call per family there would undo it. The
    candidate sees their initial titles immediately; the reserve pool fills
    in a few seconds later, invisibly. (A thin family that predates this
    feature, or one hand-edited down, is still covered -- see the top-up call
    in engine.run_search_task, which runs synchronously so THAT run benefits.)"""
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        for f in ordered_for_engine(list_families(db, profile_id)):
            top_up_family_titles(db, f)
    finally:
        db.close()


def ordered_for_engine(families: list[RoleFamily]) -> list[RoleFamily]:
    """Active families only, in the order the engine should spend its
    per-cluster budget: display order. Inactive families are dropped here --
    the engine creates no cluster/discovery stream for them at all (see
    snapshot._role_groups).

    The MAX_ROLE_CLUSTERS overflow-merge only matters when a profile has more
    ACTIVE families than the cap -- see snapshot.build_snapshot, which merges
    the overflow into the last cluster in this order."""
    return sorted(
        (f for f in families if f.tier != "inactive"),
        key=lambda f: (f.position, f.id),
    )
