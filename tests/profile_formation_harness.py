"""Read-only diagnostic harness for the profile-FORMATION stage, as redesigned
around services/formation.py: three MID-model calls that run in parallel in
production -- (1) structured extraction (parsing.extract_attributes),
(2a) families (profile_intel.generate_families: role families with their titles,
plus an intent draft) and (2b) summary (profile_intel.generate_summary: the
cv_summary + "looking for" header, skipped entirely when the CV is short enough
to be its own summary) -- followed by the CHEAP family-rename reconciliation.
Complements tests/gate_harness.py and tests/prompt_pipeline_harness.py, which
both start from an already-built profile snapshot -- this is the stage before that.

Runs entirely off a hardcoded sample CV string below -- no DB reads or writes, no
profile_id required, nothing persisted. Makes up to 4 live LLM calls: extraction,
families and summary (all MID_MODEL), and the rename-reconciliation call
(CHEAP_MODEL) -- the last one simulated here (a fake rename of the first family
from stage 2a) since there's no live DB profile for
reconcile_summary_after_family_change itself to run against, and skipped entirely
for a short CV whose cv_summary is the raw text (reconcile is a no-op there). The
MID calls are shown sequentially here only so the report can present each on
its own; formation.py runs them concurrently.

Usage:
    venv/Scripts/python tests/profile_formation_harness.py

Writes a self-contained HTML report -- the exact prompt text sent and the exact,
un-truncated raw JSON response received for each stage -- to
tests/profile_formation_reports/<timestamp>.html.
"""
import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Built from the actual profile excerpts reported during the role-family/header/
# location review: a first-year electrical engineering student seeking an
# internship (exercises target-role breadth + family clustering/labeling), with
# the relocation/remote-availability wording reported as under-extracting work
# types (exercises the parsing.py location fix), plus enough self-directed-project
# and skill-hedging detail to give profile_intel's header/brief something real to
# describe.
SAMPLE_CV = """Alex Morgan
Southend-on-Sea, Essex, UK

Motivated first-year BEng Electrical and Electronic Engineering student at the
University of Southampton (expected 2029). Seeking a summer internship or work
experience placement within electrical engineering, electronics, aerospace, or
manufacturing.

Based in Southend-on-Sea, Essex. Willing to relocate anywhere in the UK.
Available to start a remote role with no notice; in-person roles in the time
needed to relocate (London is commutable from Southend).

Relevant experience:
- Built a self-directed home automation project using Arduino and a custom PCB
  I designed in KiCad, wiring up sensors and writing the control firmware in C
  myself over a summer.
- Completed a first-year coursework project simulating a basic power
  distribution circuit in MATLAB/Simulink.
- Treasurer, University Robotics Society (unpaid, elected role) -- managed a
  GBP 2,000 termly budget and organised two open-day demonstration events for
  prospective students.

Skills:
- CAD (KiCad, basic SolidWorks) -- self-taught, used in the home automation
  project above
- C programming -- self-taught, used for the Arduino firmware
- MATLAB/Simulink -- coursework only, one module
- Python -- one online course, not yet used on a real project

A-Level Physics (A), Maths (A), Further Maths (B).

Not interested in pure software development roles with no hardware/electronics
component.
"""


def _render_html(sample_cv: str, stages: list[dict]) -> str:
    def esc(s) -> str:
        return html.escape(str(s))

    sections = [f"<h2>Sample CV / notes text fed in</h2><pre>{esc(sample_cv)}</pre>"]
    for s in stages:
        system_block = (
            f"<h3>System prompt</h3><pre>{esc(s['system'])}</pre>" if s["system"] else ""
        )
        sections.append(f"""
<section>
  <h2>{esc(s['title'])}</h2>
  <div class="meta">Model: {esc(s['model'])}</div>
  {system_block}
  <h3>Exact prompt sent</h3>
  <pre>{esc(s['prompt'])}</pre>
  <h3>Exact raw JSON response received (no truncation)</h3>
  <pre>{esc(json.dumps(s['response'], indent=2, ensure_ascii=False))}</pre>
</section>
""")
    body = "\n".join(sections)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profile-formation prompt/output audit</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", sans-serif; max-width: 980px; margin: 32px auto;
       padding: 0 16px; color: #1a1a1a; background: #fff; }}
h1 {{ font-size: 22px; }}
h2 {{ font-size: 17px; margin-top: 40px; border-bottom: 1px solid #ddd; padding-bottom: 6px; }}
h3 {{ font-size: 13px; color: #555; margin-bottom: 4px; }}
pre {{ background: #f6f6f6; border: 1px solid #e0e0e0; border-radius: 6px; padding: 12px;
      white-space: pre-wrap; word-wrap: break-word; font-size: 12.5px; line-height: 1.5; }}
.meta {{ font-size: 12px; color: #777; margin-bottom: 8px; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #1b1b1b; color: #e6e6e6; }}
  pre {{ background: #262626; border-color: #3a3a3a; color: #e6e6e6; }}
  h2 {{ border-color: #3a3a3a; }}
  h3 {{ color: #aaa; }}
  .meta {{ color: #999; }}
}}
</style></head>
<body>
<h1>Profile-formation prompt/output audit</h1>
<p>Generated {datetime.now(timezone.utc).isoformat()}. Everything below is exact and
un-truncated -- this is what parsing.py, profile_intel.py, and families.py actually sent to
and received from the model for the sample CV text above, run against the current prompts.</p>
{body}
</body></html>"""


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))

    from app.config import (
        CV_SHORT_WORD_THRESHOLD, CV_SUMMARY_MAX_CHARS, CV_SUMMARY_RAW_MAX_CHARS,
    )
    from app.services import families, parsing, profile_intel
    from app.services.profile_intel import clip_summary
    from app.services.llm import CHEAP_MODEL, MID_MODEL, llm_json

    # SAMPLE_CV currently sits under CV_SHORT_WORD_THRESHOLD, so this run
    # exercises the short-CV path (verbatim cv_summary, no AI-written header --
    # the summary call is never made at all). Make SAMPLE_CV longer than the
    # threshold to instead exercise the long-CV path (AI-compressed summary +
    # header). Mirrors formation._is_short_cv exactly.
    is_short = len(SAMPLE_CV.split()) < CV_SHORT_WORD_THRESHOLD

    # Stage 1 -- structured extraction (the DB-free parsing.extract_attributes call).
    print(f"[profile_formation] stage 1: structured extraction "
          f"(parsing._extract_prompt, {MID_MODEL})...")
    extract_prompt = parsing._extract_prompt(SAMPLE_CV)
    extract_response = llm_json(extract_prompt, system=parsing._EXTRACT_SYSTEM, model=MID_MODEL)
    if not extract_response:
        raise SystemExit("Extraction call failed or returned empty -- see the [llm_json] error above.")

    # Stage 2a -- families (+titles) and the intent draft. Runs IN PARALLEL with
    # stages 1 and 2b in production (services/formation.py); sequential here only
    # so each call gets its own report section.
    print(f"[profile_formation] stage 2a: families + intent draft ({MID_MODEL})...")
    families_prompt = profile_intel._families_prompt(SAMPLE_CV, "", True)
    families_response = llm_json(families_prompt, model=MID_MODEL)
    if not families_response:
        raise SystemExit("Families call failed or returned empty -- see the [llm_json] error above.")

    # Stage 2b -- the cv_summary + "Looking for" header. Skipped entirely for a
    # short CV, exactly as formation.run_formation_calls skips the call.
    if is_short:
        summary_prompt = ("(skipped -- short CV: cv_summary is the raw text verbatim, "
                          "so formation never makes this call)")
        summary_response: dict = {}
    else:
        print(f"[profile_formation] stage 2b: cv_summary + header ({MID_MODEL})...")
        summary_prompt = profile_intel._summary_prompt(SAMPLE_CV, "")
        summary_response = llm_json(summary_prompt, model=MID_MODEL)
        if not summary_response:
            raise SystemExit("Summary call failed or returned empty -- see the [llm_json] error above.")

    family_labels = [
        str(f.get("label")).strip() for f in (families_response.get("families") or [])
        if isinstance(f, dict) and str(f.get("label") or "").strip()
    ]
    # Mirrors formation.persist_formation / profile_intel.generate_summary, both of
    # which clip through profile_intel.clip_summary now -- a raw slice here would
    # report a mid-word cut this harness's caller no longer gets in production.
    cv_summary = (
        clip_summary(SAMPLE_CV, CV_SUMMARY_RAW_MAX_CHARS) if is_short
        else clip_summary(str(summary_response.get("cv_summary") or ""), CV_SUMMARY_MAX_CHARS)
    )

    # Stage 3 simulates the family-rename reconciliation (families.py's
    # reconcile_summary_after_family_change) -- no live DB profile here, so this
    # builds the identical prompt via families._reconcile_prompt directly, faking
    # a rename of the first family. Skipped for a short CV, whose cv_summary is
    # the raw text (reconcile is a no-op there -- see _summary_is_raw_cv).
    if family_labels and cv_summary and not is_short:
        old_name = family_labels[0]
        new_name = f"{old_name} (renamed)"
        remaining = family_labels[1:]
        print(f"[profile_formation] stage 3/3: family-rename reconciliation ({CHEAP_MODEL}) -- "
              f"simulating {old_name!r} -> {new_name!r}...")
        reconcile_prompt = families._reconcile_prompt(old_name, new_name, remaining, cv_summary)
        reconcile_response = llm_json(reconcile_prompt, model=CHEAP_MODEL)
    else:
        reconcile_prompt = (
            "(skipped -- short CV: cv_summary is the raw text, so reconcile is a no-op; "
            "or no family label to simulate a rename against)"
        )
        reconcile_response: dict = {}

    stages = [
        {
            "title": "Stage 1 -- Structured extraction (parsing.py::_extract_prompt)",
            "model": MID_MODEL,
            "system": parsing._EXTRACT_SYSTEM,
            "prompt": extract_prompt,
            "response": extract_response,
        },
        {
            "title": "Stage 2a -- Families + intent draft (profile_intel.py::_families_prompt)",
            "model": MID_MODEL,
            "system": "",
            "prompt": families_prompt,
            "response": families_response,
        },
        {
            "title": "Stage 2b -- cv_summary + header (profile_intel.py::_summary_prompt)",
            "model": MID_MODEL,
            "system": "",
            "prompt": summary_prompt,
            "response": summary_response,
        },
        {
            "title": "Stage 3 -- Family-rename reconciliation, simulated "
                     "(families.py::_reconcile_prompt)",
            "model": CHEAP_MODEL,
            "system": "",
            "prompt": reconcile_prompt,
            "response": reconcile_response,
        },
    ]

    out_dir = ROOT / "tests" / "profile_formation_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = out_dir / f"{stamp}.html"
    out_path.write_text(_render_html(SAMPLE_CV, stages), encoding="utf-8")

    print(f"\n[profile_formation] families: {family_labels}")
    print(f"[profile_formation] cv_summary: {cv_summary!r}")
    print(f"[profile_formation] header: {summary_response.get('header')!r}")
    print(f"[profile_formation] wrote {out_path}")


if __name__ == "__main__":
    main()
