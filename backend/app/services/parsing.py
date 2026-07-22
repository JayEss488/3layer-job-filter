"""CV / free-text extraction into normalised profile_attributes.

Deliberately narrow now: this call extracts only the STRUCTURED, engine-load-
bearing facts the pipeline needs as their own rows -- seniority, sector
interests, location/work-type, salary, and the candidate's own hard filters.
Skills, qualifications, and past roles (paid or otherwise) are NOT extracted
here anymore: they were noise as chips (the profile shifted from "match on paper"
to "the candidate wants this"), and everything the judge/gates need from them now
rides in the cv_summary the formation "understand" call writes instead (see
services/formation.py + profile_intel.generate_understanding) -- its PAID work
experience section (_SUMMARY_TASK) is what covers past-role detail now. The types
still exist in config.ATTRIBUTE_TYPES and can be filled in by hand -- this just
stops auto-populating them.

The extraction is split into a pure `extract_attributes(text)` LLM call and a
`persist_attributes(...)` DB step so formation can run it IN PARALLEL with the
understand call (two threads, one shared session is unsafe -- so the LLM work is
DB-free and the writes happen back on the request thread once both return)."""
import io

from sqlalchemy.orm import Session

from ..config import ATTRIBUTE_TYPES
from ..models import ProfileAttribute
from .llm import MID_MODEL, llm_json


class CVParseFailed(Exception):
    """Raised when the LLM extraction call itself failed (network/API/schema error),
    as opposed to a call that succeeded but genuinely found nothing -- callers should
    surface this to the user rather than let it look like an empty CV."""


_EXTRACT_SYSTEM = (
    "You extract the structured, factual preferences and hard constraints from a "
    "candidate's CV or notes for a job-search profile. Be faithful to the source: "
    "do NOT inflate, exaggerate, or invent claims. Never extract a skill, "
    "qualification, or past role here -- those fields are handled elsewhere."
)


# Extraction schema. Deliberately excludes skill/qualification/past_role (no
# longer auto-filled -- past-role detail now lives in the cv_summary's PAID work
# experience section, see profile_intel._SUMMARY_TASK) and target_role
# (profile_intel/formation generates that separately from the full picture, and
# supersedes any guess this call would make). No cv_summary either -- the
# formation "understand" call writes that in the same pass as the header, so the
# two don't overlap (see services/formation.py).
def _extract_prompt(text: str) -> str:
    return f"""Extract a job-search profile from the document below.
Return ONLY a JSON object with these keys (omit a key if nothing applies; never invent):
{{
  "seniority":   ["one of: Junior, Mid, Senior, Lead, Director, C-Suite"],
  "sector_target": ["explicit sector, industry, mission, or cause-targeting language distinct from a job title -- e.g. named industries, causes, or organisation types the candidate has written about (cover-letter angles, 'I want to move into X'), captured even when no matching target_role title exists yet. Do not invent this from a skills list alone -- only include it if the source actually expresses a sector/mission preference"],
  "location":    ["one item for the candidate's city/region if stated, PLUS a separate item for EVERY work-type (Remote, Hybrid, On-site) they would accept -- do not collapse multiple accepted work types into a single item, and do not keep only whichever one sounds most immediate. Infer On-site/Hybrid acceptance from stated relocation willingness (e.g. 'willing to relocate anywhere in the UK') or any stated willingness/availability to work in person, even when it's phrased as conditional on notice period or relocation timing -- a candidate who can 'start remote immediately, in-person once relocated' wants BOTH Remote and On-site (or Hybrid) listed, not just the immediate-sounding one. For example, 'Based in Southend-on-Sea. Willing to relocate anywhere in the UK. Available to start a remote role with no notice; in-person roles in the time needed to relocate' should extract as multiple items: 'Southend-on-Sea', 'Remote', and 'On-site' (or 'Hybrid')"],
  "salary":      ["a single range like '70000-90000'. Use the figure the candidate explicitly states if there is one. If none is stated but their seniority level (see above) and role/sector/location are clear enough to judge, give a plausible rough market-rate range for that combination instead of leaving this empty -- salary and seniority band correlate strongly, so use that inference rather than omitting the field. Keep an inferred range broad, not a narrow point-estimate (e.g. for a Junior role, '22000-40000' reflects the real spread better than a narrow '28000-38000'). Only omit this key entirely if there truly isn't enough in the document to judge even roughly"],
  "custom":      ["any hard constraints stated, e.g. 'visa sponsorship required'"],
  "must_have":   ["a non-negotiable the candidate explicitly states they REQUIRE in a role, short and concrete, e.g. 'visa sponsorship', 'fully remote', 'salary above 40k'. Only include what the source actually states as a hard requirement -- never infer one from a skills list or a nice-to-have"],
  "avoid":       ["something the candidate explicitly states they do NOT want and would reject a role over, short and concrete, e.g. 'no cold-calling', 'no night shifts', 'no commission-only pay'. Only include explicit refusals the source states -- never invent one"]
}}

Every key is a plain list of strings. Do NOT extract skills, qualifications, past roles, or target roles here -- those are handled separately.

Document:
{text[:40000]}"""


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


def extract_attributes(text: str) -> dict:
    """Pure LLM extraction of structured attributes from CV/notes text -- no DB.
    Returns the raw parsed dict (keyed by attribute type), or {} on a call
    failure. Kept DB-free so formation can run it in a thread alongside the
    understand call (see services/formation.py)."""
    if not text or not text.strip():
        return {}
    return llm_json(_extract_prompt(text), system=_EXTRACT_SYSTEM, model=MID_MODEL)


def persist_attributes(
    db: Session, profile_id: int, data: dict, source: str
) -> list[ProfileAttribute]:
    """Insert one unconfirmed attribute row per extracted item. Skips any type
    not present in `data` and dedupes case-insensitively within a type. Does not
    commit -- the caller owns the transaction (formation batches this with the
    understand-call writes into one commit)."""
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
            # Robust to either a plain string or a legacy {"value": ...} object.
            value = str(item.get("value", "") if isinstance(item, dict) else item).strip()
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
            )
            db.add(attr)
            created.append(attr)
    db.flush()  # populate ids for the response
    return created
