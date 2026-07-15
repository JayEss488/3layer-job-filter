"""Role families: the user-editable streams that ARE the engine's clusters.

The pipeline scores, gates, and judges each cluster independently, so a
candidate targeting genuinely different fields is judged fairly on each instead
of against a blend of both. Those clusters used to be re-derived on every run by
an LLM call over the flat target_role list. They are now rows the candidate owns
and edits (models.RoleFamily), and the LLM clustering below survives only as the
SEEDING step: it runs when a profile has target roles that belong to no family
yet -- a fresh CV parse, or a profile that predates this table -- and never again
unless new ungrouped roles appear.

That swap is what makes the grouping stable and inspectable: previously two runs
could silently cluster the same roles differently (the cache is per-process), and
the candidate had no way to say "these two are one search, that one is separate."
"""
from functools import lru_cache

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import FAMILY_REGEN_TARGET_COUNT, FAMILY_TIER_DEFAULT, MAX_ROLE_CLUSTERS
from ..models import ProfileAttribute, RoleFamily
from .llm import STRONG_MODEL, llm_json
from .profile_intel import _BACKGROUND_TYPES, _TARGET_ROLE_GUIDANCE, _background_text, _grouped_values


@lru_cache(maxsize=256)
def _cluster_target_roles_cached(roles_key: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    """LLM grouping of a role set into clusters, cached for this process's
    lifetime per unique (sorted, deduped) role set. Falls open to a single
    cluster on anything malformed."""
    roles = list(roles_key)
    data = llm_json(
        f"""Group these job title strings into clusters by underlying job function /
career path, AT MOST 3 clusters total. Split into separate clusters whenever two
titles represent meaningfully different job functions -- even within the same
broad field. For example, "Communications Officer" and "Editorial Assistant" are
different functions (external-facing comms/PR vs. content editing) and belong in
SEPARATE clusters, not merged into one "Communications & Editorial" cluster. Only
merge titles into the same cluster when they are genuinely the same underlying
job function -- different seniority phrasings or near-duplicate wording of the
same role. For example, "CAD Engineering Intern", "Electrical Engineering
Intern", and "Manufacturing Engineering Intern" are all the same underlying
engineering-intern function and belong in ONE cluster, not three.
If the titles genuinely span more than 3 distinct job functions, keep the 3
most substantively supported (most titles, or most central to the set) and fold
the rest into whichever surviving cluster is the closest fit -- never invent a
4th cluster. Every title must appear in exactly one cluster.
Also give each cluster a short human label (2-3 words) naming the job function
it represents, e.g. "Data Analyst" or "Electrical Engineering".
Output ONLY JSON: {{"clusters": [{{"label": "...", "titles": ["title a", "title b"]}}]}}
Titles: {', '.join(roles)}"""
    )
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
    # output -- merge any excess into the last cluster rather than let an
    # unbounded cluster count reach the pipeline's per-cluster fan-out.
    if len(out) > MAX_ROLE_CLUSTERS:
        overflow = tuple(r for cluster in out[MAX_ROLE_CLUSTERS:] for r in cluster[1:])
        out = out[:MAX_ROLE_CLUSTERS - 1] + (out[MAX_ROLE_CLUSTERS - 1] + overflow,)
    return tuple(out)


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


def ensure_families(db: Session, profile_id: int) -> list[RoleFamily]:
    """Idempotently give every target_role a family, seeding new ones by LLM
    clustering when needed. Safe (and cheap) to call on any read path: it does
    nothing at all -- no LLM call, no write -- once every target_role has a
    family_id, which is the steady state after the first call.

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
        by_name = {f.name.lower(): f for f in existing}
        next_pos = max((f.position for f in existing), default=-1) + 1
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
        model=STRONG_MODEL,
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


def ordered_for_engine(families: list[RoleFamily]) -> list[RoleFamily]:
    """Families in the order the engine should spend its per-cluster budget:
    core streams first, then display order within a tier.

    Only matters when a profile has more families than MAX_ROLE_CLUSTERS -- see
    snapshot.build_snapshot, which merges the overflow into the last cluster.
    Ordering core-first means a secondary stream is what gets merged away, not
    whichever family happened to be created last."""
    return sorted(families, key=lambda f: (0 if f.tier == "core" else 1, f.position, f.id))
