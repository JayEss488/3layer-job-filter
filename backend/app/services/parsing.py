"""CV / free-text parsing into normalised profile_attributes.

Both parse-cv and parse-text funnel into parse_text_to_attributes: extract text,
ask the model for JSON keyed by attribute type (crucially separating past_role
from target_role), then insert one row per item as confirmed=false."""
import io

from sqlalchemy.orm import Session

from ..config import ATTRIBUTE_TYPES
from ..models import ProfileAttribute
from .llm import STRONG_MODEL, llm_json

_PARSE_SYSTEM = (
    "You extract a structured job-search profile from a candidate's CV or notes. "
    "Be faithful to the source: do NOT inflate, exaggerate, or invent claims. "
    "Crucially, separate what the candidate HAS done (past_role) from what they "
    "WANT next (target_role) — never conflate them."
)


def _parse_prompt(text: str) -> str:
    return f"""Extract a job-search profile from the document below.
Return ONLY a JSON object with these keys (omit a key if nothing applies; never invent):
{{
  "past_role":   [{{"value": "job title only the candidate has actually held, e.g. 'Sales Associate' -- never append the employer name (not 'Sales Associate - Acme Corp')", "proficiency": "duration/commitment level, ONLY if the source states or clearly implies it, e.g. '3 years' vs 'one-off, one week volunteer stint' -- omit this key entirely if not stated, never invent a duration"}}],
  "skill":       [{{"value": "a concrete skill/tool, max 12 total", "proficiency": "depth signal, ONLY if the source states or clearly implies it, e.g. '5+ years, daily use' or 'used once in a course project' -- omit this key entirely if not stated, never invent a duration or expertise level"}}],
  "experience":  ["short achievement bullets, e.g. 'Led team of 8'; include a duration/frequency qualifier only if the source text states it"],
  "seniority":   ["one of: Junior, Mid, Senior, Lead, Director, C-Suite"],
  "target_role": ["4-6 job titles this candidate should realistically target next. If the document explicitly states an ambition, include it, but do not stop there: weigh the FULL picture -- leadership/project experience, applied use of tools (not just a listed skill), academic background, languages, communications/organisational work -- as heavily as a formal skills-inventory section. Do not default to generic titles that only match keywords in a skills list if the candidate's strongest, most differentiated evidence points elsewhere (e.g. a small self-taught coding project is weaker evidence than a led, evidenced project with real outcomes)."],
  "location":    ["city/region and/or work types like Remote, Hybrid, On-site"],
  "salary":      ["a single range like '70000-90000' only if clearly stated"],
  "custom":      ["any hard constraints stated, e.g. 'visa sponsorship required'"]
}}

Note: "past_role" and "skill" items are objects with "value" and an optional "proficiency" -- every other key is a plain list of strings.

Document:
{text[:12000]}"""


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
