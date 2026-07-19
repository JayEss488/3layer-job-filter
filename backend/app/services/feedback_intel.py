"""Turns a candidate's free-text search feedback (profile.search_feedback) into
structured avoid/must_have profile_attributes, when the feedback is actually
actionable. Mirrors parsing.py's extract-then-insert shape, but scoped to the
two hard-filter attribute types and deliberately kept on the cheap model --
this is a small, frequent call (fires on every feedback-box save), not a rare
high-value one like CV parsing."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Profile, ProfileAttribute
from .llm import llm_json

_FEEDBACK_SYSTEM = (
    "You read a candidate's free-text feedback about their recent job-search results "
    "and decide whether it states a concrete, actionable exclusion or requirement for "
    "FUTURE searches -- as opposed to vague commentary, praise, or a one-off complaint "
    "about a single listing with no general rule behind it. "
    "Only extract something when the candidate is clearly stating a rule they want "
    "applied going forward (e.g. 'stop showing sales roles', 'nothing over 4 days a "
    "week in office', 'I need visa sponsorship'). Do NOT invent, infer, or generalise "
    "beyond what they actually wrote -- e.g. 'these three are too junior' is about "
    "specific listings, not a general seniority rule, so extract nothing from it. "
    "When genuinely nothing actionable is stated, return both keys as empty lists -- "
    "do not force an extraction just to have something to return."
)


def _feedback_prompt(feedback_text: str, existing_values: set[str]) -> str:
    existing = ", ".join(sorted(existing_values)) or "(none yet)"
    return f"""Candidate's feedback on recent search results:
"{feedback_text}"

The candidate already has these avoid/must-have filters set -- do NOT propose anything
that duplicates or restates one of these:
{existing}

Return ONLY a JSON object with exactly these two keys, each a list of short, concrete
strings (max 3 items combined across both lists; [] for a key with nothing to add):
{{
  "avoid": ["something the candidate wants EXCLUDED from future results, short and concrete, e.g. 'sales roles', 'night shifts'"],
  "must_have": ["a non-negotiable the candidate wants future results to satisfy, short and concrete, e.g. 'visa sponsorship', 'fully remote'"]
}}"""


def interpret_search_feedback(db: Session, profile_id: int) -> list[ProfileAttribute]:
    """Reads profile.search_feedback and, if it contains an actionable rule,
    inserts new unconfirmed avoid/must_have rows for it. Returns [] both when
    there's nothing to interpret and when the call itself fails -- callers that
    want to distinguish "nothing actionable" from "call failed" don't need to
    here, since either way there's nothing new to show the user."""
    profile = db.get(Profile, profile_id)
    if not profile or not profile.search_feedback or not profile.search_feedback.strip():
        return []

    existing = db.execute(
        select(ProfileAttribute.value).where(
            ProfileAttribute.profile_id == profile_id,
            ProfileAttribute.type.in_(["avoid", "must_have"]),
        )
    ).scalars().all()
    existing_values = {v.strip().lower() for v in existing if v and v.strip()}

    data = llm_json(
        _feedback_prompt(profile.search_feedback.strip(), existing_values),
        system=_FEEDBACK_SYSTEM,
    )
    if not data:
        # llm_json returns {} both on a call failure and (in principle) on a
        # well-formed-but-empty response -- but a well-formed response always
        # has the "avoid"/"must_have" keys per the prompt, even when both are
        # [], so an empty dict here can only mean the call itself failed.
        return []

    created: list[ProfileAttribute] = []
    seen = set(existing_values)
    for attr_type in ("avoid", "must_have"):
        values = data.get(attr_type)
        if not isinstance(values, list):
            continue
        for item in values:
            if len(created) >= 3:
                break
            value = str(item).strip()[:120]
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            attr = ProfileAttribute(
                profile_id=profile_id,
                type=attr_type,
                value=value,
                source="feedback_derived",
                confirmed=False,
            )
            db.add(attr)
            created.append(attr)

    if created:
        db.commit()
        for attr in created:
            db.refresh(attr)
    return created
