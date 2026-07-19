"""CV / free-text parsing into normalised profile_attributes.

Both parse-cv and parse-text funnel into parse_text_to_attributes: extract text,
ask the model for JSON keyed by attribute type (crucially separating past_role
from target_role), then insert one row per item as confirmed=false."""
import io

from sqlalchemy.orm import Session

from ..config import ATTRIBUTE_TYPES
from ..models import ProfileAttribute
from .llm import STRONG_MODEL, llm_json


class CVParseFailed(Exception):
    """Raised when the LLM extraction call itself failed (network/API/schema error),
    as opposed to a call that succeeded but genuinely found nothing -- callers should
    surface this to the user rather than let it look like an empty CV."""

_PARSE_SYSTEM = (
    "You extract a structured job-search profile from a candidate's CV or notes. "
    "Be faithful to the source: do NOT inflate, exaggerate, or invent claims. "
    "Crucially, separate what the candidate HAS done (past_role) from what they "
    "WANT next (target_role) — never conflate them. A leadership title from a "
    "student club, society, or volunteer activity (e.g. 'President of the Finance "
    "Society') is still a past_role, but it is NOT employment — mark it 'Informal' "
    "per the schema below rather than treating it like a paid job. "
    "Be equally skeptical when reading skills: actively look for hedging or context "
    "language around each one -- 'self-taught', 'personal project', 'sandbox', "
    "'Developer Edition', 'prototype', 'course project', 'practiced with AI "
    "assistance', 'not yet used professionally' -- and reflect it honestly via "
    "evidence_origin below rather than smoothing it into a confident-sounding skill "
    "claim. A skill built entirely through unpaid, self-directed, academic, or "
    "AI-assisted work is NOT the same evidence as one exercised in paid/commercial "
    "work, even when the depth (proficiency) is similar."
)


def _parse_prompt(text: str) -> str:
    return f"""Extract a job-search profile from the document below.
Return ONLY a JSON object with these keys (omit a key if nothing applies; never invent):
{{
  "past_role":   [{{"value": "job title only the candidate has actually held. For genuine paid employment, the title alone, e.g. 'Sales Associate' -- never append the employer name (not 'Sales Associate - Acme Corp'). For an unpaid/informal role (student club, society, volunteering -- see proficiency below), a bare generic title like 'President', 'Co-Lead', or 'Volunteer' is meaningless out of context, so name the SPECIFIC organisation/cause/activity it belongs to instead, e.g. 'President of the Finance Society' or 'Volunteer at [named cause/org]' -- keep it to a few words, not a full sentence. If the source gives no specific name to attach, leave that item out entirely rather than extracting a bare, context-free title", "proficiency": "ONLY the exact literal string 'Informal' -- include this key set to 'Informal' if and only if the role was NOT paid employment (e.g. a student club, society, volunteer, or other unpaid/extracurricular position), even if a leadership title like 'President' or 'Founder' was held. Omit this key entirely for genuine paid employment/internships -- never invent it, never use any other value"}}],
  "skill":       [{{"value": "a concrete skill/tool, max 16 total -- include every skill the source names explicitly even if the candidate describes it as weak, informal, self-taught, or their 'least' developed area; reflect that self-assessment via proficiency (e.g. 'Familiar' or 'One-time') rather than omitting the skill", "proficiency": "ONLY one of the exact literal strings 'Expert', 'Proficient', 'Familiar', 'One-time' -- pick the closest match to the depth signal the source states or clearly implies (e.g. '5+ years, daily use' -> 'Expert'; 'used once in a course project' -> 'One-time'; explicitly self-described as weak/least-trained -> 'Familiar'). Omit this key entirely if no depth signal is stated or implied, never invent one", "evidence_origin": "ONLY one of the exact literal strings 'Commercial', 'Self-directed', 'Academic', 'AI-assisted' -- where the skill's depth was actually earned, DISTINCT from proficiency (which grades how deep, not how earned). 'Commercial' = used in a paid job/internship. 'Self-directed' = personal project, sandbox, side build, self-taught, not paid or in production (e.g. 'built a Developer Edition org', 'practiced on my own'). 'Academic' = coursework/university assignment. 'AI-assisted' = the source states the work leaned on AI tooling rather than the candidate's own independent reasoning (e.g. 'wrote a few AI-assisted SQL queries'). Omit this key entirely if the source gives no origin signal either way -- never invent one, and never default to 'Commercial' just because the skill is listed under a job"}}],
  "qualification": ["a formal qualification: degree (including classification/honours if the source states it, e.g. 'First Class Honours BSc Physics, Durham University'), professional certification, or license. Only include what the source explicitly states, never invent or infer a classification that isn't written down"],
  "seniority":   ["one of: Junior, Mid, Senior, Lead, Director, C-Suite"],
  "sector_target": ["explicit sector, industry, mission, or cause-targeting language distinct from a job title -- e.g. named industries, causes, or organisation types the candidate has written about (cover-letter angles, 'I want to move into X'), captured even when no matching target_role title exists yet. Do not invent this from a skills list alone -- only include it if the source actually expresses a sector/mission preference"],
  "location":    ["city/region and/or work types like Remote, Hybrid, On-site"],
  "salary":      ["a single range like '70000-90000'. Use the figure the candidate explicitly states if there is one. If none is stated but their seniority level (see above) and role/sector/location are clear enough to judge, give a plausible rough market-rate range for that combination instead of leaving this empty -- salary and seniority band correlate strongly (e.g. a Junior candidate typically commands a materially lower range than a Senior one in the same field), so use that inference rather than omitting the field. Keep an inferred range broad, not a narrow point-estimate -- real market rates for any given seniority/role/location vary widely by company size, sector, and specific responsibilities, so the span should be wide enough to not filter out plausible genuine openings (e.g. for a Junior role, '22000-40000' reflects the real spread better than a narrow '28000-38000'). Only omit this key entirely if there truly isn't enough in the document to judge even roughly (e.g. no seniority signal at all)"],
  "custom":      ["any hard constraints stated, e.g. 'visa sponsorship required'"],
  "must_have":   ["a non-negotiable the candidate explicitly states they REQUIRE in a role, short and concrete, e.g. 'visa sponsorship', 'fully remote', 'salary above 40k'. Only include what the source actually states as a hard requirement -- never infer one from a skills list or a nice-to-have"],
  "avoid":       ["something the candidate explicitly states they do NOT want and would reject a role over, short and concrete, e.g. 'no cold-calling', 'no night shifts', 'no commission-only pay'. Only include explicit refusals the source states -- never invent one"]
}}

Note: "past_role" items are objects with "value" and an optional "proficiency". "skill" items are objects with "value" and optional "proficiency"/"evidence_origin" keys. Every other key is a plain list of strings. target_role is deliberately NOT extracted here -- profile_intel.py generates it separately (and supersedes any guess this call would make) from the full profile once parsing finishes.

Document:
{text[:40000]}"""


_SUMMARY_SYSTEM = (
    "You compress a candidate's CV/notes into a short, dense paragraph of extra "
    "background context for another AI to read alongside a normalised skills/role "
    "list. Preserve specific, differentiating details that list would lose -- named "
    "projects, concrete outcomes, scope of leadership, domain-specific nuance, tools "
    "used in context. Do not restate a generic skills inventory or job-title list; "
    "that is supplied separately. Be faithful to the source -- never invent or embellish. "
    "If the source describes ONE complete, verifiable, end-to-end piece of work (e.g. a "
    "full project from raw data/input through to a finished, concrete output), prioritise "
    "describing that over generic bullet points -- it is stronger evidence than a list of "
    "isolated skills and should not be crowded out by less specific detail. Also preserve "
    "any hedging or self-assessment language attached to a skill or project -- 'self-taught', "
    "'personal project', 'sandbox', 'prototype', 'AI-assisted', 'not yet used professionally' "
    "-- rather than resolving it into more confident-sounding prose than the source supports."
)


def summarize_cv_text(text: str) -> str:
    """Compress the raw CV/notes into a short paragraph of extra context for the
    final job-match judge (see snapshot.py::cv_text_base), kept separate from the
    structured attribute rows so the editable-fields model doesn't change -- this
    just gives the judge back some of the nuance that parsing into typed rows
    necessarily drops. Returns "" on empty input or an LLM failure (snapshot.py
    then falls back to the attribute-only text, same as before this existed)."""
    if not text or not text.strip():
        return ""
    data = llm_json(
        f"""Compress the document below into ONE paragraph (max 100 words) of dense
background context -- concrete projects, achievements, leadership scope, domain
nuance -- that a plain skills/role list would lose. No preamble, no bullet points.

Document:
{text[:40000]}

Return ONLY JSON: {{"summary": "..."}}""",
        system=_SUMMARY_SYSTEM,
        model=STRONG_MODEL,
    )
    return str(data.get("summary", "")).strip()[:1200]


def extract_text_from_upload(filename: str, raw: bytes) -> str:
    """Pull plain text out of a PDF / DOCX / txt upload."""
    name = (filename or "").lower()
    if name.endswith(".pdf"):
        import pdfplumber

        with pdfplumber.open(io.BytesIO(raw)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    if name.endswith(".docx"):
        import docx

        document = docx.Document(io.BytesIO(raw))
        return "\n".join(p.text for p in document.paragraphs)
    # Fallback: treat as plain text.
    return raw.decode("utf-8", errors="ignore")


def parse_text_to_attributes(
    db: Session, profile_id: int, text: str, source: str
) -> list[ProfileAttribute]:
    """Parse text and insert each extracted item as an unconfirmed attribute row."""
    if not text or not text.strip():
        return []

    data = llm_json(_parse_prompt(text), system=_PARSE_SYSTEM, model=STRONG_MODEL)
    if not data:
        # llm_json returns {} both on a genuine call failure and (in principle) on a
        # model deciding literally nothing applies -- for real CV/notes text the
        # latter is not realistic (some field always matches), so treat this as a
        # failure rather than silently persisting zero attributes.
        raise CVParseFailed("The AI parsing call failed -- see server logs for the underlying error")

    created: list[ProfileAttribute] = []
    seen: set[tuple[str, str]] = set()
    for attr_type in ATTRIBUTE_TYPES:
        if attr_type not in data:
            continue
        values = data[attr_type]
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            continue
        for item in values:
            proficiency = None
            evidence_origin = None
            if isinstance(item, dict):
                value = str(item.get("value", "")).strip()
                proficiency = item.get("proficiency")
                if proficiency is not None:
                    proficiency = str(proficiency).strip() or None
                evidence_origin = item.get("evidence_origin")
                if evidence_origin is not None:
                    evidence_origin = str(evidence_origin).strip() or None
            else:
                value = str(item).strip()
            if not value:
                continue
            key = (attr_type, value.lower())
            if key in seen:
                continue
            seen.add(key)
            attr = ProfileAttribute(
                profile_id=profile_id,
                type=attr_type,
                value=value,
                source=source,
                confirmed=False,
                proficiency=proficiency,
                evidence_origin=evidence_origin,
            )
            db.add(attr)
            created.append(attr)

    db.flush()  # populate ids for the response
    return created
