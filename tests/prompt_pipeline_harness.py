"""Read-only diagnostic harness that traces real, live LLM calls through ALL THREE
AI stages of the search pipeline in one run, for a small real sample of cached
listings: the weak gate (full_auto.screen_gate, CHEAP_MODEL), the medium scorer
(full_auto.rank_gate, MID_MODEL), and the strong judge (full_auto.
final_evaluation_split, EXP_MODEL). See CLAUDE.md's "search pipeline" section for
what each stage does.

Unlike tests/gate_harness.py (screen_gate only) and analyze_rank_gate.py (rank_gate
only), this follows a handful of real jobs THROUGH all three stages in sequence, so
you can see exactly what prompt each stage received and what it produced for the
SAME real listing -- and audit whether each stage's prompt gives the AI the best
possible understanding of the candidate and the role.

Historical prompt/response logs for these stages are not persisted anywhere (gate
decisions are cached as booleans/reason-codes, not raw prompt text; final-judge
verdicts are cached as parsed JSON, not the raw call) and prompts have since changed
version (FINAL_EVAL_PROMPT_VERSION, screen_v10, rank_v5) -- so old runs cannot be
replayed byte-for-byte. This makes fresh, live calls against the CURRENT prompts
instead.

Cost: small and bounded, same order of magnitude as gate_harness.py/
analyze_rank_gate.py -- one build_snapshot call (cheap-model region inference), one
embeddings call, and per role cluster: one screen_gate call (CHEAP_MODEL), one
rank_gate call (MID_MODEL), and one final_evaluation_split call (EXP_MODEL, the most
expensive tier) -- NOT a full search: no discovery, no Phase 5 scraping, no
gate_cache pollution (bypassed both read and write, same as gate_harness.py), and no
SearchRun/Role rows created.

To keep every stage populated with real data even though the small sample won't
always survive every filter by chance, the judge stage sends its top-ranked N
regardless of whether they'd actually clear RANK_REJECT_SCORE_FLOOR in production --
each job's record says plainly whether it would really have reached the judge on a
live run. Nothing here is a substitute for gate_harness.py/analyze_rank_gate.py's own
larger-sample statistical reports; this is depth (follow real jobs end-to-end,
capture every exact prompt/response) over breadth.

Usage:
    venv/Scripts/python tests/prompt_pipeline_harness.py
    venv/Scripts/python tests/prompt_pipeline_harness.py --profile-id 11 --gate-sample-per-cluster 12
    venv/Scripts/python tests/prompt_pipeline_harness.py --dry-run --gate-sample-per-cluster 5

--dry-run builds and shows the real prompts every stage would send, with placeholder
verdicts, and makes NO live LLM calls (build_snapshot's own small one-off region-
inference/role-clustering call still runs, same as gate_harness.py's --dry-run).

Writes a JSON report and a self-contained browsable HTML report to --out-dir
(default: tests/prompt_pipeline_reports/).
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_AXIS_ORDER = ("sector_ok", "hard_gate_ok", "listing_ok", "seniority_ok", "requirements_ok",
               "skills_ok", "salary_ok", "work_arrangement_ok")

_REASON_LABELS = {
    "ok": "ok",
    "seniority_high": "seniority too high", "seniority_low": "seniority too low",
    "too_many_gaps": "too many stated must-have gaps",
    "requirement_gap": "conflicts with a candidate-specific requirement",
    "skills_gap": "core skills mismatch", "salary_mismatch": "salary below stated floor",
    "arrangement_mismatch": "work-arrangement conflict",
    "hard_filter": "hit a candidate avoid/must-have filter",
    "not_a_real_listing": "not a real single job posting (aggregator/category page or board boilerplate)",
    "sector_ambiguous": "role-function fit genuinely unsure (not dropped, but flagged as a hint "
                        "downstream)",
    "sector_mismatch": "off-sector (different job function) -- unconditional gate drop",
    "gate_error": "gate LLM call failed (fail-open)",
    "missing_decision": "omitted from gate response (fail-open)",
    "DRY_RUN_PLACEHOLDER": "dry run -- no real judgment was made",
}

# Which profile fields feed which prompt(s), for the "profile formation" panel.
# Extends gate_harness.py's _FIELD_DRIVES with rank/judge destinations, since this
# harness (unlike gate_harness.py) spans all three stages.
_FIELD_DRIVES = {
    "search_terms": "WEAK GATE's ROLE FUNCTION FIT block + MEDIUM SCORER's 'Candidate target roles' line "
                     "('Candidate target roles')",
    "target_role_weight_tiers": "annotates target roles in both the WEAK GATE and MEDIUM SCORER prompts "
                                 "with tick/cross-derived priority labels",
    "avoid": "WEAK GATE's CANDIDATE HARD FILTERS block -> hard_gate_ok (unconditional drop); also reaches "
             "the STRONG JUDGE via cv_text_base's 'HARD EXCLUSIONS' line -> DISQUALIFIER rule 7",
    "must_have": "WEAK GATE's CANDIDATE HARD FILTERS block -> hard_gate_ok (unconditional drop); also "
                 "reaches the STRONG JUDGE via cv_text_base's 'HARD REQUIREMENTS' line -> DISQUALIFIER rule 7",
    "seniority": "WEAK GATE's SENIORITY block + MEDIUM SCORER prompt; also reaches the STRONG JUDGE via "
                 "cv_text_base's 'Seniority:' line -> DISQUALIFIER rule 1",
    "key_skills": "WEAK GATE's SENIORITY + CORE SKILLS OVERLAP blocks, MEDIUM SCORER prompt; also reaches "
                  "the STRONG JUDGE via cv_text_base's 'Skills:' line",
    "skill_weight_tiers": "annotates core skills with tick/cross-derived priority labels in the WEAK GATE "
                          "and MEDIUM SCORER prompts",
    "skill_evidence_tiers": "annotates core skills with evidence-strength caveats (e.g. 'familiar evidence "
                            "only') in the WEAK GATE and MEDIUM SCORER prompts",
    "requirements": "WEAK GATE's CANDIDATE-SPECIFIC REQUIREMENTS block -> requirements_ok",
    "soft_must_have": "folded into the WEAK GATE's requirements block as a preference, not a hard requirement",
    "soft_avoid": "folded into the WEAK GATE's requirements block as a dislike, not a hard exclusion",
    "salary_floor": "WEAK GATE's SALARY FIT block -> salary_ok",
    "work_types": "WEAK GATE's WORK ARRANGEMENT block -> work_arrangement_ok; also reaches the STRONG JUDGE "
                  "via cv_text_base's 'Location:' line -> DISQUALIFIER rule 2",
    "location": "WEAK GATE's WORK ARRANGEMENT block ('Candidate location'); also reaches the STRONG JUDGE "
                "via cv_text_base's 'Location:' line",
    "hard_axes": "promotes a normally-soft WEAK GATE axis to an unconditional drop (adds NON-NEGOTIABLE "
                 "wording to that axis's prompt text)",
    "candidate_brief": "WEAK GATE + MEDIUM SCORER 'CANDIDATE BACKGROUND' block; also reaches the STRONG "
                       "JUDGE via cv_text_base's 'Skill evidence detail' line",
}
# Judge-only fields: not part of eng_profile at all, read straight off the Profile row
# and baked into cv_text_base -- gate_harness.py's screen-only report has no
# equivalent of these since screen_gate/rank_gate never see them.
_JUDGE_ONLY_FIELD_DRIVES = {
    "intent_text": "STRONG JUDGE ONLY, via cv_text_base's 'What the candidate is looking for (in their own "
                   "words)' line -- the candidate's own free-text statement, given authoritative weight; "
                   "never reaches the weak gate or medium scorer",
    "search_feedback": "STRONG JUDGE ONLY, via cv_text_base's 'Candidate's feedback on recent search "
                       "results' line -- a steer for this run, not enforced like a hard filter",
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile-id", type=int, default=None,
                    help="Defaults to the single active profile.")
    p.add_argument("--cluster", default=None,
                    help="Restrict to one role cluster (case-insensitive substring match against its "
                         "label). Default: all clusters.")
    p.add_argument("--gate-sample-per-cluster", type=int, default=15,
                    help="How many top embed-ranked real listings per cluster to send through a live "
                         "screen_gate call (default 15; clamped to 20 = _GATE_BATCH so every cluster is "
                         "exactly one gate call and one rank call).")
    p.add_argument("--judge-sample-per-cluster", type=int, default=4,
                    help="How many of the top rank-scored gate survivors per cluster to send through a "
                         "live final_evaluation_split call (default 4). Sent regardless of whether they "
                         "clear RANK_REJECT_SCORE_FLOOR, to guarantee real judge output even on a small "
                         "sample -- see module docstring.")
    p.add_argument("--out-dir", default=None,
                    help="Output directory (default: tests/prompt_pipeline_reports/).")
    p.add_argument("--dry-run", action="store_true",
                    help="Build and display the real prompts every stage would send, with placeholder "
                         "verdicts -- NO live screen_gate/rank_gate/final_evaluation_split/llm() calls, "
                         "no cost beyond build_snapshot's own small one-off call.")
    return p.parse_args()


def _resolve_profile(db, models, profile_id):
    if profile_id is None:
        active = db.query(models.Profile).filter(models.Profile.is_active.is_(True)).all()
        if len(active) != 1:
            raise SystemExit(
                f"--profile-id required: found {len(active)} active profiles "
                f"({[p.id for p in active]}), expected exactly 1."
            )
        profile_id = active[0].id
    profile = db.get(models.Profile, profile_id)
    if profile is None:
        raise SystemExit(f"profile {profile_id} not found")
    return profile


def _decode_reason(packed_reason):
    # 8 codes since screen_v9, unchanged by screen_v10 (sector_code appended last --
    # see full_auto.screen_gate's docstring); older-shaped strings still decode fine,
    # sector just reads as "ok".
    parts = (list((packed_reason or "").split("|")) + ["ok"] * 8)[:8]
    (seniority_code, req_code, skills_code, salary_code, arr_code, hard_code, listing_code,
     sector_code) = parts
    return {
        "seniority": _REASON_LABELS.get(seniority_code, seniority_code),
        "requirements": _REASON_LABELS.get(req_code, req_code),
        "skills": _REASON_LABELS.get(skills_code, skills_code),
        "salary": _REASON_LABELS.get(salary_code, salary_code),
        "work_arrangement": _REASON_LABELS.get(arr_code, arr_code),
        "hard_filter": _REASON_LABELS.get(hard_code, hard_code),
        "listing": _REASON_LABELS.get(listing_code, listing_code),
        "sector": _REASON_LABELS.get(sector_code, sector_code),
    }


def _install_capture_hooks(engine, llm_calls, call_ctx):
    """Bypasses screen_gate/rank_gate's shared gate_cache (read AND write, exactly
    like gate_harness.py) so every sampled job gets a genuinely fresh judgment at
    every stage and the shared production cache is left untouched, then wraps
    llm() to log the exact prompt/response of every call plus which stage/cluster
    it belongs to (from the mutable `call_ctx`, updated by the caller immediately
    before each stage's real function is invoked -- safe because this harness runs
    each cluster's three stages sequentially, never concurrently, even though
    rank_gate internally uses a small ThreadPoolExecutor: a single-cluster sample
    is clamped to one _GATE_BATCH, so that pool only ever holds one batch)."""
    engine._gate_cache_lookup = lambda keys: {}
    engine._gate_cache_store = lambda entries: None

    original_llm = engine.llm

    # **kw so full_auto.llm's prompt-cache params (stage/cache_key/
    # cache_retention) pass straight through -- an enumerated signature here
    # TypeErrors the moment that function grows another keyword.
    def _logging_llm(prompt, system="", model=engine.CHEAP_MODEL, require_json=False,
                     temperature=0.2, **kw):
        start = time.monotonic()
        entry = {
            "stage": call_ctx.get("stage"), "cluster_idx": call_ctx.get("cluster_idx"),
            "cluster_label": call_ctx.get("cluster_label"),
            "prompt": prompt, "system": system, "model": model, "temperature": temperature,
            "dry_run": False,
        }
        try:
            raw = original_llm(prompt, system=system, model=model,
                                require_json=require_json, temperature=temperature, **kw)
        except Exception as e:
            entry.update(raw_response=None, error=str(e), latency_seconds=round(time.monotonic() - start, 3))
            llm_calls.append(entry)
            raise
        entry.update(raw_response=raw, error=None, latency_seconds=round(time.monotonic() - start, 3))
        llm_calls.append(entry)
        return raw

    engine.llm = _logging_llm


def _build_cluster_profile(eng_profile, role_clusters, idx):
    """Mirrors the construction at engine.py's per-cluster gate+rank+judge call
    sites exactly: a per-cluster copy of the profile with search_terms swapped to
    just that cluster's roles."""
    cluster_profile = dict(eng_profile)
    cluster_profile["search_terms"] = role_clusters[idx].get("roles") or eng_profile.get("search_terms")
    cluster_profile["_multi_cluster"] = len(role_clusters) > 1
    return cluster_profile


def _job_base_record(j):
    return {
        "identity": j.get("_identity"),
        "title": j.get("title"), "company": j.get("company") or "", "location": j.get("location") or "",
        "url": j.get("url") or "", "source": j.get("board") or "",
        "snippet": (j.get("snippet") or "")[:600],
        "full_text_len": len(j.get("full_text") or ""),
        "has_real_scrape": bool(j.get("_has_full_text")),
        "embed_score": round(j.get("embed_score", 0.0), 4),
    }


def _dry_run_screen(engine, cluster_profile, sample, llm_calls, call_ctx):
    call_ctx.update(stage="screen")
    listing_block = "\n".join(
        f"{i + 1}. {c['title']} @ {c.get('company', '')} | "
        f"{(c.get('location') or 'location unknown')}"
        f"{engine._listing_salary_suffix(c)} | "
        f"{(c.get('full_text') or c.get('snippet') or '')[:engine.GATE_LISTING_TEXT_CHARS]}"
        for i, c in enumerate(sample)
    )
    prompt = engine._screen_prompt(cluster_profile, listing_block)
    llm_calls.append({
        "stage": "screen", "cluster_idx": call_ctx.get("cluster_idx"), "cluster_label": call_ctx.get("cluster_label"),
        "prompt": prompt, "system": None, "model": engine.CHEAP_MODEL, "temperature": 0,
        "raw_response": None, "error": None, "latency_seconds": None, "dry_run": True,
    })
    for c in sample:
        for axis in _AXIS_ORDER:
            c[f"_{axis}"] = True
        c["_sector_ambiguous"] = False
        c["_gate_reason"] = "DRY_RUN_PLACEHOLDER"
        c["_key_requirements"] = []


def _dry_run_rank(engine, cluster_profile, pool, llm_calls, call_ctx):
    call_ctx.update(stage="rank")
    listing_block = "\n".join(
        f"{i + 1}. {c['title']} @ {c.get('company', '')} | "
        f"{(c.get('location') or 'location unknown')} | "
        f"{'[gate note: role-function fit vs target roles was ambiguous, not a confirmed match] ' if c.get('_sector_ambiguous') else ''}"
        f"{(c.get('full_text') or c.get('snippet') or '')[:engine.RANK_LISTING_TEXT_CHARS]}"
        for i, c in enumerate(pool)
    )
    prompt = engine._rank_prompt(cluster_profile, listing_block)
    llm_calls.append({
        "stage": "rank", "cluster_idx": call_ctx.get("cluster_idx"), "cluster_label": call_ctx.get("cluster_label"),
        "prompt": prompt, "system": None, "model": engine.MID_MODEL, "temperature": 0,
        "raw_response": None, "error": None, "latency_seconds": None, "dry_run": True,
    })
    for c in pool:
        c["_rank_score"] = 50.0


def _dry_run_judge(engine, judge_candidates, cv_text, llm_calls, call_ctx):
    """Reconstructs _run_final_eval's inline prompt string exactly (there is no
    standalone prompt-builder function for this stage to call directly, unlike
    _screen_prompt/_rank_prompt)."""
    call_ctx.update(stage="judge")
    cv_text_trunc = cv_text[:5000]
    jobs_block = "\n\n---\n\n".join(
        engine._final_eval_job_block(i, j) for i, j in enumerate(judge_candidates)
    )
    prompt = f"""Candidate Background Profile:
{cv_text_trunc}

Judge the {len(judge_candidates)} complete job postings below. Return up to {engine.FINAL_PICKS} genuinely strong fits in
"strong" (best first), and up to 3 least-bad disqualifier-only survivors in "backup" (best first; empty
if "strong" already covers it or nothing qualifies). For any job you hard-exclude from both lists via a
DISQUALIFIERS rule, add it to "disqualified" with a short reason.

Jobs Payload:
{jobs_block}"""
    llm_calls.append({
        "stage": "judge", "cluster_idx": call_ctx.get("cluster_idx"), "cluster_label": call_ctx.get("cluster_label"),
        "prompt": prompt, "system": engine._FINAL_EVAL_SYSTEM, "model": engine.EXP_MODEL, "temperature": 0.2,
        "raw_response": None, "error": None, "latency_seconds": None, "dry_run": True,
    })
    return [], [], []


def _process_cluster(engine, engine_svc, idx, cluster, role_clusters, eng_profile, cv_text_base,
                      jobs_for_cluster, args, llm_calls, call_ctx):
    label = engine_svc._cluster_label(cluster)
    cluster_profile = _build_cluster_profile(eng_profile, role_clusters, idx)
    soft_axes, hard_axes = engine_svc._hard_enforced_axes(engine, cluster_profile)
    call_ctx.update(cluster_idx=idx, cluster_label=label)

    gate_n = min(args.gate_sample_per_cluster, engine._GATE_BATCH)
    sample = jobs_for_cluster[:gate_n]
    journey = {j.get("_identity") or j.get("url"): _job_base_record(j) for j in sample}

    result = {
        "cluster_idx": idx, "label": label, "roles": cluster.get("roles"),
        "cluster_profile_search_terms": cluster_profile.get("search_terms"),
        "hard_axes_promoted": list(hard_axes), "soft_axes": list(soft_axes),
        "n_available_in_cluster": len(jobs_for_cluster), "n_sampled": len(sample),
    }
    if not sample:
        result["warning"] = "no cached, embedded listings available for this cluster -- nothing to sample."
        result["journey"] = []
        return result

    # ── Stage 1: WEAK GATE (screen_gate, CHEAP_MODEL) ───────────────────────────
    if args.dry_run:
        _dry_run_screen(engine, cluster_profile, sample, llm_calls, call_ctx)
    else:
        call_ctx.update(stage="screen")
        engine.screen_gate(sample, cluster_profile)

    def _clears_hard(j):
        return (j.get("_hard_gate_ok", True) and j.get("_listing_ok", True)
                and all(j.get(a, True) for a in hard_axes))

    hard_dropped = [j for j in sample if not _clears_hard(j)]
    remaining = [j for j in sample if _clears_hard(j)]
    off_sector = [j for j in remaining if not j.get("_sector_ok", True)]
    in_sector = [j for j in remaining if j.get("_sector_ok", True)]
    soft_fail_counts = [sum(1 for a in soft_axes if not j.get(a, True)) for j in in_sector]
    threshold = engine.dynamic_hard_drop_threshold(soft_fail_counts)
    survivors = [j for j, f in zip(in_sector, soft_fail_counts) if f < threshold]
    soft_dropped = [j for j, f in zip(in_sector, soft_fail_counts) if f >= threshold]

    for j in hard_dropped:
        j["_bucket"] = "dropped_hard_filter"
    for j in off_sector:
        j["_bucket"] = "dropped_off_sector"
    for j in soft_dropped:
        j["_bucket"] = "dropped_soft_threshold"
    for j in survivors:
        j["_bucket"] = "survivor"

    for j in sample:
        key = j.get("_identity") or j.get("url")
        axes = {a: j.get(f"_{a}") for a in _AXIS_ORDER}
        journey[key].update({
            "gate_axes": axes,
            "gate_reason_raw": j.get("_gate_reason"),
            "gate_reason_decoded": _decode_reason(j.get("_gate_reason")),
            "gate_key_requirements": j.get("_key_requirements") or [],
            "gate_bucket": j.get("_bucket"),
            "gate_soft_fail_count": j.get("_soft_fail_count"),
        })
    for j, f in zip(in_sector, soft_fail_counts):
        journey[j.get("_identity") or j.get("url")]["gate_soft_fail_count"] = f

    result["gate_threshold_this_round"] = threshold
    result["gate_bucket_counts"] = {
        "survivor": len(survivors), "dropped_hard_filter": len(hard_dropped),
        "dropped_off_sector": len(off_sector), "dropped_soft_threshold": len(soft_dropped),
    }

    # ── Stage 2: MEDIUM SCORER (rank_gate, MID_MODEL) ───────────────────────────
    rank_pool = survivors or in_sector or sample
    rank_pool_is_fallback = not survivors
    result["rank_pool_is_fallback"] = rank_pool_is_fallback
    if rank_pool_is_fallback:
        result["rank_pool_fallback_note"] = (
            "no gate survivor in this sample -- ranking the next-best available group anyway so the "
            "medium-scorer stage still has real output to show; in production a cluster this thin would "
            "instead rely on the gate's own MIN_RESULTS backfill (not replicated here, see "
            "_gate_rank_refill_cluster in engine.py)."
        )

    if args.dry_run:
        _dry_run_rank(engine, cluster_profile, rank_pool, llm_calls, call_ctx)
    else:
        call_ctx.update(stage="rank")
        engine.rank_gate(rank_pool, cluster_profile)

    sent_to_rank_ids = {j.get("_identity") or j.get("url") for j in rank_pool}
    for j in rank_pool:
        key = j.get("_identity") or j.get("url")
        journey[key].update({
            "sent_to_rank": True,
            "rank_score": round(j.get("_rank_score", 50.0), 1),
            "rank_gate_failed": bool(j.get("_rank_gate_failed")),
            "clears_rank_reject_floor": j.get("_rank_score", 50.0) >= engine_svc.RANK_REJECT_SCORE_FLOOR,
        })
    for j in sample:
        key = j.get("_identity") or j.get("url")
        if key not in sent_to_rank_ids:
            journey[key]["sent_to_rank"] = False

    scores = [j.get("_rank_score", 50.0) for j in rank_pool]
    result["rank_stats"] = {
        "n": len(scores),
        "min": round(min(scores), 1) if scores else None,
        "max": round(max(scores), 1) if scores else None,
        "avg": round(sum(scores) / len(scores), 1) if scores else None,
    } if scores else None

    # ── Stage 3: STRONG JUDGE (final_evaluation_split, EXP_MODEL) ───────────────
    judge_n = max(0, args.judge_sample_per_cluster)
    judge_candidates = sorted(
        rank_pool, key=lambda j: (j.get("_rank_score", 50.0), len(j.get("full_text") or "")), reverse=True
    )[:judge_n]

    cluster_roles = cluster.get("roles") or []
    from app.services.snapshot import cv_text_for_cluster
    cv_text = cv_text_for_cluster(cv_text_base, cluster_roles) if cluster_roles else cv_text_base
    result["cv_text_used"] = cv_text

    if not judge_candidates:
        result["warning"] = (result.get("warning", "") + " no candidates available for the judge stage."
                              ).strip()
        result["journey"] = list(journey.values())
        return result

    if args.dry_run:
        strong, backup, disqualified = _dry_run_judge(engine, judge_candidates, cv_text, llm_calls, call_ctx)
    else:
        call_ctx.update(stage="judge")
        strong, backup, disqualified = engine.final_evaluation_split(
            judge_candidates, cluster_profile, cv_text=cv_text
        )
        strong, backup, disqualified = strong or [], backup or [], disqualified or []

    judge_ids = {j.get("_identity") or j.get("url") for j in judge_candidates}
    verdict_by_id = {}
    for entry in strong:
        verdict_by_id[entry.get("_identity") or entry.get("url")] = ("strong", entry)
    for entry in backup:
        verdict_by_id[entry.get("_identity") or entry.get("url")] = ("backup", entry)
    for entry in disqualified:
        verdict_by_id[entry.get("_identity") or entry.get("url")] = ("disqualified", entry)

    for j in judge_candidates:
        key = j.get("_identity") or j.get("url")
        journey[key]["sent_to_judge"] = True
        verdict, entry = verdict_by_id.get(key, (None, None))
        if verdict is None:
            journey[key]["judge_verdict"] = "not_selected"
            journey[key]["judge_detail"] = None
        elif verdict == "disqualified":
            journey[key]["judge_verdict"] = "disqualified"
            journey[key]["judge_detail"] = {"reason": entry.get("reason")}
        else:
            journey[key]["judge_verdict"] = verdict
            journey[key]["judge_detail"] = {
                "summary": entry.get("summary"), "fit_level": entry.get("fit_level"),
                "role_type": entry.get("role_type"), "can_do_fit": entry.get("can_do_fit"),
                "filters_on": entry.get("filters_on"), "highlight": entry.get("highlight"),
                "requirements": entry.get("requirements"), "concerns": entry.get("concerns"),
                "role_salary": entry.get("role_salary"), "work_style": entry.get("work_style"),
                "role_seniority": entry.get("role_seniority"), "deadline": entry.get("deadline"),
                "scam_suspect": entry.get("scam_suspect"),
            }
    for j in sample:
        key = j.get("_identity") or j.get("url")
        if key not in judge_ids:
            journey[key].setdefault("sent_to_judge", False)

    result["judge_stats"] = {
        "n_sent": len(judge_candidates), "n_strong": len(strong), "n_backup": len(backup),
        "n_disqualified": len(disqualified),
        "n_not_selected": len(judge_candidates) - len(strong) - len(backup) - len(disqualified),
    }
    result["journey"] = list(journey.values())
    return result


def render_html_report(report) -> str:
    report_json = json.dumps(report).replace("</", "<\\/")
    dry = report["dry_run"]
    banner_class = "dry" if dry else "live"
    banner_text = (
        "DRY RUN -- placeholder verdicts, no live API calls were made."
        if dry else
        f"LIVE RUN -- real calls made to {report['models']['cheap']} (weak gate), "
        f"{report['models']['mid']} (medium scorer), and {report['models']['judge']} (strong judge)."
    )
    html = _HTML_SHELL
    html = html.replace("__TITLE__", f"Pipeline Prompt Audit - {report['profile_name']}")
    html = html.replace("__CSS__", _CSS)
    html = html.replace("__BANNER__", f'<div class="banner {banner_class}">{banner_text}</div>')
    html = html.replace("__REPORT_JSON__", report_json)
    html = html.replace("__JS__", _JS)
    return html


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; margin: 0; padding: 24px;
       background: #0b0d10; color: #e6e6e6; }
@media (prefers-color-scheme: light) { body { background: #f7f7f8; color: #16181d; } }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 17px; margin-top: 26px; }
h3 { font-size: 14px; margin-top: 16px; }
.banner { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.banner.dry { background: #3a3410; color: #f2d675; }
.banner.live { background: #1a3a2e; color: #7be0a8; }
.banner.warn { background: #3a1a1a; color: #f28b8b; }
.stat-row { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
.stat-tile { background: rgba(127,127,127,0.12); border-radius: 10px; padding: 10px 16px; min-width: 120px; }
.stat-tile .num { font-size: 20px; font-weight: 700; }
.stat-tile .label { font-size: 11px; opacity: 0.75; }
details { margin: 6px 0; border: 1px solid rgba(127,127,127,0.25); border-radius: 8px; padding: 6px 12px; }
summary { cursor: pointer; font-weight: 600; font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 8px; }
th, td { border-bottom: 1px solid rgba(127,127,127,0.2); padding: 5px 7px; text-align: left; vertical-align: top; }
th { cursor: pointer; user-select: none; position: sticky; top: 0; background: inherit; }
.chip { display: inline-block; width: 16px; text-align: center; border-radius: 4px; margin-right: 2px;
        font-size: 10px; font-family: monospace; }
.chip.ok { background: #1a3a2e; color: #7be0a8; }
.chip.bad { background: #3a1a1a; color: #f28b8b; }
.badge { padding: 2px 7px; border-radius: 6px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.badge.survivor, .badge.strong { background: #1a3a2e; color: #7be0a8; }
.badge.dropped_hard_filter, .badge.dropped_off_sector, .badge.disqualified { background: #3a1a1a; color: #f28b8b; }
.badge.dropped_soft_threshold, .badge.backup { background: #3a2f10; color: #f2c675; }
.badge.not_selected, .badge.na { background: rgba(127,127,127,0.18); color: inherit; opacity: 0.8; }
.tabs { display: flex; gap: 6px; flex-wrap: wrap; margin: 12px 0; }
.tab-btn { padding: 6px 14px; border-radius: 8px; border: 1px solid rgba(127,127,127,0.35); background: transparent;
           color: inherit; cursor: pointer; font-size: 12.5px; }
.tab-btn.active { background: #4c8bf5; border-color: #4c8bf5; color: #fff; }
.cluster-panel { display: none; }
.cluster-panel.active { display: block; }
.stage-section { border: 1px solid rgba(127,127,127,0.25); border-radius: 10px; padding: 12px 16px; margin: 14px 0; }
.stage-section h3 { margin-top: 0; }
tr.job-row { cursor: pointer; }
tr.job-row:hover { background: rgba(127,127,127,0.08); }
.row-detail { display: none; }
.row-detail.open { display: table-row; }
.row-detail td { background: rgba(127,127,127,0.06); }
pre { white-space: pre-wrap; word-break: break-word; font-size: 11.5px; max-height: 480px; overflow-y: auto;
      background: rgba(127,127,127,0.08); padding: 8px; border-radius: 6px; }
a { color: #7bb0f2; }
code { font-size: 11.5px; }
.field-flow { font-size: 12px; margin: 3px 0; padding: 4px 8px; border-left: 3px solid #4c8bf5;
              background: rgba(76,139,245,0.08); }
.field-flow b { font-family: monospace; }
.concern { margin: 2px 0; }
.small { font-size: 11.5px; opacity: 0.8; }
"""

_JS = """
function esc(s) {
  return String(s === undefined || s === null ? "" : s).replace(/[&<>"']/g, function(m) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m];
  });
}
function axisChips(axes) {
  var labels = {sector_ok:"Sec", hard_gate_ok:"Hrd", listing_ok:"Lst", seniority_ok:"Sen", requirements_ok:"Req",
                skills_ok:"Skl", salary_ok:"Sal", work_arrangement_ok:"Wrk"};
  var order = ["sector_ok","hard_gate_ok","listing_ok","seniority_ok","requirements_ok","skills_ok","salary_ok","work_arrangement_ok"];
  var html = "";
  for (var i = 0; i < order.length; i++) {
    var k = order[i];
    html += '<span class="chip ' + (axes[k] ? "ok" : "bad") + '" title="' + k + '">' + labels[k] + '</span>';
  }
  return html;
}
function badge(text, cls) { return '<span class="badge ' + esc(cls || text) + '">' + esc(text) + '</span>'; }

function renderFormation(r) {
  var html = '';
  r.profile_formation.forEach(function(f) {
    if (f.value === null || f.value === undefined || f.value === '' ||
        (Array.isArray(f.value) && f.value.length === 0)) return;
    html += '<div class="field-flow"><b>' + esc(f.field) + '</b>: ' + esc(JSON.stringify(f.value)) +
            '<div class="small">&rarr; ' + esc(f.drives) + '</div></div>';
  });
  document.getElementById('formation').innerHTML = html;
  var cvHtml = '';
  r.clusters.forEach(function(c) {
    cvHtml += '<details><summary>cv_text for [' + esc(c.label) + ']</summary><pre>' + esc(c.cv_text_used) + '</pre></details>';
  });
  document.getElementById('cvTexts').innerHTML = cvHtml;
}

function renderTabs(r) {
  var tabs = document.getElementById('tabs');
  var panels = document.getElementById('panels');
  tabs.innerHTML = '';
  panels.innerHTML = '';
  r.clusters.forEach(function(c, i) {
    var btn = document.createElement('button');
    btn.className = 'tab-btn' + (i === 0 ? ' active' : '');
    btn.textContent = c.label + ' (' + c.n_sampled + ' sampled)';
    btn.addEventListener('click', function() {
      document.querySelectorAll('.tab-btn').forEach(function(b) { b.classList.remove('active'); });
      document.querySelectorAll('.cluster-panel').forEach(function(p) { p.classList.remove('active'); });
      btn.classList.add('active');
      document.getElementById('panel-' + i).classList.add('active');
    });
    tabs.appendChild(btn);
    panels.appendChild(renderClusterPanel(c, i, r));
  });
}

function llmCallsFor(r, clusterIdx, stage) {
  return r.llm_calls.filter(function(c) { return c.cluster_idx === clusterIdx && c.stage === stage; });
}

function renderPromptBlock(call, idx) {
  var meta = call.model + ', temp=' + call.temperature +
    (call.latency_seconds !== null && call.latency_seconds !== undefined ? ', ' + call.latency_seconds + 's' : '') +
    (call.dry_run ? ' [DRY RUN]' : '') + (call.error ? ' [ERROR]' : '');
  var html = '<details' + (idx === 0 ? ' open' : '') + '><summary>Call ' + (idx + 1) + ' -- ' + esc(meta) + '</summary>';
  if (call.system) html += '<div><b>System prompt:</b><pre>' + esc(call.system) + '</pre></div>';
  html += '<div><b>User prompt:</b><pre>' + esc(call.prompt) + '</pre></div>';
  if (call.error) {
    html += '<div><b>Error:</b><pre>' + esc(call.error) + '</pre></div>';
  } else if (call.dry_run) {
    html += '<div class="small"><i>Dry run -- no API call made, no response.</i></div>';
  } else {
    html += '<div><b>Raw response:</b><pre>' + esc(call.raw_response) + '</pre></div>';
  }
  html += '</details>';
  return html;
}

function renderClusterPanel(c, i, r) {
  var div = document.createElement('div');
  div.className = 'cluster-panel' + (i === 0 ? ' active' : '');
  div.id = 'panel-' + i;
  var html = '<p class="small">Target roles: <code>' + esc((c.roles||[]).join(', ')) + '</code> | ' +
    'hard-enforced axes: <code>' + esc((c.hard_axes_promoted||[]).join(', ') || 'none') + '</code></p>';
  if (c.warning) html += '<div class="banner warn">' + esc(c.warning) + '</div>';

  if (c.gate_bucket_counts) {
    html += '<div class="stat-row">';
    ['survivor','dropped_hard_filter','dropped_off_sector','dropped_soft_threshold'].forEach(function(b) {
      html += '<div class="stat-tile"><div class="num">' + (c.gate_bucket_counts[b]||0) + '</div><div class="label">' + b + '</div></div>';
    });
    html += '</div>';
  }

  html += '<h3>Job journey (' + (c.journey||[]).length + ' real listing(s) traced through all 3 stages)</h3>';
  html += '<table><thead><tr><th>Title / Company</th><th>Embed</th><th>Weak gate</th><th>Medium score</th><th>Strong judge</th></tr></thead><tbody id="journey-' + i + '"></tbody></table>';

  html += '<div class="stage-section"><h3>Stage 1 -- Weak gate (screen_gate, ' + esc(r.models.cheap) + ')</h3>' +
    '<div id="screen-calls-' + i + '"></div></div>';
  html += '<div class="stage-section"><h3>Stage 2 -- Medium scorer (rank_gate, ' + esc(r.models.mid) + ')</h3>';
  if (c.rank_pool_is_fallback) html += '<div class="banner dry">' + esc(c.rank_pool_fallback_note) + '</div>';
  if (c.rank_stats) html += '<p class="small">score spread: min=' + c.rank_stats.min + ' max=' + c.rank_stats.max + ' avg=' + c.rank_stats.avg + ' (n=' + c.rank_stats.n + ')</p>';
  html += '<div id="rank-calls-' + i + '"></div></div>';
  html += '<div class="stage-section"><h3>Stage 3 -- Strong judge (final_evaluation_split, ' + esc(r.models.judge) + ')</h3>';
  if (c.judge_stats) {
    html += '<div class="stat-row">' +
      '<div class="stat-tile"><div class="num">' + c.judge_stats.n_sent + '</div><div class="label">sent to judge</div></div>' +
      '<div class="stat-tile"><div class="num">' + c.judge_stats.n_strong + '</div><div class="label">strong</div></div>' +
      '<div class="stat-tile"><div class="num">' + c.judge_stats.n_backup + '</div><div class="label">backup</div></div>' +
      '<div class="stat-tile"><div class="num">' + c.judge_stats.n_disqualified + '</div><div class="label">disqualified</div></div>' +
      '<div class="stat-tile"><div class="num">' + c.judge_stats.n_not_selected + '</div><div class="label">not selected</div></div>' +
      '</div>';
  }
  html += '<details><summary>Static system prompt (identical for every judge call, every cluster -- shown once)</summary><pre>' + esc(r.final_eval_system_prompt) + '</pre></details>';
  html += '<div id="judge-calls-' + i + '"></div></div>';

  div.innerHTML = html;
  return div;
}

function jobDetailHtml(j) {
  var html = '<div><b>URL:</b> <a href="' + esc(j.url) + '" target="_blank" rel="noopener">' + esc(j.url) + '</a></div>';
  html += '<div class="small" style="margin-top:4px">' + esc(j.source) + ' | full_text length: ' + j.full_text_len +
    (j.has_real_scrape ? ' (real page scrape)' : ' (snippet only, no separate scrape)') + '</div>';
  html += '<div style="margin-top:6px"><b>Snippet:</b> ' + esc(j.snippet) + '</div>';
  if (j.gate_reason_decoded) {
    html += '<div style="margin-top:6px"><b>Gate axes:</b> ' + axisChips(j.gate_axes) + ' reason: ' + esc(JSON.stringify(j.gate_reason_decoded)) + '</div>';
  }
  if (j.gate_key_requirements && j.gate_key_requirements.length) {
    html += '<div style="margin-top:4px"><b>Gate-extracted key requirements:</b> ' + esc(JSON.stringify(j.gate_key_requirements)) + '</div>';
  }
  if (j.judge_detail) {
    var d = j.judge_detail;
    if (d.reason) {
      html += '<div style="margin-top:6px"><b>Disqualified reason:</b> ' + esc(d.reason) + '</div>';
    } else {
      html += '<div style="margin-top:6px"><b>Summary:</b> ' + esc(d.summary) + '</div>';
      html += '<div><b>Role type:</b> ' + esc(d.role_type) + '</div>';
      html += '<div><b>Can-do fit:</b> ' + esc(d.can_do_fit) + '</div>';
      html += '<div><b>Likely filters on:</b> ' + esc((d.filters_on || []).join(', ')) + '</div>';
      html += '<div><b>Highlight when applying:</b> ' + esc(d.highlight) + '</div>';
      html += '<div><b>Facts:</b> salary=' + esc(d.role_salary) + ', work_style=' + esc(d.work_style) +
        ', seniority=' + esc(d.role_seniority) + ', deadline=' + esc(d.deadline) +
        ', scam_suspect=' + esc(d.scam_suspect) + '</div>';
      if (d.requirements && d.requirements.length) {
        html += '<div style="margin-top:4px"><b>Requirements checklist:</b><ul>';
        d.requirements.forEach(function(rq) {
          html += '<li>[' + (rq.met ? 'MET' : 'GAP') + ', ' + rq.category + '] ' + esc(rq.text) + '</li>';
        });
        html += '</ul></div>';
      }
      if (d.concerns && d.concerns.length) {
        html += '<div style="margin-top:4px"><b>Concerns:</b>';
        d.concerns.forEach(function(cn) { html += '<div class="concern">- ' + esc(cn) + '</div>'; });
        html += '</div>';
      }
    }
  }
  return html;
}

function renderJourney(c, i) {
  var body = document.getElementById('journey-' + i);
  body.innerHTML = '';
  (c.journey || []).forEach(function(j) {
    var tr = document.createElement('tr');
    tr.className = 'job-row';
    var gateCell = j.gate_bucket ? badge(j.gate_bucket) + ' ' + axisChips(j.gate_axes || {}) : '<span class="small">n/a</span>';
    var rankCell = j.sent_to_rank ? (j.rank_score + (j.clears_rank_reject_floor ? '' : ' <span class="small">(below floor)</span>')) : '<span class="badge na">not ranked</span>';
    var judgeCell = j.sent_to_judge ? badge(j.judge_verdict || 'n/a') : '<span class="badge na">not sent</span>';
    tr.innerHTML = '<td>' + esc(j.title) + '<div class="small">' + esc(j.company) + '</div></td>' +
      '<td>' + j.embed_score.toFixed(3) + '</td>' +
      '<td>' + gateCell + '</td>' +
      '<td>' + rankCell + '</td>' +
      '<td>' + judgeCell + '</td>';
    var detailRow = document.createElement('tr');
    detailRow.className = 'row-detail';
    detailRow.innerHTML = '<td colspan="5">' + jobDetailHtml(j) + '</td>';
    tr.addEventListener('click', function() { detailRow.classList.toggle('open'); });
    body.appendChild(tr);
    body.appendChild(detailRow);
  });
}

document.addEventListener('DOMContentLoaded', function() {
  renderFormation(REPORT);
  renderTabs(REPORT);
  REPORT.clusters.forEach(function(c, i) {
    renderJourney(c, i);
    var screenCalls = llmCallsFor(REPORT, c.cluster_idx, 'screen');
    var rankCalls = llmCallsFor(REPORT, c.cluster_idx, 'rank');
    var judgeCalls = llmCallsFor(REPORT, c.cluster_idx, 'judge');
    document.getElementById('screen-calls-' + i).innerHTML = screenCalls.map(renderPromptBlock).join('') || '<p class="small">no call made (empty sample)</p>';
    document.getElementById('rank-calls-' + i).innerHTML = rankCalls.map(renderPromptBlock).join('') || '<p class="small">no call made (empty pool)</p>';
    document.getElementById('judge-calls-' + i).innerHTML = judgeCalls.map(renderPromptBlock).join('') || '<p class="small">no call made (empty judge sample)</p>';
  });
});
"""

_HTML_SHELL = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
  <h1>Pipeline Prompt Audit</h1>
  <div>__BANNER__</div>
  <h2>Profile &amp; CV formation (what feeds each stage's prompt)</h2>
  <div id="formation"></div>
  <div id="cvTexts"></div>
  <h2>Role clusters</h2>
  <div class="tabs" id="tabs"></div>
  <div id="panels"></div>
<script>
const REPORT = __REPORT_JSON__;
__JS__
</script>
</body>
</html>
"""


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    args.gate_sample_per_cluster = max(1, args.gate_sample_per_cluster)

    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))

    import app.database as database
    import app.models as models
    from app.services import snapshot
    from app.services import engine as engine_svc
    import full_auto as engine  # noqa: E402 -- after app.* imports so .env is loaded

    db = database.SessionLocal()
    try:
        profile = _resolve_profile(db, models, args.profile_id)
        print(f"[prompt_pipeline] profile {profile.id} ({profile.name!r})")

        print("[prompt_pipeline] building profile snapshot (small real API cost: region-inference "
              "+ role-clustering, same as gate_harness.py/analyze_rank_gate.py already accept)...")
        snap = snapshot.build_snapshot(db, profile.id)
        role_clusters = snap["role_clusters"]
        eng_profile = snap["engine_profile"]
        cv_text_base = snap["cv_text_base"]
        if not role_clusters:
            raise SystemExit("profile has no target-role clusters -- add at least one target_role "
                              "attribute first.")
        print(f"[prompt_pipeline] {len(role_clusters)} role cluster(s): "
              + "; ".join(f"{c['label']}={c['roles']}" for c in role_clusters))

        if args.cluster:
            needle = args.cluster.lower()
            matches = [i for i, c in enumerate(role_clusters) if needle in (c.get("label") or "").lower()]
            if not matches:
                available = [c.get("label") for c in role_clusters]
                raise SystemExit(f"--cluster {args.cluster!r} matched no cluster; available: {available}")
        else:
            matches = list(range(len(role_clusters)))

        cluster_texts = [c["weighted_text"] for c in role_clusters]
        cluster_embeddings = engine.get_embeddings_batch(cluster_texts)
        print(f"[prompt_pipeline] embedded {len(cluster_texts)} cluster text(s) via {engine.EMBED_MODEL}")

        rows = (
            db.query(models.JobSeen)
            .filter(models.JobSeen.profile_id == profile.id, models.JobSeen.dead_reason.is_(None))
            .all()
        )
        rows_with_embedding = [r for r in rows if r.embedding]
        print(f"[prompt_pipeline] {len(rows_with_embedding)}/{len(rows)} non-dead jobs_seen rows have a "
              f"cached embedding")
        if not rows_with_embedding:
            raise SystemExit("no cached, embedded jobs_seen rows for this profile -- run a real search "
                              "at least once first so there's something real to sample.")

        scored = engine_svc._score_rows(rows_with_embedding, cluster_embeddings)
        by_cluster = defaultdict(list)
        for d in scored:
            by_cluster[d["_cluster"]].append(d)

        est_calls = 3 * len(matches)
        if args.dry_run:
            print("[prompt_pipeline] --dry-run: building prompts only, NO live screen_gate/rank_gate/"
                  "final_evaluation_split/llm() calls beyond build_snapshot's own small one-off call.")
        else:
            print(f"[prompt_pipeline] LIVE RUN: about to make up to ~{est_calls} real call(s) across "
                  f"{engine.CHEAP_MODEL} (weak gate), {engine.MID_MODEL} (medium scorer), and "
                  f"{engine.EXP_MODEL} (strong judge, the most expensive tier). This spends real API "
                  f"credits, but is bounded -- one call per stage per cluster, not a full search.")

        llm_calls: list = []
        call_ctx: dict = {}
        if not args.dry_run:
            _install_capture_hooks(engine, llm_calls, call_ctx)

        clusters_report = []
        for idx in matches:
            cluster = role_clusters[idx]
            jobs_for_cluster = by_cluster.get(idx, [])
            print(f"\n[{cluster['label']}] {len(jobs_for_cluster)} cached listing(s) available, "
                  f"sampling top {min(args.gate_sample_per_cluster, engine._GATE_BATCH)} by embed_score")
            cluster_result = _process_cluster(
                engine, engine_svc, idx, cluster, role_clusters, eng_profile, cv_text_base,
                jobs_for_cluster, args, llm_calls, call_ctx,
            )
            clusters_report.append(cluster_result)
            if cluster_result.get("judge_stats"):
                js = cluster_result["judge_stats"]
                print(f"  gate: {cluster_result.get('gate_bucket_counts')}")
                print(f"  rank: {cluster_result.get('rank_stats')}")
                print(f"  judge: {js}")

        profile_formation = [
            {"field": k, "value": eng_profile.get(k), "drives": drives}
            for k, drives in _FIELD_DRIVES.items()
        ] + [
            {"field": k, "value": getattr(profile, k, None), "drives": drives}
            for k, drives in _JUDGE_ONLY_FIELD_DRIVES.items()
        ]

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile.id, "profile_name": profile.name,
            "dry_run": args.dry_run,
            "models": {"cheap": engine.CHEAP_MODEL, "mid": engine.MID_MODEL, "judge": engine.EXP_MODEL},
            "final_eval_prompt_version": engine.FINAL_EVAL_PROMPT_VERSION,
            "thresholds": {
                "RELEVANCE_PRIMARY": engine_svc.RELEVANCE_PRIMARY, "RELEVANCE_FLOOR": engine_svc.RELEVANCE_FLOOR,
                "RANK_REJECT_SCORE_FLOOR": engine_svc.RANK_REJECT_SCORE_FLOOR,
                "MIN_RESULTS": engine_svc.MIN_RESULTS, "TARGET_POOL": engine_svc.TARGET_POOL,
                "JUDGE_POOL": engine_svc.JUDGE_POOL, "FINAL_PICKS": engine.FINAL_PICKS,
            },
            "profile_formation": profile_formation,
            "final_eval_system_prompt": engine._FINAL_EVAL_SYSTEM,
            "clusters": clusters_report,
            "llm_calls": llm_calls,
            "warnings": [
                "Judge stage sends its top rank-scored N regardless of RANK_REJECT_SCORE_FLOOR, to "
                "guarantee real prompt/output data for that stage even on a small sample -- each job's "
                "record says whether it would really clear the floor in production.",
                "full_text is whatever this job's cached full_text is (a genuine prior Phase-5 scrape) "
                "or the discovery-time snippet as fallback, exactly like production's _rows_to_dicts -- "
                "this harness does not re-scrape any page.",
                "Sample is the current top embed-ranked cached listings per cluster (same convention as "
                "analyze_rank_gate.py), not a random cross-section.",
                "gate_cache is bypassed (read+write) so every sampled job gets a fresh judgment and the "
                "shared production cache is left untouched, same as gate_harness.py.",
            ],
        }

        out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "tests" / "prompt_pipeline_reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
        json_path = out_dir / f"report_{stamp}.json"
        html_path = out_dir / f"report_{stamp}.html"
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        html_path.write_text(render_html_report(report), encoding="utf-8")

        print(f"\n[prompt_pipeline] wrote JSON report to {json_path}")
        print(f"[prompt_pipeline] wrote HTML report to {html_path} -- open it in a browser")
    finally:
        db.close()


if __name__ == "__main__":
    main()
