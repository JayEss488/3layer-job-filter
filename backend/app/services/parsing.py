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
    "per the schema below rather than treating it like a paid job."
)


def _parse_prompt(text: str) -> str:
    return f"""Extract a job-search profile from the document below.
Return ONLY a JSON object with these keys (omit a key if nothing applies; never invent):
{{
  "past_role":   [{{"value": "job title only the candidate has actually held. For genuine paid employment, the title alone, e.g. 'Sales Associate' -- never append the employer name (not 'Sales Associate - Acme Corp'). For an unpaid/informal role (student club, society, volunteering -- see proficiency below), a bare generic title like 'President', 'Co-Lead', or 'Volunteer' is meaningless out of context, so name the SPECIFIC organisation/cause/activity it belongs to instead, e.g. 'President of the Finance Society' or 'Volunteer at [named cause/org]' -- keep it to a few words, not a full sentence. If the source gives no specific name to attach, leave that item out entirely rather than extracting a bare, context-free title", "proficiency": "ONLY the exact literal string 'Informal' -- include this key set to 'Informal' if and only if the role was NOT paid employment (e.g. a student club, society, volunteer, or other unpaid/extracurricular position), even if a leadership title like 'President' or 'Founder' was held. Omit this key entirely for genuine paid employment/internships -- never invent it, never use any other value"}}],
  "skill":       [{{"value": "a concrete skill/tool, max 16 total -- include every skill the source names explicitly even if the candidate describes it as weak, informal, self-taught, or their 'least' developed area; reflect that self-assessment via proficiency (e.g. 'Familiar' or 'One-time') rather than omitting the skill", "proficiency": "ONLY one of the exact literal strings 'Expert', 'Proficient', 'Familiar', 'One-time' -- pick the closest match to the depth signal the source states or clearly implies (e.g. '5+ years, daily use' -> 'Expert'; 'used once in a course project' -> 'One-time'; explicitly self-described as weak/least-trained -> 'Familiar'). Omit this key entirely if no depth signal is stated or implied, never invent one"}}],
  "qualification": ["a formal qualification: degree (including classification/honours if the source states it, e.g. 'First Class Honours BSc Physics, Durham University'), professional certification, or license. Only include what the source explicitly states, never invent or infer a classification that isn't written down"],
  "seniority":   ["one of: Junior, Mid, Senior, Lead, Director, C-Suite"],
  "sector_target": ["explicit sector, industry, mission, or cause-targeting language distinct from a job title -- e.g. named industries, causes, or organisation types the candidate has written about (cover-letter angles, 'I want to move into X'), captured even when no matching target_role title exists yet. Do not invent this from a skills list alone -- only include it if the source actually expresses a sector/mission preference"],
  "location":    ["city/region and/or work types like Remote, Hybrid, On-site"],
  "salary":      ["a single range like '70000-90000' only if clearly stated"],
  "custom":      ["any hard constraints stated, e.g. 'visa sponsorship required'"]
}}

Note: "past_role" and "skill" items are objects with "value" and an optional "proficiency" -- every other key is a plain list of strings. target_role is deliberately NOT extracted here -- profile_intel.py generates it separately (and supersedes any guess this call would make) from the full profile once parsing finishes.

Document:
{text[:40000]}"""


_SUMMARY_SYSTEM = (
    "You compress a candidate's CV/notes into a short, dense paragraph of extra "
    "background context for another AI to read alongside a normalised skills/role "
    "list. Preserve specific, differentiating details that list would lose -- named "
    "projects, concrete outcomes, scope of leadership, domain-specific nuance, tools "
    "used in context. Do not restate a generic skills inventory or job-title list; "
    "that is supplied separately. Be faithful to the source -- never invent or embellish."
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
            if isinstance(item, dict):
                value = str(item.get("value", "")).strip()
                proficiency = item.get("proficiency")
                if proficiency is not None:
                    proficiency = str(proficiency).strip() or None
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
            )
            db.add(attr)
            created.append(attr)

    db.flush()  # populate ids for the response
    return created
