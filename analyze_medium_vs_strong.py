"""Read-only-ish diagnostic report comparing the medium-tier scorer (full_auto.rank_gate,
MID_MODEL) against the strong-tier judge's historical, already-persisted verdicts
(full_auto.final_evaluation_split, EXP_MODEL) for the SAME real jobs.

Historical rank_gate scores are NOT recoverable (see analyze_rank_gate.py's docstring --
gate_cache keys on a one-way hash of the profile signature, and the signature's `sectors`
field is itself non-deterministic LLM output, so the exact old cache key can't be rebuilt).
So the medium-AI side of this report is a FRESH, live `rank_gate` call over real jobs that
already have a real historical strong-AI verdict sitting in `jobs_seen.eval_verdict` --
this makes the comparison possible without spending anything on the judge tier at all.

Sourcing:
  - Selects the --sample-size (default 100) MOST RECENTLY EVALUATED `JobSeen` rows (by
    `evaluated_at`) across every profile that still exists in the DB (profiles whose
    id no longer has a `Profile` row -- e.g. deleted since -- are skipped, since
    `build_snapshot` needs a live profile to build role clusters). Biasing toward the most
    recent rows avoids mixing in results scored under an older `FINAL_EVAL_PROMPT_VERSION`/
    `screen_v`/`rank_v` prompt revision.
  - For each involved profile: one real `build_snapshot()` call (small, already-accepted
    cheap-model cost, same as every other harness in this repo) to get its CURRENT role
    clusters, then a free local cosine re-score (`engine_svc._score_rows`) to assign each
    sampled job to a cluster and pull embed_score + the historical `_eval_verdict`/
    `_eval_analysis` that ride along on that row for free.
  - The ONLY live LLM spend in this script: one real `full_auto.rank_gate()` call per
    cluster (MID_MODEL, batched internally at `_GATE_BATCH`=20/call) over the sampled jobs
    in that cluster. `gate_cache` is bypassed (read+write), same as tests/gate_harness.py,
    so every job gets a genuinely fresh score and the production cache is untouched.
  - The strong-AI side makes NO live call. The exact candidate-side text the judge saw
    historically was never persisted (only the job's own full_text/snippet is), so a live
    re-call wouldn't be a byte-exact replay of the past anyway. Instead this reconstructs
    the prompt that would/have been sent -- the job's real historical full_text/snippet via
    `full_auto._final_eval_job_block`, plus the CURRENT profile's real
    `snapshot.cv_text_for_cluster` -- purely for display, and pairs it with the verdict that
    ACTUALLY happened (`eval_verdict`/`eval_analysis`, read straight off the row).
  - Each job record's `job_text` is the COMPLETE text either stage actually scored/judged on
    (a real scrape when `has_real_scrape`, else the discovery snippet -- see
    `engine._rows_to_dicts`), not an arbitrarily short display slice.
  - `has_recorded_reasoning` is False for a "reject" verdict that never tripped a
    DISQUALIFIER rule -- it simply wasn't chosen among the judge's bounded strong/backup
    shortlist that run, so nothing was ever written down explaining why. `claude_gap_note`
    is left `None` by this script (it makes no call, of any tier, to explain these) --
    filling it in is a manual/external step: read `job_text` against the candidate's
    `cv_text_for_cluster` and judge for yourself, or have an assistant do it, rather than
    spending further judge-tier credits on jobs the pipeline already scored once.

Usage:
    venv/Scripts/python analyze_medium_vs_strong.py
    venv/Scripts/python analyze_medium_vs_strong.py --sample-size 50

Writes a JSON report and a self-contained browsable HTML report to --out-dir
(default: tests/medium_vs_strong_reports/).
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sample-size", type=int, default=100,
                    help="How many most-recently-evaluated JobSeen rows (across all still-existing "
                         "profiles) to re-score with a live rank_gate call (default 100).")
    p.add_argument("--high-rank-floor", type=float, default=70.0,
                    help="rank_score at/above this, paired with a 'reject' historical verdict, is "
                         "flagged as a medium-AI-confident/strong-AI-said-no mismatch (default 70).")
    p.add_argument("--low-rank-ceiling", type=float, default=55.0,
                    help="rank_score below this, paired with a 'strong' historical verdict, is flagged "
                         "as a medium-AI-would-have-buried-it mismatch (default 55, matching the real "
                         "RANK_REJECT_SCORE_FLOOR production uses).")
    p.add_argument("--out-dir", default=None,
                    help="Output directory (default: tests/medium_vs_strong_reports/).")
    return p.parse_args()


def _install_capture_hooks(engine, llm_calls, call_ctx):
    """Bypasses rank_gate's gate_cache (read+write) so every sampled job gets a fresh
    score this run, and wraps engine.llm to log every prompt/response tagged with
    whichever profile/cluster the caller is currently processing (call_ctx) -- same
    pattern as tests/gate_harness.py/tests/prompt_pipeline_harness.py."""
    engine._gate_cache_lookup = lambda keys: {}
    engine._gate_cache_store = lambda entries: None
    original_llm = engine.llm

    def _logging_llm(prompt, system="", model=engine.MID_MODEL, require_json=False, temperature=0.2):
        start = time.monotonic()
        entry = {
            "profile_id": call_ctx.get("profile_id"), "profile_name": call_ctx.get("profile_name"),
            "cluster_idx": call_ctx.get("cluster_idx"), "cluster_label": call_ctx.get("cluster_label"),
            "prompt": prompt, "system": system, "model": model, "temperature": temperature,
        }
        try:
            raw = original_llm(prompt, system=system, model=model,
                                require_json=require_json, temperature=temperature)
        except Exception as e:
            entry.update(raw_response=None, error=str(e), latency_seconds=round(time.monotonic() - start, 3))
            llm_calls.append(entry)
            raise
        entry.update(raw_response=raw, error=None, latency_seconds=round(time.monotonic() - start, 3))
        llm_calls.append(entry)
        return raw

    engine.llm = _logging_llm


def _reconstruct_judge_prompt(engine, job_dict, cv_text):
    """Mirrors tests/prompt_pipeline_harness.py's _dry_run_judge template (there is no
    standalone prompt-builder function for this stage in full_auto.py itself), applied
    to ONE job at a time since each historical row was independently judged (possibly
    alongside other jobs we don't have here) -- this shows what the judge's per-job
    payload block looks like, not a byte-exact replay of whatever batch it originally
    rode in with."""
    cv_text_trunc = cv_text[:5000]
    job_block = engine._final_eval_job_block(0, job_dict)
    return f"""Candidate Background Profile:
{cv_text_trunc}

Judge the 1 complete job posting below. Return up to {engine.FINAL_PICKS} genuinely strong fits in
"strong" (best first), and up to 3 least-bad disqualifier-only survivors in "backup" (best first; empty
if "strong" already covers it or nothing qualifies). For any job you hard-exclude from both lists via a
DISQUALIFIERS rule, add it to "disqualified" with a short reason.

Jobs Payload:
{job_block}"""


def _score_stats(scores):
    if not scores:
        return None
    s = sorted(scores)
    return {
        "n": len(s), "min": round(min(s), 1), "max": round(max(s), 1),
        "avg": round(sum(s) / len(s), 1), "median": round(s[len(s) // 2], 1),
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()

    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))

    import app.database as database
    import app.models as models
    from app.services import snapshot
    from app.services import engine as engine_svc
    import full_auto as engine  # noqa: E402 -- after app.* imports so .env is loaded

    db = database.SessionLocal()
    try:
        existing_profile_ids = [pid for (pid,) in db.query(models.Profile.id).all()]
        if not existing_profile_ids:
            raise SystemExit("no profiles exist in the DB.")

        rows = (
            db.query(models.JobSeen)
            .filter(models.JobSeen.eval_verdict.isnot(None),
                    models.JobSeen.profile_id.in_(existing_profile_ids))
            .order_by(models.JobSeen.evaluated_at.desc())
            .limit(args.sample_size)
            .all()
        )
        if not rows:
            raise SystemExit("no JobSeen rows with a historical eval_verdict for any still-existing "
                              "profile -- run a real search at least once first.")

        by_profile_rows = defaultdict(list)
        for r in rows:
            by_profile_rows[r.profile_id].append(r)

        print(f"[analyze] sampled {len(rows)} most-recently-evaluated job(s) across "
              f"{len(by_profile_rows)} profile(s): "
              + ", ".join(f"profile {pid} ({len(v)})" for pid, v in by_profile_rows.items()))
        print(f"[analyze] LIVE calls will be made to {engine.MID_MODEL} only (rank_gate, batches of "
              f"{engine._GATE_BATCH}). No calls to {engine.EXP_MODEL} (the judge) -- its side of this "
              f"report reuses each job's real historical verdict.")

        llm_calls: list = []
        call_ctx: dict = {}
        _install_capture_hooks(engine, llm_calls, call_ctx)

        job_records = []
        profile_summaries = []

        for profile_id, prows in by_profile_rows.items():
            profile = db.get(models.Profile, profile_id)
            call_ctx["profile_id"] = profile_id
            call_ctx["profile_name"] = profile.name
            print(f"\n[analyze] profile {profile_id} ({profile.name!r}) -- {len(prows)} sampled job(s)")

            snap = snapshot.build_snapshot(db, profile_id)
            role_clusters = snap["role_clusters"]
            eng_profile = snap["engine_profile"]
            cv_text_base = snap["cv_text_base"]
            if not role_clusters:
                print(f"  [skip] profile {profile_id} has no role-target clusters any more.")
                continue

            cluster_profiles = []
            for idx, rc in enumerate(role_clusters):
                cp = dict(eng_profile)
                cp["search_terms"] = rc.get("roles") or eng_profile.get("search_terms")
                cp["_multi_cluster"] = len(role_clusters) > 1
                cluster_profiles.append(cp)

            cluster_texts = [c["weighted_text"] for c in role_clusters]
            cluster_embeddings = engine.get_embeddings_batch(cluster_texts)

            scored = engine_svc._score_rows(prows, cluster_embeddings)
            by_cluster = defaultdict(list)
            for d in scored:
                by_cluster[d["_cluster"]].append(d)

            profile_summaries.append({
                "profile_id": profile_id, "profile_name": profile.name, "n_jobs": len(prows),
                "role_clusters": [{"label": c.get("label"), "roles": c.get("roles")} for c in role_clusters],
            })

            for idx, cluster in enumerate(role_clusters):
                cluster_jobs = by_cluster.get(idx, [])
                if not cluster_jobs:
                    continue
                call_ctx["cluster_idx"] = idx
                call_ctx["cluster_label"] = cluster.get("label")
                print(f"  [{cluster.get('label')}] {len(cluster_jobs)} job(s) -> live rank_gate")
                engine.rank_gate(cluster_jobs, cluster_profiles[idx])
                scores = [d.get("_rank_score", 50.0) for d in cluster_jobs]
                print(f"    rank_score spread: min={min(scores):.0f} max={max(scores):.0f} "
                      f"avg={sum(scores)/len(scores):.0f}")

                cluster_roles = cluster.get("roles") or []
                cv_text = (snapshot.cv_text_for_cluster(cv_text_base, cluster_roles)
                           if cluster_roles else cv_text_base)

                for d in cluster_jobs:
                    eval_analysis_raw = d.get("_eval_analysis")
                    try:
                        eval_analysis = json.loads(eval_analysis_raw) if eval_analysis_raw else {}
                    except Exception:
                        eval_analysis = {"_unparsed": eval_analysis_raw}

                    # d["full_text"] is already "real scrape or snippet" (see
                    # engine._rows_to_dicts) -- this IS the complete text either stage
                    # actually scored/judged on, up to FINAL_EVAL_JOB_TEXT_CHARS (8000)
                    # when a real scrape exists. Surface it whole so a reviewer isn't
                    # stuck with just the short discovery snippet.
                    job_text = d.get("full_text") or ""
                    has_reasoning = bool(eval_analysis.get("concerns") or eval_analysis.get("summary")
                                          or eval_analysis.get("match_reasons") or eval_analysis.get("top_match_reason"))

                    job_records.append({
                        "profile_id": profile_id, "profile_name": profile.name,
                        "cluster_idx": idx, "cluster_label": cluster.get("label"),
                        "identity": d.get("_identity"), "title": d.get("title"),
                        "company": d.get("company") or "", "location": d.get("location") or "",
                        "url": d.get("url") or "", "board": d.get("board") or "",
                        "job_text": job_text,
                        "has_real_scrape": bool(d.get("_has_full_text")),
                        "embed_score": round(d.get("embed_score", 0.0), 4),
                        "rank_score": round(d.get("_rank_score", 50.0), 1),
                        "rank_note": d.get("_rank_note", ""),
                        "rank_gate_failed": bool(d.get("_rank_gate_failed")),
                        "eval_verdict": d.get("_eval_verdict"),
                        "eval_analysis": eval_analysis,
                        "has_recorded_reasoning": has_reasoning,
                        # Filled in externally (not by any live call this script makes) for
                        # jobs where eval_verdict == "reject" with has_recorded_reasoning
                        # False -- the strong judge simply didn't pick these among its
                        # bounded shortlist, so nothing was ever written down explaining
                        # why. See analyze_medium_vs_strong.py's module docstring.
                        "claude_gap_note": None,
                        "judge_prompt_reconstructed": _reconstruct_judge_prompt(engine, d, cv_text),
                    })

        if not job_records:
            raise SystemExit("no jobs were scored -- nothing to report.")

        # ── Cross-tab stats ──────────────────────────────────────────────────────
        by_verdict = defaultdict(list)
        for j in job_records:
            by_verdict[j["eval_verdict"] or "unknown"].append(j["rank_score"])
        stats_by_verdict = {v: _score_stats(scores) for v, scores in by_verdict.items()}

        high_rank_low_verdict = [
            j for j in job_records
            if j["rank_score"] >= args.high_rank_floor and j["eval_verdict"] == "reject"
        ]
        low_rank_high_verdict = [
            j for j in job_records
            if j["rank_score"] < args.low_rank_ceiling and j["eval_verdict"] == "strong"
        ]
        high_rank_low_verdict.sort(key=lambda j: j["rank_score"], reverse=True)
        low_rank_high_verdict.sort(key=lambda j: j["rank_score"])

        print("\n=== Cross-tab: fresh rank_score by historical eval_verdict ===")
        for verdict, s in stats_by_verdict.items():
            print(f"  {verdict:<10} n={s['n']:<4} min={s['min']:<5} max={s['max']:<5} "
                  f"avg={s['avg']:<5} median={s['median']}")
        print(f"\n  MISMATCH (rank>={args.high_rank_floor:.0f} but historically REJECTED): "
              f"{len(high_rank_low_verdict)} / {len(by_verdict.get('reject', []))} reject(s)")
        print(f"  MISMATCH (rank<{args.low_rank_ceiling:.0f} but historically STRONG): "
              f"{len(low_rank_high_verdict)} / {len(by_verdict.get('strong', []))} strong(s)")

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sample_size_requested": args.sample_size,
            "sample_actual": len(job_records),
            "mid_model": engine.MID_MODEL,
            "exp_model": engine.EXP_MODEL,
            "rank_reject_score_floor": engine_svc.RANK_REJECT_SCORE_FLOOR,
            "high_rank_floor": args.high_rank_floor,
            "low_rank_ceiling": args.low_rank_ceiling,
            "profiles": profile_summaries,
            "stats_by_verdict": stats_by_verdict,
            "mismatch_counts": {
                "high_rank_low_verdict": len(high_rank_low_verdict),
                "low_rank_high_verdict": len(low_rank_high_verdict),
                "reject_total": len(by_verdict.get("reject", [])),
                "strong_total": len(by_verdict.get("strong", [])),
                "backup_total": len(by_verdict.get("backup", [])),
            },
            "high_rank_low_verdict_examples": high_rank_low_verdict,
            "low_rank_high_verdict_examples": low_rank_high_verdict,
            "jobs": job_records,
            "llm_calls": llm_calls,
            "warnings": [
                "Historical eval_verdict/eval_analysis are real, persisted strong-AI decisions from "
                "whenever that job was actually judged -- not re-derived here. rank_score is FRESH, "
                "computed just now, so it reflects the CURRENT rank_gate prompt/model, which may differ "
                "slightly from whatever was live when the historical verdict was produced.",
                "judge_prompt_reconstructed shows what the strong judge's payload for this ONE job would "
                "look like right now (current profile's cv_text_for_cluster + this job's real historical "
                "full_text/snippet) -- it is NOT sent to any model here, and is not necessarily identical "
                "to whatever batch/context the job was originally judged alongside.",
                "Only profiles that still exist in the DB are included -- profiles deleted since their "
                "jobs were judged are skipped, since role clusters can't be rebuilt without a live profile "
                "row.",
            ],
        }

        out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "tests" / "medium_vs_strong_reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
        json_path = out_dir / f"report_{stamp}.json"
        html_path = out_dir / f"report_{stamp}.html"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        html_path.write_text(render_html_report(report), encoding="utf-8")

        print(f"\n[analyze] wrote JSON report to {json_path}")
        print(f"[analyze] wrote HTML report to {html_path} -- open it in a browser")
    finally:
        db.close()


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; margin: 0; padding: 24px;
       background: #0b0d10; color: #e6e6e6; }
@media (prefers-color-scheme: light) { body { background: #f7f7f8; color: #16181d; } }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 17px; margin-top: 28px; }
h3 { font-size: 14px; }
.banner { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.banner.live { background: #1a3a2e; color: #7be0a8; }
.banner.warn { background: #3a1a1a; color: #f28b8b; }
.stat-row { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
.stat-tile { background: rgba(127,127,127,0.12); border-radius: 10px; padding: 10px 16px; min-width: 140px; }
.stat-tile .num { font-size: 22px; font-weight: 700; }
.stat-tile .label { font-size: 11px; opacity: 0.75; }
.stat-tile.mismatch { background: rgba(242,139,139,0.18); }
details { margin: 6px 0; border: 1px solid rgba(127,127,127,0.25); border-radius: 8px; padding: 6px 12px; }
summary { cursor: pointer; font-weight: 600; font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 10px; }
th, td { border-bottom: 1px solid rgba(127,127,127,0.2); padding: 5px 7px; text-align: left; vertical-align: top; }
th { cursor: pointer; user-select: none; position: sticky; top: 0; background: inherit; }
th:hover { opacity: 0.7; }
.badge { padding: 2px 7px; border-radius: 6px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.badge.strong { background: #1a3a2e; color: #7be0a8; }
.badge.backup { background: #3a2f10; color: #f2c675; }
.badge.reject { background: #3a1a1a; color: #f28b8b; }
.badge.mismatch { background: #5a1f8a; color: #e2b8ff; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }
input, select { padding: 5px 8px; border-radius: 6px; border: 1px solid rgba(127,127,127,0.4);
                background: transparent; color: inherit; font-size: 12.5px; }
tr.job-row { cursor: pointer; }
tr.job-row:hover { background: rgba(127,127,127,0.08); }
.row-detail { display: none; }
.row-detail.open { display: table-row; }
.row-detail td { background: rgba(127,127,127,0.06); }
pre { white-space: pre-wrap; word-break: break-word; font-size: 11.5px; max-height: 420px; overflow-y: auto;
      background: rgba(127,127,127,0.08); padding: 8px; border-radius: 6px; }
a { color: #7bb0f2; }
code { font-size: 11.5px; }
"""

_JS = """
function esc(s) {
  return String(s === undefined || s === null ? "" : s).replace(/[&<>"']/g, function(m) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m];
  });
}
function badge(text, cls) { return '<span class="badge ' + esc(cls || text) + '">' + esc(text) + '</span>'; }
function isMismatch(j) {
  return (j.rank_score >= REPORT.high_rank_floor && j.eval_verdict === "reject") ||
         (j.rank_score < REPORT.low_rank_ceiling && j.eval_verdict === "strong");
}

function renderDashboard() {
  var html = '<div class="stat-row">';
  html += '<div class="stat-tile"><div class="num">' + REPORT.sample_actual + '</div><div class="label">jobs scored</div></div>';
  ['strong','backup','reject'].forEach(function(v) {
    var s = REPORT.stats_by_verdict[v];
    if (!s) return;
    html += '<div class="stat-tile"><div class="num">' + s.avg + '</div><div class="label">avg rank_score, ' + v +
      ' (n=' + s.n + ', med=' + s.median + ')</div></div>';
  });
  html += '<div class="stat-tile mismatch"><div class="num">' + REPORT.mismatch_counts.high_rank_low_verdict +
    '</div><div class="label">rank&ge;' + REPORT.high_rank_floor + ' but REJECTED (of ' + REPORT.mismatch_counts.reject_total + ' reject)</div></div>';
  html += '<div class="stat-tile mismatch"><div class="num">' + REPORT.mismatch_counts.low_rank_high_verdict +
    '</div><div class="label">rank&lt;' + REPORT.low_rank_ceiling + ' but STRONG (of ' + REPORT.mismatch_counts.strong_total + ' strong)</div></div>';
  html += '</div>';
  document.getElementById('dashboard').innerHTML = html;
}

function renderProfiles() {
  var html = '';
  REPORT.profiles.forEach(function(p) {
    html += '<details><summary>Profile ' + p.profile_id + ' (' + esc(p.profile_name) + ') -- ' + p.n_jobs + ' job(s)</summary>';
    p.role_clusters.forEach(function(c) {
      html += '<div>[' + esc(c.label) + '] ' + esc((c.roles||[]).join(', ')) + '</div>';
    });
    html += '</details>';
  });
  document.getElementById('profiles').innerHTML = html;
}

function jobDetailHtml(j) {
  var html = '<div><b>URL:</b> <a href="' + esc(j.url) + '" target="_blank" rel="noopener">' + esc(j.url) + '</a></div>';
  html += '<div class="small" style="margin-top:4px">' + (j.has_real_scrape ? 'real page scrape' : 'discovery snippet only') +
    ', ' + (j.job_text||'').length + ' char(s) | rank note: ' + esc(j.rank_note || '(none)') + '</div>';
  html += '<details style="margin-top:6px" open><summary>Full job text (complete, as scored/judged)</summary><pre>' +
    esc(j.job_text) + '</pre></details>';
  html += '<details style="margin-top:8px"' + (j.has_recorded_reasoning ? '' : ' open') +
    '><summary>Historical strong-AI verdict + reasoning (real, persisted)' +
    (j.has_recorded_reasoning ? '' : ' -- NONE recorded, see claude_gap_note below') + '</summary><pre>' +
    esc(JSON.stringify(j.eval_analysis, null, 2)) + '</pre></details>';
  if (!j.has_recorded_reasoning && j.eval_verdict === 'reject') {
    html += '<div style="margin-top:8px"><b>Why this probably wasn\'t a strong fit (Claude\'s read, not the pipeline\'s):</b><br>' +
      esc(j.claude_gap_note || '(not yet annotated)') + '</div>';
  }
  html += '<details style="margin-top:6px"><summary>Reconstructed strong-judge prompt (NOT sent -- for display only)</summary><pre>' +
    esc(j.judge_prompt_reconstructed) + '</pre></details>';
  return html;
}

var sortKey = null, sortDir = 1;

function renderJobs() {
  var q = document.getElementById('search').value.toLowerCase();
  var verdictF = document.getElementById('verdictFilter').value;
  var mismatchF = document.getElementById('mismatchFilter').value;
  var rows = REPORT.jobs.filter(function(j) {
    if (q && ((j.title||'') + ' ' + (j.company||'')).toLowerCase().indexOf(q) === -1) return false;
    if (verdictF && j.eval_verdict !== verdictF) return false;
    if (mismatchF === 'yes' && !isMismatch(j)) return false;
    return true;
  });
  if (sortKey) {
    rows = rows.slice().sort(function(a, b) {
      var av = a[sortKey], bv = b[sortKey];
      if (av === null || av === undefined) av = '';
      if (bv === null || bv === undefined) bv = '';
      if (av === bv) return 0;
      return (av > bv ? 1 : -1) * sortDir;
    });
  }
  document.getElementById('rowCount').textContent = rows.length + ' / ' + REPORT.jobs.length;
  var body = document.getElementById('jobsBody');
  body.innerHTML = '';
  rows.forEach(function(j) {
    var tr = document.createElement('tr');
    tr.className = 'job-row';
    var mismatch = isMismatch(j);
    tr.innerHTML = '<td>' + esc(j.profile_name) + '<div class="small">' + esc(j.cluster_label) + '</div></td>' +
      '<td>' + esc(j.title) + '<div class="small">' + esc(j.company) + '</div></td>' +
      '<td>' + j.embed_score.toFixed(3) + '</td>' +
      '<td>' + j.rank_score + (j.rank_gate_failed ? ' <span class="small">(fail-open)</span>' : '') + '</td>' +
      '<td>' + badge(j.eval_verdict || 'unknown', j.eval_verdict) + (mismatch ? ' ' + badge('MISMATCH', 'mismatch') : '') + '</td>';
    var detailRow = document.createElement('tr');
    detailRow.className = 'row-detail';
    detailRow.innerHTML = '<td colspan="5">' + jobDetailHtml(j) + '</td>';
    tr.addEventListener('click', function() { detailRow.classList.toggle('open'); });
    body.appendChild(tr);
    body.appendChild(detailRow);
  });
}

function renderPromptLog() {
  var html = '';
  REPORT.llm_calls.forEach(function(c, i) {
    var label = 'profile ' + c.profile_id + ' / cluster ' + c.cluster_idx + ' (' + esc(c.cluster_label) + ')' + (c.error ? ' [ERROR]' : '');
    var meta = c.model + ', temp=' + c.temperature + (c.latency_seconds !== null && c.latency_seconds !== undefined ? ', ' + c.latency_seconds + 's' : '');
    html += '<details><summary>Call ' + (i+1) + ': ' + label + ' -- ' + meta + '</summary>';
    if (c.system) html += '<div><b>System:</b><pre>' + esc(c.system) + '</pre></div>';
    html += '<div><b>Prompt:</b><pre>' + esc(c.prompt) + '</pre></div>';
    if (c.error) {
      html += '<div><b>Error:</b><pre>' + esc(c.error) + '</pre></div>';
    } else {
      html += '<div><b>Raw response:</b><pre>' + esc(c.raw_response) + '</pre></div>';
    }
    html += '</details>';
  });
  document.getElementById('promptLog').innerHTML = html;
}

document.addEventListener('DOMContentLoaded', function() {
  renderDashboard();
  renderProfiles();
  renderJobs();
  renderPromptLog();
  document.querySelectorAll('#jobsTable th[data-key]').forEach(function(th) {
    th.addEventListener('click', function() {
      var key = th.dataset.key;
      sortDir = (sortKey === key) ? -sortDir : 1;
      sortKey = key;
      renderJobs();
    });
  });
  document.getElementById('search').addEventListener('input', renderJobs);
  document.getElementById('verdictFilter').addEventListener('change', renderJobs);
  document.getElementById('mismatchFilter').addEventListener('change', renderJobs);
});
"""

_HTML_SHELL = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Medium vs Strong AI Alignment Report</title>
<style>__CSS__</style>
</head>
<body>
  <h1>Medium-AI vs Strong-AI Alignment Report</h1>
  <div class="banner live">LIVE rank_gate calls (__MID_MODEL__) over __SAMPLE__ real, already-judged jobs. No new __EXP_MODEL__ (judge) calls -- verdicts are real historical data.</div>
  <div id="dashboard"></div>
  <h2>Profiles included</h2>
  <div id="profiles"></div>
  <h2>Jobs (<span id="rowCount"></span>)</h2>
  <div class="controls">
    <input id="search" placeholder="Search title/company...">
    <select id="verdictFilter"><option value="">All verdicts</option><option value="strong">strong</option><option value="backup">backup</option><option value="reject">reject</option></select>
    <select id="mismatchFilter"><option value="">All rows</option><option value="yes">Mismatches only</option></select>
  </div>
  <table id="jobsTable">
    <thead><tr>
      <th data-key="profile_name">Profile / Cluster</th>
      <th data-key="title">Title</th>
      <th data-key="embed_score">Embed score</th>
      <th data-key="rank_score">Rank score (fresh, medium AI)</th>
      <th data-key="eval_verdict">Historical verdict (strong AI)</th>
    </tr></thead>
    <tbody id="jobsBody"></tbody>
  </table>
  <h2>rank_gate prompt / response log (__CALL_COUNT__ live call(s))</h2>
  <div id="promptLog"></div>
<script>
const REPORT = __REPORT_JSON__;
__JS__
</script>
</body>
</html>
"""


def render_html_report(report) -> str:
    report_json = json.dumps(report).replace("</", "<\\/")
    html = _HTML_SHELL
    html = html.replace("__CSS__", _CSS)
    html = html.replace("__MID_MODEL__", report["mid_model"])
    html = html.replace("__EXP_MODEL__", report["exp_model"])
    html = html.replace("__SAMPLE__", str(report["sample_actual"]))
    html = html.replace("__CALL_COUNT__", str(len(report["llm_calls"])))
    html = html.replace("__REPORT_JSON__", report_json)
    html = html.replace("__JS__", _JS)
    return html


if __name__ == "__main__":
    main()
