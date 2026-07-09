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
    "WANT next (target_role) — never conflate them. A leadership title from a "
    "student club, society, or volunteer activity (e.g. 'President of the Finance "
    "Society') is still a past_role, but it is NOT employment — mark it 'Informal' "
    "per the schema below rather than treating it like a paid job."
)


def _parse_prompt(text: str) -> str:
    return f"""Extract a job-search profile from the document below.
Return ONLY a JSON object with these keys (omit a key if nothing applies; never invent):
{{
  "past_role":   [{{"value": "job title only the candidate has actually held, e.g. 'Sales Associate' -- never append the employer name (not 'Sales Associate - Acme Corp')", "proficiency": "ONLY the exact literal string 'Informal' -- include this key set to 'Informal' if and only if the role was NOT paid employment (e.g. a student club, society, volunteer, or other unpaid/extracurricular position), even if a leadership title like 'President' or 'Founder' was held. Omit this key entirely for genuine paid employment/internships -- never invent it, never use any other value"}}],
  "skill":       [{{"value": "a concrete skill/tool, max 16 total -- include every skill the source names explicitly even if the candidate describes it as weak, informal, self-taught, or their 'least' developed area; reflect that self-assessment via proficiency (e.g. 'Familiar' or 'One-time') rather than omitting the skill", "proficiency": "ONLY one of the exact literal strings 'Expert', 'Proficient', 'Familiar', 'One-time' -- pick the closest match to the depth signal the source states or clearly implies (e.g. '5+ years, daily use' -> 'Expert'; 'used once in a course project' -> 'One-time'; explicitly self-described as weak/least-trained -> 'Familiar'). Omit this key entirely if no depth signal is stated or implied, never invent one"}}],
  "experience":  [{{"value": "short achievement bullet, e.g. 'Led team of 8'; include a duration/frequency qualifier only if the source text states it. Prioritise leadership/organising achievements (name the scope where stated -- budget, team size, duration, solo vs. team) and concrete technical/problem-solving anecdotes over generic filler", "proficiency": "ONLY the exact literal string 'Personal project' -- include this key set to 'Personal project' if and only if the achievement was NOT paid/commercial work (e.g. a personal coding project, academic project, hackathon, or other self-initiated activity with no employer). Omit this key entirely for paid/commercial work or anything ambiguous -- never invent it, never use any other value"}}],
  "qualification": ["a formal qualification: degree (including classification/honours if the source states it, e.g. 'First Class Honours BSc Physics, Durham University'), professional certification, or license. Only include what the source explicitly states, never invent or infer a classification that isn't written down"],
  "seniority":   ["one of: Junior, Mid, Senior, Lead, Director, C-Suite"],
  "target_role": ["4-6 job titles this candidate should realistically target next. If the document explicitly states an ambition, include it, but do not stop there: weigh the FULL picture -- leadership/project experience, applied use of tools (not just a listed skill), academic background, languages, communications/organisational work -- as heavily as a formal skills-inventory section. Do not default to generic titles that only match keywords in a skills list if the candidate's strongest, most differentiated evidence points elsewhere (e.g. a small self-taught coding project is weaker evidence than a led, evidenced project with real outcomes). If the document states explicit sector, industry, or cause-targeting language (e.g. cover-letter angles, named industries/organisations the candidate is drawn to), also generate target_role titles reflecting those sectors specifically (e.g. 'Clean Energy Policy Assistant', 'Charity Communications Officer') -- do not let a generic skills-first framing crowd those out."],
  "sector_target": ["explicit sector, industry, mission, or cause-targeting language distinct from a job title -- e.g. named industries, causes, or organisation types the candidate has written about (cover-letter angles, 'I want to move into X'), captured even when no matching target_role title exists yet. Do not invent this from a skills list alone -- only include it if the source actually expresses a sector/mission preference"],
  "location":    ["city/region and/or work types like Remote, Hybrid, On-site"],
  "salary":      ["a single range like '70000-90000' only if clearly stated"],
  "custom":      ["any hard constraints stated, e.g. 'visa sponsorship required'"]
}}

Note: "past_role", "skill", and "experience" items are objects with "value" and an optional "proficiency" -- every other key is a plain list of strings.

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
