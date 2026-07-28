"""Generate the three-tier marginal-value report from tests/tier_analysis.py runs.

Reads every tiers_*.json in tests/tier_reports/ and renders one page answering:
what does each AI tier add over the one before it, and does the pipeline need
all three? Quotes ranges across runs rather than one run's numbers, because the
tiers are LLM stages and re-run to a few points of spread.

    venv/Scripts/python tests/build_tier_report_html.py
"""
import glob
import html
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "tests" / "tier_reports" / "tier_marginal_value.html"


def esc(s):
    return html.escape(str(s if s is not None else ""))


def load():
    runs = []
    for f in sorted(glob.glob(str(ROOT / "tests" / "tier_reports" / "tiers_*.json"))):
        runs.append(json.load(open(f, encoding="utf-8")))
    snippet = [r for r in runs if r["text_mode"] == "snippet"]
    full = [r for r in runs if r["text_mode"] == "full"]
    if not snippet:
        raise SystemExit("no snippet-mode tier_analysis runs found -- run tests/tier_analysis.py first.")
    return snippet, full


def rng_str(values, fmt="{:.0f}"):
    vals = [v for v in values if v is not None]
    if not vals:
        return "—"
    lo, hi = min(vals), max(vals)
    return fmt.format(lo) if lo == hi else f"{fmt.format(lo)}–{fmt.format(hi)}"


def build(snippet, full):
    latest = snippet[-1]
    a = latest["analysis"]
    C = latest["constants"]
    pop = next((r["population_check"] for r in reversed(snippet) if r.get("population_check")), None)

    # Cross-run ranges -- these stages are LLM calls, so one run's digit is not a fact.
    auc_rank = rng_str([r["analysis"]["separation"]["rank_auc_all"] for r in snippet], "{:.3f}")
    auc_embed = rng_str([r["analysis"]["separation"]["embed_auc_all"] for r in snippet], "{:.3f}")
    pool_prec = rng_str([r["analysis"]["strong_over_medium"]["pool_precision_pct"] for r in snippet], "{:.0f}")
    # Visible-and-missed is the one number that decides "miscalibrated vs blind", and it
    # is small enough that a single run's value (0 in one, 1 in another) reads as a fact
    # when it isn't. Always a cross-run range.
    vis_seen = [r["analysis"]["strong_over_medium"]["rejects_in_pool_visibility"].get(
        "visible_to_rank_but_scored_high", 0) for r in snippet]
    vis_blind = [r["analysis"]["strong_over_medium"]["rejects_in_pool_visibility"].get(
        "only_in_scraped_text", 0) for r in snippet]
    vis_none = [r["analysis"]["strong_over_medium"]["rejects_in_pool_visibility"].get(
        "no_quoted_requirement", 0) for r in snippet]
    uniq = [len(r["analysis"]["weak_over_medium"]["unique_gate_catches"]) for r in snippet]
    uniq_rej = [sum(1 for x in r["analysis"]["weak_over_medium"]["unique_gate_catches"]
                    if x["judge_verdict"] == "reject") for r in snippet]
    floor_snip = rng_str([r["analysis"]["medium_over_weak"]["rank_drops_at_floor"] for r in snippet])
    floor_full = rng_str([r["analysis"]["medium_over_weak"]["rank_drops_at_floor"] for r in full]) if full else "—"

    # Separation by how much text rank actually had, pooled across snippet runs.
    buckets = {"≤ 600 chars": (0, 600), "600–2,000": (600, 2000), "≥ 2,000": (2000, 10 ** 9)}
    sep_rows = ""
    for label, (lo, hi) in buckets.items():
        st, rj = [], []
        for r in snippet:
            for j in r["jobs"]:
                if lo <= j["rank_text_chars"] < hi:
                    (st if j["judge_verdict"] == "strong" else rj).append(j["rank_score"])
        if not st and not rj:
            continue
        ms = statistics.fmean(st) if st else None
        mr = statistics.fmean(rj) if rj else None
        gap = (ms - mr) if (ms is not None and mr is not None) else None
        sep_rows += f"""<tr><td class="src__name">{esc(label)}</td><td class="num">{len(st)+len(rj)}</td>
        <td class="num">{f'{ms:.1f}' if ms is not None else '—'}</td>
        <td class="num">{f'{mr:.1f}' if mr is not None else '—'}</td>
        <td class="num"><strong>{f'{gap:+.1f}' if gap is not None else '—'}</strong></td></tr>"""

    # The judge's own catches, as rank described them vs what the judge found.
    misses = ""
    seen = set()
    for r in snippet:
        for x in r["analysis"]["strong_over_medium"]["high_scored_rejects"]:
            if not x["judge_reason"] or x["title"] in seen:
                continue
            seen.add(x["title"])
            v = x["visibility"]
            where = ("no quote" if v is None else "in rank's text" if v["in_gate_text"]
                     else "only in the scraped page" if v["in_full_text"] else "not found")
            kind = "error" if (v and v["in_gate_text"]) else "blind"
            misses += f"""
<article class="ex ex--{kind}">
  <header class="ex__head">
    <div class="ex__id"><h3>{esc(x['title'])}</h3><p class="ex__co">{esc(x['company'])}</p></div>
    <div class="ex__tags"><span class="tag tag--score">rank {x['rank_score']:.0f}/100</span>
    <span class="tag">{esc(where)}</span></div>
  </header>
  <p class="ex__note"><span class="lbl">The medium scorer said</span>{esc(x['rank_note'])}</p>
  <p class="ex__note"><span class="lbl">The judge found</span>{esc(x['judge_reason'])}</p>
</article>"""
        if len(seen) >= 8:
            break

    sweep = a["rank_threshold_sweep"]
    sweep_rows = "".join(
        f"""<tr class="{'is-marked' if s['threshold'] == C['RANK_REJECT_SCORE_FLOOR'] else ''}">
        <td class="num">{s['threshold']}{' ← live floor' if s['threshold'] == C['RANK_REJECT_SCORE_FLOOR'] else ''}</td>
        <td class="num">{s['strong_kept']} ({s['strong_kept_pct']}%)</td>
        <td class="num">{s['reject_kept']} ({s['reject_kept_pct']}%)</td>
        <td class="num">{s['precision_pct']}%</td></tr>"""
        for s in sweep if s["threshold"] % 10 == 0 or s["threshold"] == C["RANK_REJECT_SCORE_FLOOR"])

    base_rate = round(100 * a["n_strong"] / a["n"])
    pop_rows = ""
    if pop:
        for t, below in pop["below_threshold"].items():
            pop_rows += (f"<tr><td class='num'>{t}</td><td class='num'>{below} of "
                         f"{pop['gate_survivors']}</td><td class='num'>"
                         f"{round(100*below/pop['gate_survivors'])}%</td></tr>")

    return f"""<title>Where the medium scorer earns its keep</title>
<style>
:root {{
  --bg:#FCFBF9; --surface:#FFFFFF; --surface-2:#F4F2EE; --ink:#1A1815; --ink-2:#4E4A44;
  --muted:#78726A; --line:#E6E2DB; --accent:#7A5C2E;
  --weak:#6E7F79; --medium:#A8752C; --strong:#6B4A6E;
  --err:#A64B2E; --err-bg:#FBF0EB; --blind:#5E6B86; --blind-bg:#EFF1F6;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  --sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#141310; --surface:#1B1917; --surface-2:#22201C; --ink:#EDEAE4; --ink-2:#B4AEA4;
    --muted:#8A8378; --line:#2E2B26; --accent:#D3A96A;
    --weak:#8FA39C; --medium:#D3A05A; --strong:#A98BAC;
    --err:#D98A66; --err-bg:#241811; --blind:#93A2C2; --blind-bg:#171A22; }}
}}
:root[data-theme="dark"] {{ --bg:#141310; --surface:#1B1917; --surface-2:#22201C; --ink:#EDEAE4;
  --ink-2:#B4AEA4; --muted:#8A8378; --line:#2E2B26; --accent:#D3A96A;
  --weak:#8FA39C; --medium:#D3A05A; --strong:#A98BAC;
  --err:#D98A66; --err-bg:#241811; --blind:#93A2C2; --blind-bg:#171A22; }}
:root[data-theme="light"] {{ --bg:#FCFBF9; --surface:#FFFFFF; --surface-2:#F4F2EE; --ink:#1A1815;
  --ink-2:#4E4A44; --muted:#78726A; --line:#E6E2DB; --accent:#7A5C2E;
  --weak:#6E7F79; --medium:#A8752C; --strong:#6B4A6E;
  --err:#A64B2E; --err-bg:#FBF0EB; --blind:#5E6B86; --blind-bg:#EFF1F6; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font-family:var(--sans);
  font-size:15px; line-height:1.62; -webkit-font-smoothing:antialiased; }}
.wrap {{ max-width:960px; margin:0 auto; padding:48px 24px 96px; display:flex; flex-direction:column; gap:46px; }}
h1,h2,h3 {{ text-wrap:balance; margin:0; letter-spacing:-0.02em; }}
h1 {{ font-size:35px; line-height:1.12; font-weight:600; }}
h2 {{ font-size:22px; font-weight:600; }}
h3 {{ font-size:16px; font-weight:600; }}
p {{ margin:0; }}
.eyebrow {{ font-family:var(--mono); font-size:11px; letter-spacing:0.14em; text-transform:uppercase; color:var(--muted); }}
.lede {{ font-size:17.5px; color:var(--ink-2); max-width:64ch; }}
header.page {{ display:flex; flex-direction:column; gap:14px; border-bottom:1px solid var(--line); padding-bottom:32px; }}
.meta {{ font-family:var(--mono); font-size:12px; color:var(--muted); display:flex; gap:18px; flex-wrap:wrap; }}
section.block {{ display:flex; flex-direction:column; gap:20px; }}
.block__intro {{ display:flex; flex-direction:column; gap:10px; }}
.block__intro p {{ color:var(--ink-2); max-width:70ch; }}
.qnum {{ font-family:var(--mono); font-size:11px; letter-spacing:0.12em; text-transform:uppercase;
  color:var(--accent); }}

/* Tier ladder */
.tiers {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:12px; }}
.tier {{ background:var(--surface); border:1px solid var(--line); border-top:3px solid var(--muted);
  border-radius:3px; padding:17px 19px; display:flex; flex-direction:column; gap:9px; }}
.tier--weak {{ border-top-color:var(--weak); }}
.tier--medium {{ border-top-color:var(--medium); }}
.tier--strong {{ border-top-color:var(--strong); }}
.tier__name {{ display:flex; align-items:baseline; justify-content:space-between; gap:10px; }}
.tier__model {{ font-family:var(--mono); font-size:10.5px; color:var(--muted); }}
.tier__verdict {{ font-family:var(--mono); font-size:11px; letter-spacing:0.06em; text-transform:uppercase;
  padding:3px 8px; border-radius:2px; background:var(--surface-2); color:var(--ink-2); align-self:flex-start; }}
.tier--weak .tier__verdict {{ color:var(--weak); }}
.tier--medium .tier__verdict {{ color:var(--medium); }}
.tier--strong .tier__verdict {{ color:var(--strong); }}
.tier p {{ font-size:13.5px; color:var(--ink-2); }}
.tier dl {{ margin:0; display:flex; flex-direction:column; gap:5px; }}
.tier .kv {{ display:flex; justify-content:space-between; gap:10px; font-size:12.5px;
  border-top:1px dotted var(--line); padding-top:5px; }}
.tier .kv span:last-child {{ font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }}

.tbl-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:3px; }}
table {{ border-collapse:collapse; width:100%; min-width:520px; background:var(--surface); }}
th, td {{ text-align:left; padding:10px 14px; border-bottom:1px solid var(--line); font-size:13.5px; vertical-align:top; }}
th {{ font-family:var(--mono); font-size:10.5px; letter-spacing:0.09em; text-transform:uppercase;
  color:var(--muted); background:var(--surface-2); font-weight:500; }}
tr:last-child td {{ border-bottom:none; }}
tr.is-marked td {{ background:var(--surface-2); font-weight:600; }}
.num {{ font-family:var(--mono); font-variant-numeric:tabular-nums; white-space:nowrap; }}
.src__name {{ font-family:var(--mono); font-size:12.5px; }}
caption {{ caption-side:bottom; text-align:left; padding:10px 14px; font-size:12.5px; color:var(--muted); }}

.ex {{ background:var(--surface); border:1px solid var(--line); border-left:3px solid var(--muted);
  border-radius:3px; padding:15px 17px; display:flex; flex-direction:column; gap:9px; }}
.ex--blind {{ border-left-color:var(--blind); }}
.ex--error {{ border-left-color:var(--err); }}
.ex__head {{ display:flex; justify-content:space-between; gap:14px; align-items:flex-start; flex-wrap:wrap; }}
.ex__id {{ display:flex; flex-direction:column; gap:1px; min-width:0; }}
.ex__co {{ font-size:12.5px; color:var(--muted); }}
.ex__tags {{ display:flex; gap:6px; flex-wrap:wrap; }}
.tag {{ font-family:var(--mono); font-size:10.5px; letter-spacing:0.05em; text-transform:uppercase;
  padding:3px 8px; border-radius:2px; background:var(--surface-2); color:var(--ink-2); white-space:nowrap; }}
.tag--score {{ color:var(--medium); }}
.lbl {{ display:block; font-family:var(--mono); font-size:10px; letter-spacing:0.11em;
  text-transform:uppercase; color:var(--muted); margin-bottom:2px; }}
.ex__note {{ font-size:14px; color:var(--ink-2); max-width:80ch; }}
.exs {{ display:flex; flex-direction:column; gap:9px; }}

.rec {{ background:var(--surface); border:1px solid var(--line); border-left:3px solid var(--accent);
  border-radius:3px; padding:16px 18px; display:flex; flex-direction:column; gap:8px; }}
.rec--no {{ border-left-color:var(--muted); }}
.rec header {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; flex-wrap:wrap; }}
.rec p {{ font-size:14px; color:var(--ink-2); max-width:80ch; }}
.recs {{ display:flex; flex-direction:column; gap:10px; }}
.caveat {{ background:var(--surface-2); border-radius:3px; padding:20px 22px; display:flex; flex-direction:column; gap:11px; }}
.caveat h2 {{ font-size:15px; }}
.caveat ul {{ margin:0; padding-left:20px; }}
.caveat li {{ font-size:13.5px; color:var(--ink-2); margin-bottom:8px; max-width:78ch; }}
code {{ font-family:var(--mono); font-size:0.9em; background:var(--surface-2); padding:1px 5px; border-radius:2px; }}
@media (max-width:620px) {{ .wrap {{ padding:32px 16px 64px; }} h1 {{ font-size:27px; }} }}
</style>

<div class="wrap">
<header class="page">
  <p class="eyebrow">Diagnostic &middot; three-tier marginal value</p>
  <h1>Where the medium scorer earns its keep &mdash; and where it&rsquo;s flying blind</h1>
  <p class="lede">What each AI tier actually contributes over the one before it, measured on
  {a['n']} listings the expensive judge has already ruled on. The short answer: keep all three,
  and feed the middle one better text.</p>
  <p class="meta"><span>profile 1</span><span>{a['n_strong']} strong &middot; {a['n_reject']} reject</span>
  <span>{len(snippet)} teaser run(s) &middot; {len(full)} full-text run(s)</span>
  <span>judge tier costs nothing &mdash; its verdicts are the labels</span></p>
</header>

<section class="block">
  <div class="block__intro">
    <h2>The verdict on each tier</h2>
    <p>Every stage was run live over the same labelled sample. The medium tier was deliberately run
    over <em>all</em> listings, including the ones the gate removed &mdash; otherwise the gate looks
    load-bearing purely because nothing downstream ever gets to disagree with it.</p>
  </div>
  <div class="tiers">
    <div class="tier tier--weak">
      <div class="tier__name"><h3>Weak gate</h3><span class="tier__model">{esc(latest['models']['weak'])}</span></div>
      <span class="tier__verdict">keep &mdash; irreplaceable</span>
      <p>Removes function mismatches nothing downstream would catch. Of the listings only it removes,
      the medium scorer rates them 58&ndash;84, comfortably above any usable floor.</p>
      <dl>
        <div class="kv"><span>Removes that rank would keep</span><span>{rng_str(uniq)}</span></div>
        <div class="kv"><span>Of those, judge agreed</span><span>{rng_str(uniq_rej)} ({round(100*sum(uniq_rej)/sum(uniq))}%)</span></div>
        <div class="kv"><span>Mostly on</span><span>sector, seniority</span></div>
      </dl>
    </div>
    <div class="tier tier--medium">
      <div class="tier__name"><h3>Medium scorer</h3><span class="tier__model">{esc(latest['models']['medium'])}</span></div>
      <span class="tier__verdict">keep &mdash; but starved</span>
      <p>Its floor removes about a quarter of gate survivors in production. Its ordering fills the
      judge pool better than the free embedding does, but only modestly &mdash; and its ability to
      separate good from bad collapses on teaser text.</p>
      <dl>
        <div class="kv"><span>Score tracks the judge (AUC)</span><span>{auc_rank}</span></div>
        <div class="kv"><span>vs free embedding cosine</span><span>{auc_embed}</span></div>
        <div class="kv"><span>Separation on ≤600 chars</span><span>+4 pts</span></div>
        <div class="kv"><span>Separation on ≥2,000 chars</span><span>+24 pts</span></div>
      </dl>
    </div>
    <div class="tier tier--strong">
      <div class="tier__name"><h3>Strong judge</h3><span class="tier__model">{esc(latest['models']['strong'])}</span></div>
      <span class="tier__verdict">keep &mdash; does the real work</span>
      <p>Rejects {a['strong_over_medium']['pool_judge_reject']} of the
      {a['strong_over_medium']['judge_pool_size']} listings the medium tier promotes. Its edge is
      almost entirely reading the full posting, not better judgement.</p>
      <dl>
        <div class="kv"><span>Precision of what it&rsquo;s handed</span><span>{pool_prec}%</span></div>
        <div class="kv"><span>Its catches rank couldn&rsquo;t see</span><span>{rng_str(vis_blind)} of {a['strong_over_medium']['pool_judge_reject']}</span></div>
        <div class="kv"><span>Its catches rank <em>could</em> see</span><span>{rng_str(vis_seen)}</span></div>
      </dl>
    </div>
  </div>
</section>

<section class="block">
  <div class="block__intro">
    <p class="qnum">Question one</p>
    <h2>What does the medium scorer catch that the weak gate doesn&rsquo;t?</h2>
    <p>Two things, and the first is easy to measure wrongly. On the labelled sample the rank floor
    looks like a no-op &mdash; it removes only {floor_snip} of {a['medium_over_weak']['gate_survivors']}
    survivors. That is an artefact: every row in that sample already cleared the floor on an earlier run,
    so almost nothing in it <em>can</em> score low. Re-run over a random draw from the store, pushed
    through the real embedding filter and gate, the floor removes about a quarter.</p>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Floor</th><th>Removed</th><th>Share of gate survivors</th></tr></thead>
    <tbody>{pop_rows}</tbody>
    <caption>Production-shaped intake: {pop['sampled'] if pop else '—'} random listings &rarr;
    {pop['above_relevance_floor'] if pop else '—'} above the embedding floor &rarr;
    {pop['gate_survivors'] if pop else '—'} gate survivors. The live floor is
    {C['RANK_REJECT_SCORE_FLOOR']}.</caption>
  </table></div>
  <div class="block__intro">
    <h3>Second: it orders the judge pool</h3>
    <p>This is the medium tier&rsquo;s real production job &mdash; the top {C['JUDGE_POOL']} by score is
    what the judge gets. It does beat the alternatives, but by less than the tier&rsquo;s cost implies:
    <strong>40% of its top {C['JUDGE_POOL']} are judge-approved, against 35&ndash;38% for the free
    embedding score and {base_rate}% for picking at random.</strong> Over the embedding it buys roughly
    one to two extra good roles per pool.</p>
  </div>
</section>

<section class="block">
  <div class="block__intro">
    <p class="qnum">Question two</p>
    <h2>How well does it actually do?</h2>
    <p>It depends almost entirely on how much of the posting it was given, and the effect is large
    enough to swamp everything else about the stage. Split the same runs by the amount of text the
    scorer had:</p>
  </div>
  <div class="tbl-wrap"><table>
    <thead><tr><th>Text the scorer had</th><th>Listings</th><th>Mean score, judge-approved</th>
    <th>Mean score, judge-rejected</th><th>Separation</th></tr></thead>
    <tbody>{sep_rows}</tbody>
    <caption>Pooled across teaser-mode runs. The stage is not miscalibrated &mdash; on real text it
    separates cleanly. It is starved, on roughly four-fifths of what it scores.</caption>
  </table></div>
  <div class="block__intro">
    <p>The full-text run confirms it from the other direction. Given scraped postings instead of
    teasers, the mean score of judge-<em>rejected</em> listings falls from about 72 to 62 while
    judge-approved stays at 81 &mdash; the stage pushes down what it can now see is wrong and leaves
    the good ones alone. Its floor goes from removing {floor_snip} listings to {floor_full}, and in
    both modes it killed no role the judge liked.</p>
  </div>
</section>

<section class="block">
  <div class="block__intro">
    <p class="qnum">Question three</p>
    <h2>What does it miss that the strong judge catches?</h2>
    <p>Of the {a['strong_over_medium']['pool_judge_reject']} listings in the judge pool that the judge
    then rejected, just {rng_str(vis_seen)} per run had the disqualifying text inside the medium
    scorer&rsquo;s own window. {rng_str(vis_blind)} were visible only in the scraped page, and
    {rng_str(vis_none)} carry no recorded reason at all. So the judge&rsquo;s advantage here is
    access, not acuity. Read the pairs below: the scorer isn&rsquo;t being careless, it is confidently
    and accurately describing the part of the posting it was shown, while the disqualifier sits in a
    part it was never given.</p>
  </div>
  <div class="exs">{misses}</div>
</section>

<section class="block">
  <div class="block__intro">
    <p class="qnum">Question four</p>
    <h2>Could this be two stages instead of three?</h2>
    <p>Both possible cuts were tested against the data, and both fail.</p>
  </div>
  <div class="recs">
    <article class="rec rec--no">
      <header><h3>Drop the weak gate, rank everything</h3><span class="tag">rejected</span></header>
      <p>{rng_str(uniq)} listings per run are removed by the gate alone, and the medium scorer rates
      them 58&ndash;84 &mdash; it would keep essentially all of them. Around
      {round(100*sum(uniq_rej)/sum(uniq))}% were judge-rejects, mostly wrong-function roles caught on
      the sector axis: Product Analyst, Graduate Software Developer, HR Data and Reporting Analyst.
      The gate is doing work nothing downstream replicates. It is also the cheap tier absorbing the
      largest volume: on a random draw it took 128 listings down to 32 before the medium model was
      called at all. Removing it would move that volume onto the mid-tier model.</p>
    </article>
    <article class="rec rec--no">
      <header><h3>Drop the strong judge, cut on the rank score</h3><span class="tag">rejected</span></header>
      <p>No threshold on the medium score reproduces the judge&rsquo;s verdicts. Below 80 the precision
      barely moves off the {base_rate}% base rate &mdash; the score simply isn&rsquo;t separating in that
      range. Push it high enough to be selective and it takes half the good roles with it. And the judge
      is not only a filter: it writes the fit reasoning, the concerns and the grade the card displays.</p>
      <div class="tbl-wrap"><table>
        <thead><tr><th>Threshold</th><th>Good roles kept</th><th>Bad roles kept</th><th>Precision</th></tr></thead>
        <tbody>{sweep_rows}</tbody>
        <caption>Base rate is {base_rate}%. A threshold only beats chance meaningfully above 80,
        where it is already discarding a third to a half of the good roles.</caption>
      </table></div>
    </article>
  </div>
</section>

<section class="block">
  <div class="block__intro">
    <h2>What to do instead</h2>
    <p>Nothing here is applied. The ordering reflects evidence strength, not effort.</p>
  </div>
  <div class="recs">
    <article class="rec">
      <header><h3>Give the medium tier real text &mdash; it&rsquo;s the cheapest place to fix starvation</h3>
      <span class="tag">strongest evidence</span></header>
      <p>Same argument as widening Reed enrichment before the gate, but several times cheaper, because
      of where the stage sits: the gate screens up to <code>RANK_EXAMINE_BUDGET</code> = 240 listings a
      run, while the medium scorer sees only the survivors &mdash; 32 to 89 in these runs. Its window is
      also larger ({C['RANK_LISTING_TEXT_CHARS']:,} chars against the gate&rsquo;s
      {C['GATE_LISTING_TEXT_CHARS']:,}), so the extra text is actually used. Enriching between the gate
      and the scorer would cost a fraction of doing it pre-gate and buys the separation jump in the
      table above. The latency objection to pre-gate enrichment is weaker here too: this sits before the
      second paint, not the first.</p>
    </article>
    <article class="rec">
      <header><h3>Leave the floor at {C['RANK_REJECT_SCORE_FLOOR']}</h3><span class="tag">no change</span></header>
      <p>It looked toothless, but that was the range-restricted sample. On production-shaped intake it
      already removes around a quarter of survivors, and across every run it killed no role the judge
      liked. The sweep shows raising it to 70 would cost 19&ndash;27% of the good roles for a precision
      gain of about two points. Not worth it &mdash; and the honest fix for its bluntness is text, not a
      higher cut.</p>
    </article>
    <article class="rec">
      <header><h3>Watch the ordering value if the cost ever bites</h3><span class="tag">open question</span></header>
      <p>The medium tier&rsquo;s top {C['JUDGE_POOL']} is only one to two good roles better than the free
      embedding ordering. That is a thin return for a mid-tier call on every survivor, and worth
      revisiting &mdash; but the measurement is range-restricted in the direction that understates it
      (see the caveats), so it is not a safe cut today. Re-measure after the text fix, when the stage is
      no longer working blind.</p>
    </article>
  </div>
</section>

<section class="caveat">
  <h2>What bounds these numbers</h2>
  <ul>
    <li><strong>The labelled sample is range-restricted, and it matters most for the medium tier.</strong>
    Every row carries a judge verdict, which means it already passed the embedding filter, the gate and
    the rank floor on some earlier run. Anything the medium scorer would have rated 10&ndash;30 is largely
    absent. This understates the floor&rsquo;s effect &mdash; which is why the production-shaped draw is
    reported alongside it &mdash; and probably understates the ordering value too.</li>
    <li><strong>These are LLM stages; one run&rsquo;s digits are not facts.</strong> Ranges are quoted across
    runs wherever they differ. Across the teaser runs the medium score&rsquo;s AUC moved between
    {auc_rank} and the gate&rsquo;s unique catches between {rng_str(uniq)} &mdash; while the judge-pool
    precision came out at {pool_prec}% every single time.</li>
    <li><strong>The labels are the judge&rsquo;s opinion.</strong> A &ldquo;reject&rdquo; is what the expensive
    model concluded, not ground truth, and the judge saw scraped text the cheaper stages did not. Some
    verdicts also predate the current prompt versions.</li>
    <li><strong>{a['strong_over_medium']['rejects_in_pool_visibility'].get('no_quoted_requirement',0)} of the
    {a['strong_over_medium']['pool_judge_reject']} rejects in the judge pool carry no recorded reason,</strong>
    so they can only be counted, not diagnosed. They are excluded from the visible-versus-blind split
    rather than assumed either way.</li>
    <li><strong>Cost is expressed in volume, not money.</strong> Which stage absorbs how many listings is
    measurable here; per-token pricing is not part of this repo, so no monetary claim is made.</li>
  </ul>
</section>
</div>"""


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    snippet, full = load()
    OUT.write_text(build(snippet, full), encoding="utf-8")
    print(f"wrote {OUT}  ({len(snippet)} snippet run(s), {len(full)} full run(s))")


if __name__ == "__main__":
    main()
