"""Generate the browsable weak-gate audit page from a --ground-truth gate_harness run.

Reads the snippet-mode report JSON (production reality: what screen_gate actually
had to read) plus jobs_seen, and renders one page with every disagreement between
the cheap gate and the expensive judge, example by example.

The classification of each wrongly-dropped role (_DROP_VERDICTS below) is a HAND
review, not something the harness computed -- it's recorded here so the page can
separate "the gate was wrong" from "the gate was starved" from "the judge's label
was wrong", which is the distinction the raw retention number hides.

    venv/Scripts/python tests/build_gate_audit_html.py
"""
import html
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAYLOAD = ROOT / "tests" / "gate_reports" / "_analysis_payload.json"
OUT = ROOT / "tests" / "gate_reports" / "weak_gate_audit.html"

# Hand review of each judge-approved role the gate dropped, keyed by (title, company).
# verdict: "error"      -- the gate contradicted its own rules or the candidate's profile
#          "defensible" -- a reasonable call on the text it was given
#          "label"      -- the gate was right; the judge's "strong" label is the wrong one
_DROP_VERDICTS = {
    ("Junior BI Analyst", "aidigital"): ("error",
        "The snippet never mentions remote, hybrid or on-site at all. The gate's own rule says an "
        "unlabelled listing is treated as on-site — which matches this candidate's On-site preference. "
        "It flagged a conflict anyway, against its own classification step."),
    ("Business Intelligence Analyst", "Transunion"): ("error",
        "“This is a hybrid position” vs a stated On-site preference. Hybrid includes on-site "
        "days; treating it as a conflict rules out most office-based analyst roles in the UK."),
    ("BI Analyst", "Erin Associates"): ("error",
        "Title line reads “BI Analyst – Grimsby / Hybrid”. Same hybrid-vs-On-site call."),
    ("Data Analyst", "SR2 | Socially Responsible Recruitment | Certified B Corporation™"): ("error",
        "“Bristol | Hybrid | £28,000 - £32,000”. Same hybrid-vs-On-site call, on a role the "
        "judge rated strong."),
    ("Graduate Data Analyst", "Experian"): ("error",
        "Dropped as seniority_low — i.e. the candidate is supposedly too senior. The candidate's stated "
        "seniority is Junior and “Graduate Data Analyst” is one of their own target roles. The "
        "model's cited signal was literally “Graduate Analyst (graduate/early-career bar)”."),
    ("Junior Data Analyst", "Accor"): ("error",
        "Same failure: cited signal “Junior Data Analyst (0–2 years / start of career)” used as "
        "evidence the role is beneath a Junior candidate."),
    ("Business Intelligence Analyst", "Advantage Finance Ltd"): ("error",
        "The cited signal argues the role implies MORE than junior — that is a seniority_high argument, "
        "returned under the seniority_low code. The direction confusion screen_v10 was bumped to fix, "
        "recurring."),
    ("Data Analyst Apprentice", "QA"): ("defensible",
        "Cited signal “Apprentice”. An apprenticeship genuinely does sit below a graduate with a "
        "First, so the gate's reasoning holds even though the judge liked the role."),
    ("Graduate Data Consulting Analyst Job in London", ""): ("defensible",
        "165 characters, ending in “…”, no company name — it really does read like a search-result "
        "fragment rather than a posting. The problem is what google_jobs supplies, not the judgement."),
    ("Graduate programmes", ""): ("defensible",
        "131 characters, no company, plural title. Indistinguishable from a careers-page blurb on this text."),
    ("Graduate Automation Engineer", "Talent Curve Recruitment"): ("label",
        "“industrial automation and systems integration… site-and-office-based”. This is the exact "
        "industrial-controls false positive screen_v11 was written to catch, and it caught it. The "
        "“strong” label here is the judge's error, not the gate's."),
}

_MISS_BUCKETS = {
    "no_reason": ("Judge recorded no reason", "unknown",
        "The stored verdict carries no disqualifier text at all, so there is nothing to check the gate "
        "against. These pre-date the reasoned-rejection change and are genuinely unknowable — they are "
        "neither evidence for nor against the gate."),
    "only_full": ("Only visible in the scraped page", "blind",
        "The requirement the judge rejected on appears nowhere in the text the gate was given — only in "
        "the full page, which is scraped AFTER the gate runs. The gate was not wrong here; it was blind. "
        "Passing these through to a stage that can read more is the correct behaviour."),
    "hint_quote": ("Quote came from a truncated hint", "blind",
        "The judge quoted a key_requirements hint that was itself truncated mid-phrase, so it matches no "
        "source text verbatim. Same situation as the group above in practice."),
    "visible": ("Visible to the gate and missed", "error",
        "The disqualifying clause was inside the text the gate read, and it passed the listing anyway. "
        "This is the only group that demonstrates a calibration miss."),
}

_SOURCES = [
    ("adzuna", 500, "no", "Description is truncated to a ~500-char teaser and there is no per-job detail "
                          "endpoint. Nothing to fetch at discovery."),
    ("reed", 453, "yes", "Teaser at discovery, but the per-job Reed endpoint returns the whole description "
                         "(~3,900 chars) for one plain HTTP call. Already wired in as pre-gate enrichment, "
                         "capped at REED_ENRICH_PRE_GATE_CAP=100 per run."),
    ("google_jobs", 152, "no", "Organic search snippets, often under 200 chars. No detail endpoint."),
    ("careerjet", 243, "no", "Aggregator teaser only."),
    ("jsearch", 4191, "already", "Returns the full description at discovery — the gate already reads its "
                                 "2,000-char ceiling."),
    ("ATS vendors", 3000, "already", "Greenhouse / Lever / Ashby / Workable / Recruitee / Personio all return "
                                     "the real description at discovery."),
    ("remotive", 7085, "already", "Full description at discovery."),
]

_FIXES = [
    ("Work arrangement: Hybrid no longer conflicts with anything", "high",
        "Fixed — recovered 4 of the 7 real losses.",
        "A hybrid role has on-site days, so it cannot conflict with an On-site preference; equally it "
        "part-satisfies a Remote one. Only fully-remote-vs-wants-on-site and fully-on-site-vs-wants-"
        "remote-only are genuine conflicts. This is the same superset logic location_scope already uses. "
        "Separately, the aidigital case shows the axis firing even when the listing says nothing about "
        "arrangement, which its own prompt says to treat as on-site — worth an explicit “if you did not "
        "classify the listing, you cannot fail this axis” line."),
    ("Seniority: an entry-level floor, relative to the candidate", "high",
        "Fixed — recovered the other 3 real losses.",
        "For a Junior candidate the gate flagged Graduate and Junior titles as beneath them. The axis needs "
        "a floor: when the candidate's own seniority is Junior/Graduate/Entry, a graduate/junior/entry "
        "listing is a MATCH and can never be seniority_low — only genuinely sub-entry work (unpaid intern, "
        "pre-degree apprenticeship) qualifies. The Advantage Finance case also shows the high/low direction "
        "inverting again, so the direction rule needs restating alongside it."),
    ("Real-listing check: robust to scraped page furniture", "high",
        "Fixed — 50 spurious hard drops in the full-text pass went to 0.",
        "In the full-text pass this axis went from 3 failures to 50, because crawl4ai keeps the board's "
        "chrome and postings open with “## <Title> jobs in <City> / Create email alert / back to last "
        "search” — verbatim the category-page pattern the axis is told to reject, and it is an "
        "unconditional hard drop. Exposure today is small (145 of 5,701 rows carry scraped text, ~18% with "
        "the pattern) but it grows every run, and it is the reason more text currently makes the gate "
        "worse rather than better. Strip the chrome at persist time, and/or teach the axis that a page "
        "carrying one real posting plus board navigation is a real posting."),
    ("Listings omitted from a gate response are now re-asked", "high",
        "Fixed — 17 of 113 silently unjudged went to 0.",
        "The model regularly returned valid JSON that simply skipped some of the listings it was given. "
        "Those fell through to an “everything ok” default AND were written to the gate cache as if they "
        "were real verdicts &mdash; so a listing no model had ever looked at was recorded as passing every "
        "axis, permanently, and would never be re-screened. The gate now re-asks for just the skipped "
        "listings (one bounded retry, a much shorter prompt), and if any are still unjudged they stay "
        "fail-open for the run but are no longer cached. It also sets an explicit flag, because an "
        "unjudged listing was otherwise byte-identical to one the model actively cleared."),
    ("Widen Reed pre-gate enrichment", "info",
        "Not applied. 13 of the 47 passes are Reed rows the gate read at 453 chars.",
        "The machinery already exists and costs one plain HTTP call per job with no LLM and no browser. "
        "Raising REED_ENRICH_PRE_GATE_CAP above 100 is the only text-supply lever available that does not "
        "need new scraping — but note it trades directly against time-to-first-card, since it is blocking "
        "main-thread work sitting in front of the first paint."),
    ("Accept that Adzuna cannot be fixed at discovery", "info",
        "26 of the 47 passes.",
        "No per-job endpoint exists. These rows will reach the gate at ~500 chars indefinitely, so the gate "
        "will keep passing Adzuna listings whose disqualifier is invisible to it. That is the correct "
        "outcome — the mid tier and the judge are the stages that can read more. It does mean the gate's "
        "measurable catch rate is capped well below 100% and should not be tuned toward it."),
]


def esc(s):
    return html.escape(str(s if s is not None else ""))


def load():
    rows = json.loads(PAYLOAD.read_text(encoding="utf-8"))
    drops, misses = [], []
    for o in rows:
        if o["verdict"] == "strong" and not o["kept"]:
            v, note = _DROP_VERDICTS.get((o["title"] or "", o["company"] or ""), ("defensible", ""))
            o["_verdict"], o["_note"] = v, note
            drops.append(o)
        elif o["verdict"] == "reject" and o["kept"]:
            vis = o["vis"]
            if vis is None:
                b = "no_reason"
            elif vis["in_gate_text"]:
                b = "visible"
            elif vis["in_full_text"]:
                b = "only_full"
            else:
                b = "hint_quote"
            o["_bucket"] = b
            misses.append(o)
    order = {"error": 0, "defensible": 1, "label": 2}
    drops.sort(key=lambda o: (order[o["_verdict"]], o["title"] or ""))
    border = {"visible": 0, "only_full": 1, "hint_quote": 2, "no_reason": 3}
    misses.sort(key=lambda o: (border[o["_bucket"]], -(o["snip_chars"] or 0)))
    return drops, misses


def failed_axes(o):
    return [a for a, v in (o["axes"] or {}).items() if v is False]


def drop_card(o):
    axes = ", ".join(a.replace("_ok", "") for a in failed_axes(o)) or "—"
    sig = o.get("sig")
    return f"""
<article class="ex ex--{esc(o['_verdict'])}" data-verdict="{esc(o['_verdict'])}">
  <header class="ex__head">
    <div class="ex__id">
      <h3>{esc(o['title'] or '(untitled)')}</h3>
      <p class="ex__co">{esc(o['company'] or 'no company named')}</p>
    </div>
    <div class="ex__tags">
      <span class="tag tag--{esc(o['_verdict'])}">{esc({'error':'gate error','defensible':'defensible','label':'gate right, label wrong'}[o['_verdict']])}</span>
      <span class="tag tag--axis">failed: {esc(axes)}</span>
      <span class="tag tag--src">{esc(o['source'].split(':')[0])} &middot; {o['snip_chars']:,} chars</span>
    </div>
  </header>
  {f'<p class="ex__sig"><span class="lbl">Model&rsquo;s cited signal</span>{esc(sig)}</p>' if sig else ''}
  <p class="ex__note">{esc(o['_note'])}</p>
  <details class="ex__more">
    <summary>The text the gate read</summary>
    <pre>{esc((o['snippet'] or '')[:2000])}</pre>
    {f'<p class="ex__link"><a href="{esc(o["url"])}" target="_blank" rel="noopener">Open the listing &rarr;</a></p>' if o.get('url') else ''}
  </details>
</article>"""


def miss_card(o):
    kind = _MISS_BUCKETS[o["_bucket"]][1]
    reason = o.get("judge_reason") or "No reason recorded with the verdict."
    return f"""
<article class="ex ex--{esc(kind)}" data-bucket="{esc(o['_bucket'])}">
  <header class="ex__head">
    <div class="ex__id">
      <h3>{esc(o['title'] or '(untitled)')}</h3>
      <p class="ex__co">{esc(o['company'] or 'no company named')}</p>
    </div>
    <div class="ex__tags">
      <span class="tag tag--src">{esc(o['source'].split(':')[0])} &middot; {o['snip_chars']:,} chars</span>
    </div>
  </header>
  <p class="ex__note"><span class="lbl">Why the judge rejected it</span>{esc(reason)}</p>
  <details class="ex__more">
    <summary>The text the gate read</summary>
    <pre>{esc((o['snippet'] or '')[:2000])}</pre>
    {f'<p class="ex__link"><a href="{esc(o["url"])}" target="_blank" rel="noopener">Open the listing &rarr;</a></p>' if o.get('url') else ''}
  </details>
</article>"""


def build(drops, misses):
    dv = Counter(o["_verdict"] for o in drops)
    mb = Counter(o["_bucket"] for o in misses)
    n_good = 26
    real_loss = dv["error"]

    drop_groups = ""
    for v, title, blurb in (
        ("error", "The gate got these wrong",
         "Seven roles the judge rated strong, dropped for reasons that contradict either the gate's own "
         "rules or the candidate's stated profile. This is the real cost, and it concentrates in exactly "
         "two axes."),
        ("defensible", "Reasonable calls on the text available",
         "Three drops that follow sensibly from what the gate could see. Two are google_jobs fragments "
         "under 170 characters; fixing those means fixing the source, not the prompt."),
        ("label", "The gate was right",
         "One role where the judge's &ldquo;strong&rdquo; label is the mistake. It counts against the gate "
         "in the raw number and should not."),
    ):
        items = [o for o in drops if o["_verdict"] == v]
        if not items:
            continue
        drop_groups += f"""
<section class="grp grp--{v}">
  <div class="grp__head"><h3>{title}<span class="grp__n">{len(items)}</span></h3><p>{blurb}</p></div>
  {''.join(drop_card(o) for o in items)}
</section>"""

    miss_groups = ""
    for key in ("visible", "only_full", "hint_quote", "no_reason"):
        items = [o for o in misses if o["_bucket"] == key]
        if not items:
            continue
        label, kind, blurb = _MISS_BUCKETS[key]
        miss_groups += f"""
<section class="grp grp--{kind}">
  <div class="grp__head"><h3>{label}<span class="grp__n">{len(items)}</span></h3><p>{blurb}</p></div>
  {''.join(miss_card(o) for o in items)}
</section>"""

    src_rows = "".join(
        f"""<tr class="src--{cls}"><td class="src__name">{esc(name)}</td><td class="num">{chars:,}</td>
        <td><span class="pill pill--{cls}">{ {'no':'not available','yes':'recoverable','already':'already full'}[cls] }</span></td>
        <td class="src__note">{esc(note)}</td></tr>"""
        for name, chars, cls, note in _SOURCES)

    fix_rows = "".join(
        f"""<article class="fix fix--{pri}">
  <header><h3>{esc(t)}</h3><span class="tag tag--{pri}">{ {'high':'applied','medium':'worth doing','info':'not applied'}[pri] }</span></header>
  <p class="fix__cost">{esc(cost)}</p>
  <p>{esc(body)}</p>
</article>""" for t, pri, cost, body in _FIXES)

    return f"""<title>Weak gate audit — screen_gate vs the final judge</title>
<style>
:root {{
  --bg:#FAFAFB; --surface:#FFFFFF; --surface-2:#F1F3F6; --ink:#14161B; --ink-2:#454B57;
  --muted:#6B7280; --line:#E2E5EB; --accent:#2E5FA3;
  --err:#A8502C; --err-bg:#FBF1EC; --ok:#3B7358; --ok-bg:#EDF5F0;
  --blind:#5F5A93; --blind-bg:#F1F0F9; --unknown:#6B7280; --unknown-bg:#F2F3F6;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#0E1014; --surface:#15181E; --surface-2:#1B1F27; --ink:#E9EBEF; --ink-2:#AEB4C0;
    --muted:#828A98; --line:#272C36; --accent:#7BA6E3;
    --err:#D98A63; --err-bg:#231913; --ok:#6FB292; --ok-bg:#12201A;
    --blind:#9E97D8; --blind-bg:#1A182A; --unknown:#8A919E; --unknown-bg:#1B1F27;
  }}
}}
:root[data-theme="dark"] {{
  --bg:#0E1014; --surface:#15181E; --surface-2:#1B1F27; --ink:#E9EBEF; --ink-2:#AEB4C0;
  --muted:#828A98; --line:#272C36; --accent:#7BA6E3;
  --err:#D98A63; --err-bg:#231913; --ok:#6FB292; --ok-bg:#12201A;
  --blind:#9E97D8; --blind-bg:#1A182A; --unknown:#8A919E; --unknown-bg:#1B1F27;
}}
:root[data-theme="light"] {{
  --bg:#FAFAFB; --surface:#FFFFFF; --surface-2:#F1F3F6; --ink:#14161B; --ink-2:#454B57;
  --muted:#6B7280; --line:#E2E5EB; --accent:#2E5FA3;
  --err:#A8502C; --err-bg:#FBF1EC; --ok:#3B7358; --ok-bg:#EDF5F0;
  --blind:#5F5A93; --blind-bg:#F1F0F9; --unknown:#6B7280; --unknown-bg:#F2F3F6;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:15px; line-height:1.6; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:980px; margin:0 auto; padding:48px 24px 96px; display:flex; flex-direction:column; gap:44px; }}
h1,h2,h3 {{ text-wrap:balance; margin:0; letter-spacing:-0.018em; }}
h1 {{ font-size:34px; line-height:1.15; font-weight:640; }}
h2 {{ font-size:22px; font-weight:620; }}
h3 {{ font-size:16px; font-weight:620; }}
p {{ margin:0; }}
.eyebrow {{ font-family:var(--mono); font-size:11px; letter-spacing:0.13em; text-transform:uppercase;
  color:var(--muted); }}
.lede {{ font-size:17px; color:var(--ink-2); max-width:66ch; }}
header.page {{ display:flex; flex-direction:column; gap:14px; border-bottom:1px solid var(--line); padding-bottom:32px; }}
.meta {{ font-family:var(--mono); font-size:12px; color:var(--muted); display:flex; gap:18px; flex-wrap:wrap; }}

/* Cell separators come from each cell's own ring, not from a background showing
   through the gaps -- with 5 cells in an auto-fit grid the trailing empty track
   would otherwise paint as a solid grey block. */
.band {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:1px;
  background:var(--surface); border-radius:3px; }}
.band > div {{ background:var(--surface); box-shadow:0 0 0 1px var(--line);
  padding:18px 20px; display:flex; flex-direction:column; gap:5px; }}
/* Sized to fit the widest label used ("42% → 88.5%") in a 190px minimum track
   without wrapping the arrow onto its own line. */
.band .n {{ font-family:var(--mono); font-size:23px; font-weight:600; letter-spacing:-0.03em;
  font-variant-numeric:tabular-nums; line-height:1.1; }}
code {{ font-family:var(--mono); font-size:0.9em; background:var(--surface-2);
  padding:1px 5px; border-radius:2px; }}
.band .k {{ font-family:var(--mono); font-size:10.5px; letter-spacing:0.11em; text-transform:uppercase; color:var(--muted); }}
.band .d {{ font-size:12.5px; color:var(--ink-2); }}
.n--err {{ color:var(--err); }} .n--ok {{ color:var(--ok); }} .n--blind {{ color:var(--blind); }}

section.block {{ display:flex; flex-direction:column; gap:20px; }}
.block__intro {{ display:flex; flex-direction:column; gap:9px; }}
.block__intro p {{ color:var(--ink-2); max-width:70ch; }}

.grp {{ display:flex; flex-direction:column; gap:10px; }}
.grp__head {{ display:flex; flex-direction:column; gap:5px; padding:14px 0 4px; border-top:2px solid var(--line); }}
.grp--error .grp__head {{ border-top-color:var(--err); }}
.grp--ok .grp__head, .grp--defensible .grp__head {{ border-top-color:var(--ok); }}
.grp--blind .grp__head {{ border-top-color:var(--blind); }}
.grp--unknown .grp__head {{ border-top-color:var(--unknown); }}
.grp--label .grp__head {{ border-top-color:var(--muted); }}
.grp__head h3 {{ display:flex; align-items:center; gap:10px; }}
.grp__n {{ font-family:var(--mono); font-size:12px; font-weight:600; color:var(--muted);
  background:var(--surface-2); border-radius:2px; padding:2px 7px; font-variant-numeric:tabular-nums; }}
.grp__head p {{ font-size:13.5px; color:var(--muted); max-width:72ch; }}

.ex {{ background:var(--surface); border:1px solid var(--line); border-left:3px solid var(--muted);
  border-radius:3px; padding:16px 18px; display:flex; flex-direction:column; gap:10px; }}
.ex--error {{ border-left-color:var(--err); }}
.ex--defensible, .ex--ok {{ border-left-color:var(--ok); }}
.ex--blind {{ border-left-color:var(--blind); }}
.ex--unknown {{ border-left-color:var(--unknown); }}
.ex--label {{ border-left-color:var(--muted); }}
.ex__head {{ display:flex; justify-content:space-between; gap:16px; align-items:flex-start; flex-wrap:wrap; }}
.ex__id {{ display:flex; flex-direction:column; gap:2px; min-width:0; }}
.ex__co {{ font-size:12.5px; color:var(--muted); }}
.ex__tags {{ display:flex; gap:6px; flex-wrap:wrap; align-items:center; }}
.tag {{ font-family:var(--mono); font-size:10.5px; letter-spacing:0.05em; text-transform:uppercase;
  padding:3px 8px; border-radius:2px; background:var(--surface-2); color:var(--ink-2); white-space:nowrap; }}
.tag--error {{ background:var(--err-bg); color:var(--err); }}
.tag--defensible {{ background:var(--ok-bg); color:var(--ok); }}
.tag--label {{ background:var(--unknown-bg); color:var(--unknown); }}
/* Applied fixes read as resolved, not as outstanding severity. */
.tag--high {{ background:var(--ok-bg); color:var(--ok); }}
.tag--medium {{ background:var(--blind-bg); color:var(--blind); }}
.tag--info {{ background:var(--unknown-bg); color:var(--unknown); }}
.fix--high {{ border-left:3px solid var(--ok); }}
.fix--info {{ border-left:3px solid var(--muted); }}
.lbl {{ display:block; font-family:var(--mono); font-size:10px; letter-spacing:0.11em;
  text-transform:uppercase; color:var(--muted); margin-bottom:3px; }}
.ex__sig {{ font-family:var(--mono); font-size:12.5px; background:var(--surface-2);
  border-radius:3px; padding:10px 12px; color:var(--ink-2); }}
.ex__note {{ font-size:14px; color:var(--ink-2); max-width:78ch; }}
.ex__more summary {{ cursor:pointer; font-family:var(--mono); font-size:11px; letter-spacing:0.07em;
  text-transform:uppercase; color:var(--accent); padding:3px 0; }}
.ex__more summary:focus-visible {{ outline:2px solid var(--accent); outline-offset:3px; border-radius:2px; }}
.ex__more pre {{ font-family:var(--mono); font-size:12px; line-height:1.62; white-space:pre-wrap;
  word-break:break-word; background:var(--surface-2); border-radius:3px; padding:13px 15px;
  margin:9px 0 0; color:var(--ink-2); max-height:340px; overflow:auto; }}
.ex__link {{ margin-top:8px; font-size:12.5px; }}
a {{ color:var(--accent); }}

.tbl-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:3px; }}
table {{ border-collapse:collapse; width:100%; min-width:660px; background:var(--surface); }}
th, td {{ text-align:left; padding:11px 14px; border-bottom:1px solid var(--line); vertical-align:top; font-size:13.5px; }}
th {{ font-family:var(--mono); font-size:10.5px; letter-spacing:0.1em; text-transform:uppercase;
  color:var(--muted); background:var(--surface-2); font-weight:500; }}
tr:last-child td {{ border-bottom:none; }}
.num {{ font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }}
.src__name {{ font-family:var(--mono); font-size:12.5px; }}
.src__note {{ color:var(--ink-2); font-size:12.5px; }}
.pill {{ font-family:var(--mono); font-size:10.5px; letter-spacing:0.05em; text-transform:uppercase;
  padding:3px 8px; border-radius:2px; white-space:nowrap; }}
.pill--no {{ background:var(--err-bg); color:var(--err); }}
.pill--yes {{ background:var(--blind-bg); color:var(--blind); }}
.pill--already {{ background:var(--ok-bg); color:var(--ok); }}

.fix {{ background:var(--surface); border:1px solid var(--line); border-radius:3px;
  padding:16px 18px; display:flex; flex-direction:column; gap:8px; }}
.fix header {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; flex-wrap:wrap; }}
.fix__cost {{ font-family:var(--mono); font-size:11.5px; color:var(--muted); }}
.fix p {{ font-size:14px; color:var(--ink-2); max-width:78ch; }}
.fixes {{ display:flex; flex-direction:column; gap:10px; }}

.caveat {{ background:var(--surface-2); border-radius:3px; padding:20px 22px;
  display:flex; flex-direction:column; gap:11px; }}
.caveat h2 {{ font-size:15px; }}
.caveat li {{ font-size:13.5px; color:var(--ink-2); margin-bottom:7px; max-width:76ch; }}
.caveat ul {{ margin:0; padding-left:20px; }}
@media (max-width:620px) {{ .wrap {{ padding:32px 16px 64px; }} h1 {{ font-size:27px; }} }}
</style>

<div class="wrap">
<header class="page">
  <p class="eyebrow">Diagnostic &middot; screen_gate (CHEAP tier)</p>
  <h1>What the weak gate got wrong, and what it never had a chance to see</h1>
  <p class="lede">Every disagreement between the cheap screening gate and the expensive final judge, on
  113 listings the judge has already ruled on &mdash; scored on the text the gate actually reads in
  production, not the scraped text it never gets.</p>
  <p class="meta"><span>profile 1 &middot; Data Analytics</span><span>113 labelled listings</span>
  <span>26 strong &middot; 87 reject</span><span>text mode: snippet</span></p>
</header>

<section class="block">
  <div class="block__intro">
    <h2>Result: fixed, and it cost nothing real</h2>
    <p>All four fixes are applied, and the same 113 listings were re-screened against them
    (<code>screen_v11 &rarr; screen_v12</code>). Retention went from 15 of 26 to 23 of 26. Nine of the
    eleven drops below now pass; one new drop appeared on the sector axis, which was not touched.</p>
    <p>The raw catch rate fell alongside it, 46% to 23%, and that is the number worth understanding
    rather than defending. All 23 rejects the old gate caught and the new one passes were dropped by
    the very axes that were broken &mdash; seniority 14, work arrangement 8, sector 4, real-listing 1
    &mdash; and <strong>not one of them was knowable from the text the gate had</strong>: 11 sat only in
    the scraped page, 3 nowhere, 9 carry no recorded reason. The old gate was not screening them out.
    It was dropping them for unrelated wrong reasons and happening to be right. The one listing whose
    disqualifier was genuinely visible and missed is still exactly one, before and after &mdash; no real
    screening power was lost.</p>
    <p>The full-text pass is the clearest evidence the third fix landed. It used to be
    <em>counterproductive</em>: giving the gate more text sent retention down to 42%, because scraped
    board chrome tripped the real-listing axis into 50 spurious hard drops. It now holds the same 88.5%
    retention as the teaser pass while catching nearly twice as many bad roles (44% vs 23%). More text
    finally makes the gate better instead of worse &mdash; which is also what would make widening Reed
    enrichment worth doing later.</p>
  </div>
  <div class="band">
    <div><span class="k">Good roles kept</span><span class="n n--ok">15 &rarr; 23</span>
      <span class="d">of 26 &mdash; 58% to 88.5%, on the teaser text production actually has</span></div>
    <div><span class="k">Real screening lost</span><span class="n n--ok">none</span>
      <span class="d">Disqualifiers visible and missed: 1 before, 1 after</span></div>
    <div><span class="k">Silently unjudged</span><span class="n n--ok">17 &rarr; 0</span>
      <span class="d">Listings skipped by the model, defaulted to pass, and cached that way</span></div>
    <div><span class="k">Full-text retention</span><span class="n n--ok">42% &rarr; 88.5%</span>
      <span class="d">More text no longer costs good roles &mdash; 50 spurious drops to 0</span></div>
  </div>
</section>

<section class="block">
  <div class="block__intro">
    <h2>The diagnosis this came from</h2>
    <p>Everything below is the pre-fix state &mdash; the evidence the changes were built on, kept so the
    reasoning stays checkable.</p>
    <h2>The headline number was worse than the reality, but not by enough</h2>
    <p>The raw result was 15 of 26 judge-approved roles kept &mdash; 58%. Reading all 11 drops
    individually, only 7 are actual gate failures: three are reasonable calls on the thin text supplied,
    and one is a role the gate was right to drop and the judge was wrong to approve. Adjusted, the gate
    loses 7 of 25 genuinely good roles, or 28%.</p>
    <p>The other direction is much healthier than the 46% catch rate suggests. Of the 47 rejected roles
    the gate let through, exactly one had its disqualifying requirement inside the text the gate read.
    The rest were invisible to it or carry no recorded reason at all &mdash; which is the correct
    behaviour, not a failure.</p>
  </div>
  <div class="band">
    <div><span class="k">Real losses</span><span class="n n--err">7</span>
      <span class="d">Good roles dropped for reasons that don&rsquo;t hold up &mdash; all in two axes</span></div>
    <div><span class="k">Defensible drops</span><span class="n n--ok">3</span>
      <span class="d">Sound calls on 131&ndash;453 characters of text</span></div>
    <div><span class="k">Demonstrable passes</span><span class="n n--err">1</span>
      <span class="d">Bad roles passed with the disqualifier in plain sight</span></div>
    <div><span class="k">Passed while blind</span><span class="n n--blind">18</span>
      <span class="d">Disqualifier existed only in text the gate never receives</span></div>
    <div><span class="k">Unknowable</span><span class="n">28</span>
      <span class="d">Judge stored no reason, so nothing to check against</span></div>
  </div>
</section>

<section class="block">
  <div class="block__intro">
    <h2>Good roles the gate rejected</h2>
    <p>All 11, each with the exact text the gate read and, where the model recorded one, the signal it
    said it was relying on. The seven real failures sit in just two axes &mdash; work arrangement and
    seniority &mdash; which is what makes this cheap to fix.</p>
  </div>
  {drop_groups}
</section>

<section class="block">
  <div class="block__intro">
    <h2>Bad roles the gate passed</h2>
    <p>Grouped by whether the gate could have known. Your instinct here was right: where the
    disqualifying requirement isn&rsquo;t in the text, passing the listing to a stage that can read more
    is the system working, not failing. Only the first group is a calibration problem.</p>
  </div>
  {miss_groups}
</section>

<section class="block">
  <div class="block__intro">
    <h2>What discovery can actually supply</h2>
    <p>Taking as given that the gate will never see scraped text: this is the ceiling on how much any
    prompt fix can achieve, per source. Median snippet length is measured across the live store of
    5,701 rows.</p>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Source</th><th>Median chars</th><th>Fuller text at discovery</th><th>Detail</th></tr></thead>
    <tbody>{src_rows}</tbody>
  </table></div>
  <p class="ex__note">The gate reads up to 2,000 characters. Every ATS vendor, jsearch and remotive
  already clear that or come close, so those listings are screened on real text today. Reed is the one
  teaser source with a recovery path. Adzuna, google_jobs and careerjet have none &mdash; and Adzuna
  alone accounts for 26 of the 47 passes.</p>
</section>

<section class="block">
  <div class="block__intro">
    <h2>The fixes</h2>
    <p>The first four are applied and verified against the same 113 listings; the last was deliberately
    left alone. The three prompt changes bumped the gate cache from <code>screen_v11</code> to
    <code>screen_v12</code>, so every cached listing is re-screened on the next run.</p>
  </div>
  <div class="fixes">{fix_rows}</div>
</section>

<section class="caveat">
  <h2>What this measurement can and cannot tell you</h2>
  <ul>
    <li><strong>The retention figure is an upper bound, not a recall estimate.</strong> Only listings that
    already passed the gate on some earlier run ever reached the judge and got a label. Roles the gate
    dropped and nothing judged cannot appear here at all, so 58% measures the gate&rsquo;s consistency
    with itself, and the true figure is lower.</li>
    <li><strong>The labels are the judge&rsquo;s opinion, not ground truth.</strong> The Automation
    Engineer case is a worked example: the judge called it strong, and it is the exact industrial-controls
    false positive the gate exists to catch.</li>
    <li><strong>60% of the passes are unscoreable.</strong> 28 of 47 rejected listings carry no stored
    reason, predating the reasoned-rejection change. They are excluded from the verdict rather than
    counted either way.</li>
    <li><strong>Some drift is expected.</strong> These listings were originally screened under
    screen_v9/v10 and judged under an earlier eval version. Part of the disagreement is prompt drift
    rather than error &mdash; though the seniority and arrangement failures are visible in the
    model&rsquo;s own cited reasoning, so those stand regardless.</li>
    <li><strong>The pre-fix run had a reliability problem of its own.</strong> 17 of the 113 listings were
    omitted from a gate response entirely and failed open &mdash; kept, and cached, without ever being
    judged. That inflated the pre-fix catch-rate denominator. It is now fixed and the re-run showed zero,
    but it means the pre-fix numbers below are slightly kinder to the old gate than they should be.</li>
  </ul>
</section>
</div>"""


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    drops, misses = load()
    OUT.write_text(build(drops, misses), encoding="utf-8")
    print(f"wrote {OUT}  ({len(drops)} drops, {len(misses)} passes)")


if __name__ == "__main__":
    main()
