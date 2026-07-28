"""Read-only diagnostic harness for the screen_gate stage of the search pipeline
(see CLAUDE.md's "search pipeline" section, step 2 -- the merged 7-axis
sector+hard-filter+seniority+requirements+skills+salary+work-arrangement screen).

Pulls a random sample of real cached listings (JobSeen rows) for a profile, builds
the exact per-cluster profile screen_gate sees (the same construction
_run_engine_pipeline uses), and calls the REAL full_auto.screen_gate function live
against them -- capturing every prompt, every raw LLM response, and every per-job
decision, so you can (a) see exactly how your profile's attributes turn into the
gate's prompt text, and (b) eyeball how well the AI actually judges real listings.

This makes REAL, LIVE calls to CHEAP_MODEL (full_auto.CHEAP_MODEL) -- one per
20-listing batch, per screen_gate's own _GATE_BATCH. It bypasses screen_gate's
persistent gate_cache entirely (read AND write) so every run gets fresh judgments
and leaves the shared production cache untouched -- see _install_capture_hooks.
Nothing here calls rank_gate or the expensive final judge, and no Role/SearchRun
rows are created; no discovery or scraping happens either -- only cached JobSeen
rows already in the database are used.

Sampling is a PURE RANDOM sample of non-dead JobSeen rows for the profile -- unlike
a real search run, it is NOT pre-filtered to embed_score >= RELEVANCE_FLOOR, so
results here are broader/noisier than what a live run would actually feed the gate
(this is called out in the report's "warnings" list too).

GROUND-TRUTH MODE (--ground-truth) answers the question the plain report can't:
does the gate drop the right jobs? It samples only listings the expensive final
judge has ALREADY ruled on (JobSeen.eval_verdict), so every gate decision can be
scored against a real label -- reported as two separate rates, never one accuracy
number, because the two errors have opposite costs: dropping a job the judge liked
destroys a result the user never sees, while keeping one the judge later rejects
merely wastes a rank/judge call.

--text-mode is what makes that measurement honest. In production the gate runs
BEFORE Phase 5 scraping, so for most jobs it reads only a ~455-char API teaser --
but any job with a judge verdict is by definition one that got scraped, so its row
now carries a full_text the gate never had. Running the same sample twice:
    --text-mode snippet   what the gate really had     (production reality)
    --text-mode full      what the gate could do       (if text were supplied)
separates a MISCALIBRATED gate (misses the bar in both modes -- fix the prompt)
from a STARVED one (catches it only with full text -- fix the text supply, e.g.
wider Reed-style per-job enrichment). Those two findings call for opposite work,
and neither number alone distinguishes them.

Usage:
    venv/Scripts/python tests/gate_harness.py --dry-run --sample-size 5
    venv/Scripts/python tests/gate_harness.py --sample-size 5 --seed 1
    venv/Scripts/python tests/gate_harness.py --profile-id 7 --sample-size 100
    venv/Scripts/python tests/gate_harness.py --ground-truth --text-mode snippet
    venv/Scripts/python tests/gate_harness.py --ground-truth --text-mode full

--dry-run builds and shows the real prompts with placeholder (all-true) verdicts
and makes NO screen_gate/llm() calls -- but build_snapshot's own small one-off
region-inference/role-clustering call still runs, same negligible cost
analyze_embedding_gate.py already accepts.

Writes a JSON report and a self-contained browsable HTML report to --out-dir
(default: tests/gate_reports/).
"""
import argparse
import json
import math
import random
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_AXIS_ORDER = ("sector_ok", "hard_gate_ok", "listing_ok", "seniority_ok", "requirements_ok",
               "skills_ok", "salary_ok", "work_arrangement_ok")

_FIELD_DRIVES = {
    "sectors": "SECTOR/DOMAIN FIT block ('Candidate target sectors/domains') -> sector_ok (unconditional drop)",
    "search_terms": "SECTOR/DOMAIN FIT block ('Candidate target roles') -> sector_ok (unconditional drop)",
    "target_role_weight_tiers": "annotates target roles in the SECTOR/DOMAIN FIT block with "
                                 "tick/cross-derived priority labels (e.g. 'strongly preferred', 'deprioritize')",
    "avoid": "CANDIDATE HARD FILTERS block ('Will REJECT a role that involves...') -> "
             "hard_gate_ok (unconditional drop)",
    "must_have": "CANDIDATE HARD FILTERS block ('REQUIRES a role to satisfy...') -> "
                 "hard_gate_ok (unconditional drop)",
    "seniority": "SENIORITY/EXPERIENCE block -> seniority_ok",
    "key_skills": "SENIORITY/EXPERIENCE block (evidence check) AND CORE SKILLS OVERLAP block -> "
                  "seniority_ok, skills_ok",
    "skill_weight_tiers": "annotates core skills with tick/cross-derived priority labels",
    "skill_evidence_tiers": "annotates core skills with evidence-strength caveats "
                             "(e.g. 'familiar evidence only', 'self-directed evidence only')",
    "requirements": "CANDIDATE-SPECIFIC REQUIREMENTS block -> requirements_ok",
    "soft_must_have": "folded into CANDIDATE-SPECIFIC REQUIREMENTS block as a preference, "
                       "not a hard requirement -> requirements_ok",
    "soft_avoid": "folded into CANDIDATE-SPECIFIC REQUIREMENTS block as a dislike, "
                  "not a hard exclusion -> requirements_ok",
    "salary_floor": "SALARY FIT block -> salary_ok",
    "work_types": "WORK ARRANGEMENT block ('Candidate stated work-type preference') -> work_arrangement_ok",
    "location": "WORK ARRANGEMENT block ('Candidate location') -> work_arrangement_ok",
    "hard_axes": "promotes a normally-soft axis to an unconditional drop (adds NON-NEGOTIABLE wording "
                 "via _strict() in the prompt; engine._hard_enforced_axes moves it out of the "
                 "soft-failure count entirely)",
}

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
    "DRY_RUN_PLACEHOLDER": "dry run -- no real judgment was made",
}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile-id", type=int, default=None,
                    help="Defaults to the single active profile.")
    p.add_argument("--sample-size", type=int, default=100,
                    help="How many cached JobSeen rows to sample (default 100).")
    p.add_argument("--seed", type=int, default=None, help="Random seed for the sample (default: unseeded).")
    p.add_argument("--ground-truth", action="store_true",
                    help="Sample ONLY listings the expensive final judge has already ruled on "
                         "(JobSeen.eval_verdict set), so the gate's decisions can be scored against "
                         "a real label instead of eyeballed. Enables the agreement matrix.")
    p.add_argument("--text-mode", choices=("auto", "snippet", "full"), default="auto",
                    help="Which text screen_gate is allowed to read. 'auto' (default) = production's "
                         "own rule, full_text or snippet. 'snippet' = the API teaser only, which is "
                         "what the gate ACTUALLY had at gate time for most jobs (full_text is written "
                         "by Phase 5, after the gate). 'full' = the scraped posting. Run the same "
                         "--ground-truth sample under 'snippet' and 'full' to separate a "
                         "MISCALIBRATED gate from a STARVED one -- see the module docstring.")
    p.add_argument("--out-dir", default=None,
                    help="Output directory (default: tests/gate_reports/).")
    p.add_argument("--dry-run", action="store_true",
                    help="Build and display the real prompts with placeholder verdicts -- "
                         "NO screen_gate/llm() calls, no cost.")
    p.add_argument("--show-prompts", action="store_true",
                    help="Also print full prompts/raw responses to the console (always captured "
                         "in the JSON/HTML reports regardless of this flag).")
    p.add_argument("--quiet", action="store_true",
                    help="Suppress per-job console lines. screen_gate's own per-batch "
                         "[gate:screen] summary lines always print regardless -- not silenceable.")
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


def _sample_jobs(db, models, profile_id, sample_size, seed, ground_truth=False):
    """Pure-random sample of the profile's cached listings, or -- with
    ground_truth -- only those the expensive judge has already ruled on.

    The ground-truth sample is drawn BALANCED across the judge's verdicts rather
    than proportionally. Rejects outnumber strongs roughly 3:1 in a real store,
    and a proportional draw would make the "does the gate wrongly block good
    jobs?" half of the question rest on a handful of rows -- which is the half a
    gate change is most likely to break and the half a summary statistic hides.
    Both halves are reported separately for the same reason (see
    _ground_truth_stats), never as one blended accuracy number."""
    q = db.query(models.JobSeen).filter(
        models.JobSeen.profile_id == profile_id,
        models.JobSeen.dead_reason.is_(None),
    )
    if ground_truth:
        q = q.filter(models.JobSeen.eval_verdict.isnot(None))
    rows = q.all()
    if not rows:
        raise SystemExit(
            f"no {'judge-labelled ' if ground_truth else ''}non-dead JobSeen rows for profile "
            f"{profile_id} -- run a real search at least once first so there's something cached "
            "to sample from." + (" (--ground-truth additionally needs at least one run to have "
                                 "reached the final judge.)" if ground_truth else "")
        )
    n_available = len(rows)
    rng = random.Random(seed)
    if not ground_truth:
        return rng.sample(rows, min(sample_size, n_available)), n_available

    by_verdict = defaultdict(list)
    for r in rows:
        by_verdict[r.eval_verdict].append(r)
    # Round-robin across verdicts, so a scarce class is exhausted before a
    # plentiful one is capped rather than being crowded out proportionally.
    for group in by_verdict.values():
        rng.shuffle(group)
    sample, verdicts = [], sorted(by_verdict)
    while len(sample) < min(sample_size, n_available):
        took = False
        for v in verdicts:
            if by_verdict[v] and len(sample) < sample_size:
                sample.append(by_verdict[v].pop())
                took = True
        if not took:
            break
    return sample, n_available


def _build_snapshot_and_clusters(db, profile_id, snapshot_mod, engine):
    snap = snapshot_mod.build_snapshot(db, profile_id)
    role_clusters = snap["role_clusters"]
    eng_profile = snap["engine_profile"]
    cluster_texts = [c["weighted_text"] for c in role_clusters]
    cluster_embeddings = engine.get_embeddings_batch(cluster_texts) if cluster_texts else []
    return role_clusters, eng_profile, cluster_embeddings


def _score_sampled_rows(engine_svc, engine, rows, cluster_embeddings):
    """engine_svc._score_rows doesn't distinguish "no cached embedding" from a
    genuine 0.0 score -- both produce best_idx=0, best_score=0.0. Record which
    rows actually had an embedding first, then null out the fallback ones after,
    so the report doesn't misrepresent a missing embedding as a real low score."""
    had_embedding = {r.identity_hash: bool(r.embedding) for r in rows}
    scored = engine_svc._score_rows(rows, cluster_embeddings)
    for d in scored:
        if had_embedding.get(d.get("_identity"), False):
            d["_embed_fallback"] = False
        else:
            d["_embed_fallback"] = True
            d["embed_score"] = None
    return scored


def _apply_text_mode(engine, scored, text_mode):
    """Constrain what text screen_gate is allowed to read, and record what it
    actually got.

    This exists because sampling from JobSeen SYSTEMATICALLY FLATTERS THE GATE.
    In production the gate runs BEFORE Phase 5 scraping, so for most jobs it sees
    only the source API's ~500-char teaser (see CLAUDE.md's "Text supply" note:
    ~91% of a measured store averaged ~455 chars). But a job that reached the
    expensive judge is exactly a job that got scraped, so its row now carries a
    full_text the gate never had -- re-screening it in 'auto' mode hands the gate
    the full posting and measures a stage that doesn't exist.

    So the comparison, not either number alone, is the finding:
      --text-mode snippet -> what the gate really had. Misses here are the ones
                             production actually suffers.
      --text-mode full    -> what the gate could do if the text were supplied.
    A requirement the gate misses in BOTH modes is a calibration problem (the
    prompt saw the bar and waved it through). One it catches only in 'full' is a
    text-supply problem, and no amount of prompt tuning will fix it -- the fix is
    upstream (Reed-style per-job enrichment, a wider _enrich cap), which is a
    different and much cheaper change than re-tuning a prompt that was right all
    along.
    """
    for d in scored:
        snippet = d.get("snippet") or ""
        full = d.get("full_text") or ""
        # Stashed BEFORE the snippet mode blanks full_text below: the
        # "was the disqualifier only in the scraped text?" bucket needs the real
        # full text to compare against in EVERY mode, and reading it back off
        # d["full_text"] in snippet mode would find "" and silently misfile every
        # such job as "not found in any text" -- collapsing the starved-vs-
        # miscalibrated distinction this whole function exists to draw.
        d["_full_text_original"] = full
        if text_mode == "snippet":
            d["full_text"] = ""          # forces screen_gate's own `full_text or snippet` to fall back
            served = snippet
        else:
            served = full or snippet
        # The exact window screen_gate will read (its listing block truncates to
        # GATE_LISTING_TEXT_CHARS), kept so the "was the disqualifier even
        # visible?" check below tests the real text rather than the whole row.
        d["_gate_text_seen"] = served[:engine.GATE_LISTING_TEXT_CHARS]
        d["_gate_text_chars"] = len(d["_gate_text_seen"])
        d["_snippet_chars"] = len(snippet)
        d["_full_text_chars"] = len(full)
        d["_text_mode"] = text_mode


_QUOTE_RE = re.compile(r"[\"“”‘’']([^\"“”‘’']{12,})"
                       r"[\"“”‘’']")


def _normalize_for_match(s):
    """Collapse case, whitespace and the smart/straight quote and dash variants
    that differ between the judge's quoted reason and the source posting."""
    s = (s or "").lower()
    s = s.replace("’", "'").replace("‘", "'")
    s = s.replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-").replace("−", "-")
    return re.sub(r"\s+", " ", s).strip()


def _disqualifier_visibility(reason, gate_text, snippet, full_text):
    """Given the judge's disqualifier reason (which quotes the JD clause it
    relied on -- see full_auto's QUOTE-THEN-CLASSIFY rule), work out whether that
    clause was present in the text the gate was given.

    This is the whole point of the ground-truth mode. "The gate missed a 3-years
    bar" is not a finding on its own: if the bar was never in the gate's text,
    the gate was not wrong, it was blind, and the two call for opposite fixes.

    Returns None when the reason quotes nothing matchable (some reasons are
    paraphrases, and a scam/duplicate verdict quotes no requirement at all) --
    reported as its own bucket rather than silently scored either way.
    """
    quotes = _QUOTE_RE.findall(reason or "")
    if not quotes:
        return None
    # The longest quoted span is the requirement clause; shorter ones tend to be
    # a tool name that appears incidentally all over the posting.
    needle = _normalize_for_match(max(quotes, key=len))
    # Judge quotes are frequently truncated mid-phrase by the upstream
    # key_requirements cap (e.g. 'Advanced Excel (large datasets, statistical
    # functions, macro'), so a prefix probe beats requiring the whole span.
    probe = needle[:60]
    if len(probe) < 12:
        return None
    return {
        "quote": needle[:160],
        "in_gate_text": probe in _normalize_for_match(gate_text),
        "in_snippet": probe in _normalize_for_match(snippet),
        "in_full_text": probe in _normalize_for_match(full_text),
    }


def _install_capture_hooks(engine, llm_calls):
    """Bypasses screen_gate's persistent gate_cache (read AND write) so every
    sampled job gets a genuinely fresh LLM call every run, and wraps llm() to log
    the exact prompt/response of every call it makes from here on. Must be
    installed AFTER build_snapshot/get_embeddings_batch, which make their own
    unrelated LLM calls (region inference, role clustering) that shouldn't be
    conflated with the gate-call log this produces."""
    engine._gate_cache_lookup = lambda keys: {}
    engine._gate_cache_store = lambda entries: None

    original_llm = engine.llm
    default_model = engine.CHEAP_MODEL

    def _logging_llm(prompt, system="", model=default_model, require_json=False, temperature=0.2):
        start = time.monotonic()
        try:
            raw = original_llm(prompt, system=system, model=model,
                                require_json=require_json, temperature=temperature)
        except Exception as e:
            llm_calls.append({
                "prompt": prompt, "system": system, "model": model, "temperature": temperature,
                "raw_response": None, "error": str(e),
                "latency_seconds": round(time.monotonic() - start, 3), "dry_run": False,
            })
            raise
        llm_calls.append({
            "prompt": prompt, "system": system, "model": model, "temperature": temperature,
            "raw_response": raw, "error": None,
            "latency_seconds": round(time.monotonic() - start, 3), "dry_run": False,
        })
        return raw

    engine.llm = _logging_llm


def _build_cluster_profile(eng_profile, role_clusters, idx):
    """Mirrors the construction at engine.py's per-cluster gate+rank call site
    exactly: a per-cluster copy of the profile with search_terms swapped to just
    that cluster's roles."""
    cluster_profile = dict(eng_profile)
    cluster_profile["search_terms"] = role_clusters[idx].get("roles") or eng_profile.get("search_terms")
    cluster_profile["_multi_cluster"] = len(role_clusters) > 1
    return cluster_profile


def _annotate_profile_formation(eng_profile, role_clusters):
    fields = [{"field": k, "value": eng_profile.get(k), "drives": drives}
              for k, drives in _FIELD_DRIVES.items()]
    clusters = [
        {"label": c.get("label"), "roles": c.get("roles"), "weighted_text": c.get("weighted_text")}
        for c in role_clusters
    ]
    return {"fields": fields, "role_clusters": clusters}


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


def _dry_run_stand_in(engine, cluster_profile, batch, llm_calls):
    """Mirrors screen_gate's own listing_block/prompt construction (full_auto.py)
    without spending an API call -- records the real prompt text that WOULD be
    sent, and annotates every job with an obvious placeholder verdict so the
    rest of the harness's bucketing logic still runs end-to-end."""
    for start in range(0, len(batch), engine._GATE_BATCH):
        sub = batch[start:start + engine._GATE_BATCH]
        # Must mirror screen_gate's own listing block exactly, including the
        # `full_text or snippet` preference and the GATE_LISTING_TEXT_CHARS
        # window -- this previously used snippet[:450], which showed a --dry-run
        # reviewer a materially shorter prompt than the live call actually sends
        # and would have hidden exactly the text-supply question --text-mode
        # exists to answer.
        listing_block = "\n".join(
            f"{i + 1}. {c['title']} @ {c.get('company', '')} | "
            f"{(c.get('location') or 'location unknown')}"
            f"{engine._listing_salary_suffix(c)} | "
            f"{(c.get('full_text') or c.get('snippet') or '')[:engine.GATE_LISTING_TEXT_CHARS]}"
            for i, c in enumerate(sub)
        )
        prompt = engine._screen_prompt(cluster_profile, listing_block)
        llm_calls.append({
            "prompt": prompt, "system": None, "model": engine.CHEAP_MODEL, "temperature": 0,
            "raw_response": None, "error": None, "latency_seconds": None, "dry_run": True,
        })
        for c in sub:
            c["_sector_ok"] = True
            c["_sector_ambiguous"] = False
            c["_hard_gate_ok"] = True
            c["_listing_ok"] = True
            c["_seniority_ok"] = True
            c["_requirements_ok"] = True
            c["_skills_ok"] = True
            c["_salary_ok"] = True
            c["_work_arrangement_ok"] = True
            c["_gate_reason"] = "DRY_RUN_PLACEHOLDER"
            c["_key_requirements"] = []


def _run_cluster_rounds(engine, engine_svc, cluster_profile, cluster_idx, jobs, dry_run, llm_calls):
    """Mirrors _gate_rank_refill_cluster's real per-round bucketing logic
    (engine.py) MINUS rank_gate, judge_target/examine_cap early-stopping, and the
    MIN_RESULTS backfill -- none apply here since this harness examines the whole
    sample unconditionally and stops at screen_gate. Rounds are TARGET_POOL-sized
    (90), same as production, so a >90-job cluster sample gets split into multiple
    rounds with their own independently-computed dynamic_hard_drop_threshold,
    same as a real run would see."""
    soft_axes, hard_axes = engine_svc._hard_enforced_axes(engine, cluster_profile)
    target_pool = engine_svc.TARGET_POOL
    gate_batch = engine._GATE_BATCH
    rounds = []

    for round_index, pos in enumerate(range(0, len(jobs), target_pool)):
        batch = jobs[pos:pos + target_pool]
        before = len(llm_calls)
        if dry_run:
            _dry_run_stand_in(engine, cluster_profile, batch, llm_calls)
        else:
            engine.screen_gate(batch, cluster_profile)
        round_calls = llm_calls[before:]

        # screen_gate re-asks for any listing a response omitted (see its
        # _screen_one_batch), so a round can now produce MORE calls than batches.
        # Only the first `n_batches` map positionally onto slices of `batch`;
        # mapping a retry call that way would slice past the end and flag real
        # listings as missing. Retries are recorded but not position-mapped, and
        # the omission flag below is resolved from the FINAL annotation instead of
        # from the first response, so a listing the retry recovered is no longer
        # reported as unjudged.
        n_batches = math.ceil(len(batch) / gate_batch) if batch else 0
        for batch_index, call in enumerate(round_calls):
            call["cluster_idx"] = cluster_idx
            call["round_index"] = round_index
            call["batch_index"] = batch_index
            call["is_retry"] = batch_index >= n_batches
            if dry_run or call["is_retry"]:
                continue
            sub = batch[batch_index * gate_batch:(batch_index + 1) * gate_batch]
            if call.get("error") is not None:
                for j in sub:
                    j["_llm_call_failed"] = True

        if not dry_run:
            for j in batch:
                # screen_gate sets _gate_unjudged on anything it never got a real
                # verdict for, first call or retry -- more reliable than re-parsing
                # raw responses here, and correct however many retries it made.
                if j.get("_gate_unjudged"):
                    j["_missing_from_response"] = True

        def _clears_hard(j):
            return (j.get("_hard_gate_ok", True) and j.get("_listing_ok", True)
                    and all(j.get(a, True) for a in hard_axes))

        hard_gate_failed = [j for j in batch if not _clears_hard(j)]
        remaining = [j for j in batch if _clears_hard(j)]
        off_sector = [j for j in remaining if not j.get("_sector_ok", True)]
        in_sector = [j for j in remaining if j.get("_sector_ok", True)]

        soft_fail_counts = [sum(1 for axis in soft_axes if not j.get(axis, True)) for j in in_sector]
        threshold = engine.dynamic_hard_drop_threshold(soft_fail_counts)

        survivors, dropped_soft = [], []
        for j, fails in zip(in_sector, soft_fail_counts):
            j["_soft_fail_count"] = fails
            (dropped_soft if fails >= threshold else survivors).append(j)

        for j in hard_gate_failed:
            j["_bucket"] = "dropped_hard_filter"
        for j in off_sector:
            j["_bucket"] = "dropped_off_sector"
        for j in dropped_soft:
            j["_bucket"] = "dropped_soft_threshold"
        for j in survivors:
            j["_bucket"] = "survivor"
        for j in batch:
            j["_cluster_idx"] = cluster_idx
            j["_round_index"] = round_index

        rounds.append({
            "round_index": round_index,
            "batch_size": len(batch),
            "threshold": threshold,
            "soft_fail_counts": soft_fail_counts,
            "bucket_counts": {
                "dropped_hard_filter": len(hard_gate_failed),
                "dropped_off_sector": len(off_sector),
                "dropped_soft_threshold": len(dropped_soft),
                "survivor": len(survivors),
            },
        })
    return rounds


def _finalize_job_record(d, cluster_labels):
    axes = {a: d.get(f"_{a}") for a in _AXIS_ORDER}
    cluster_idx = d.get("_cluster_idx", d.get("_cluster", 0))
    reason_raw = d.get("_gate_reason")
    return {
        "identity": d.get("_identity"),
        "cluster_idx": cluster_idx,
        "cluster_label": cluster_labels[cluster_idx] if 0 <= cluster_idx < len(cluster_labels) else None,
        "title": d.get("title"), "company": d.get("company"), "location": d.get("location"),
        "url": d.get("url"), "snippet": d.get("snippet"),
        "embed_score": d.get("embed_score"), "embed_fallback": d.get("_embed_fallback", False),
        "axes": axes,
        "gate_reason_raw": reason_raw,
        "gate_reason_decoded": _decode_reason(reason_raw) if reason_raw else None,
        # Model-cited evidence for a seniority_ok=false verdict (screen_v10) -- only
        # present on a fresh judgment, never a cache hit (see screen_gate's docstring).
        # Lets a reviewer check the code direction (high/low) against what it's
        # actually anchored on, instead of taking the packed code on faith.
        "seniority_signal": d.get("_seniority_signal"),
        "key_requirements": d.get("_key_requirements") or [],
        "bucket": d.get("_bucket"),
        "round_index": d.get("_round_index"),
        "soft_fail_count": d.get("_soft_fail_count"),
        "prior_eval_verdict": d.get("_eval_verdict"),
        "dead_reason": None,  # sampling excludes dead rows by construction
        "llm_call_failed": d.get("_llm_call_failed", False),
        "missing_from_response": d.get("_missing_from_response", False),
        # --- ground truth (only meaningful when the row carries a judge verdict) ---
        "judge_verdict": d.get("_eval_verdict"),
        "judge_reason": d.get("_judge_reason"),
        "gate_kept": d.get("_bucket") == "survivor",
        "disqualifier_visibility": d.get("_disqualifier_visibility"),
        "gate_text_chars": d.get("_gate_text_chars"),
        "snippet_chars": d.get("_snippet_chars"),
        "full_text_chars": d.get("_full_text_chars"),
    }


def _attach_ground_truth(scored):
    """Pull each row's judge verdict + disqualifier reason off the cached
    eval_analysis and score whether the gate could have seen what the judge
    rejected on. Free -- no LLM calls, all of it already persisted."""
    for d in scored:
        if not d.get("_eval_verdict"):
            continue
        reason = None
        try:
            analysis = json.loads(d.get("_eval_analysis") or "{}")
        except Exception:
            analysis = {}
        concerns = [c for c in (analysis.get("concerns") or []) if str(c).strip()]
        if concerns:
            reason = str(concerns[0])
        d["_judge_reason"] = reason
        d["_disqualifier_visibility"] = _disqualifier_visibility(
            reason, d.get("_gate_text_seen") or "", d.get("snippet") or "",
            d.get("_full_text_original") or "",
        )


def _ground_truth_stats(job_records):
    """Score the gate against the judge's verdicts.

    Reported as two INDEPENDENT rates, never one accuracy figure, because the two
    errors have opposite costs and opposite fixes. A gate dropping a job the
    judge would have liked destroys a result the user will never see; a gate
    keeping a job the judge later rejects only wastes a rank/judge call. A single
    blended number lets a change that trades several of the first for many of the
    second look like an improvement.
    """
    labelled = [j for j in job_records if j["judge_verdict"]]
    if not labelled:
        return None

    strong = [j for j in labelled if j["judge_verdict"] == "strong"]
    reject = [j for j in labelled if j["judge_verdict"] == "reject"]
    other = [j for j in labelled if j["judge_verdict"] not in ("strong", "reject")]

    kept_strong = [j for j in strong if j["gate_kept"]]
    dropped_strong = [j for j in strong if not j["gate_kept"]]
    kept_reject = [j for j in reject if j["gate_kept"]]
    dropped_reject = [j for j in reject if not j["gate_kept"]]

    def _pct(n, d):
        return round(100.0 * n / d, 1) if d else None

    # Of the rejects the gate let through, how many were even knowable from the
    # text it read? Splits "the gate was wrong" from "the gate was blind".
    vis = Counter()
    for j in kept_reject:
        v = j.get("disqualifier_visibility")
        if v is None:
            vis["no_quoted_requirement"] += 1
        elif v["in_gate_text"]:
            vis["visible_to_gate_but_missed"] += 1
        elif v["in_full_text"]:
            vis["only_in_full_text"] += 1
        else:
            vis["not_found_in_any_text"] += 1

    return {
        "n_labelled": len(labelled),
        "n_strong": len(strong), "n_reject": len(reject), "n_other_verdict": len(other),
        # The number that matters most: good jobs the cheap gate killed.
        "strong_kept": len(kept_strong), "strong_dropped": len(dropped_strong),
        "strong_retention_pct": _pct(len(kept_strong), len(strong)),
        "strong_dropped_detail": [
            {"title": j["title"], "company": j["company"], "bucket": j["bucket"],
             "axes": j["axes"], "gate_reason_decoded": j["gate_reason_decoded"],
             "url": j["url"]}
            for j in dropped_strong
        ],
        # The saving: bad jobs the cheap gate caught before the mid/strong tiers paid.
        "reject_dropped": len(dropped_reject), "reject_kept": len(kept_reject),
        "reject_catch_pct": _pct(len(dropped_reject), len(reject)),
        "missed_reject_visibility": dict(vis),
        "missed_reject_detail": [
            {"title": j["title"], "company": j["company"],
             "judge_reason": j["judge_reason"],
             "visibility": j.get("disqualifier_visibility"),
             "gate_text_chars": j.get("gate_text_chars"),
             "snippet_chars": j.get("snippet_chars"),
             "full_text_chars": j.get("full_text_chars"),
             "url": j["url"]}
            for j in kept_reject
        ],
    }


def _build_aggregate_stats(job_records):
    overall = Counter(j["bucket"] for j in job_records)
    by_cluster = defaultdict(Counter)
    for j in job_records:
        by_cluster[j["cluster_idx"]][j["bucket"]] += 1
    soft_fail_hist = Counter(j["soft_fail_count"] for j in job_records if j["soft_fail_count"] is not None)
    reason_freq = Counter(j["gate_reason_raw"] for j in job_records if j["gate_reason_raw"])
    key_req_freq = Counter()
    for j in job_records:
        for kr in j["key_requirements"]:
            item = kr.get("item", "")
            if item:
                key_req_freq[item] += 1
    failed = [j["identity"] for j in job_records if j["llm_call_failed"]]
    missing = [j["identity"] for j in job_records if j["missing_from_response"]]
    return {
        "bucket_counts_overall": dict(overall),
        "bucket_counts_by_cluster": {str(k): dict(v) for k, v in by_cluster.items()},
        "soft_fail_histogram": {str(k): v for k, v in sorted(soft_fail_hist.items())},
        "reason_code_frequency": dict(reason_freq.most_common()),
        "key_requirements_frequency": dict(key_req_freq.most_common(30)),
        "gate_error_count": len(failed),
        "missing_decision_count": len(missing),
        "flagged_job_identities": failed + missing,
    }


def _cost_notice(est_calls, model_name, dry_run):
    if dry_run:
        print("[gate_harness] --dry-run: building prompts only, NO live screen_gate/llm() calls, "
              "no per-listing cost (build_snapshot's own small one-off region-inference/"
              "role-clustering call still runs, same negligible cost analyze_embedding_gate.py "
              "already accepts).")
        return
    print(f"[gate_harness] LIVE RUN: about to make ~{est_calls} real call(s) to {model_name} "
          f"(cheap tier, batches of up to 20 listings). This spends real API credits.")


def _print_profile_formation(formation):
    print("\n=== Profile formation (what feeds screen_gate's prompt) ===")
    for f in formation["fields"]:
        val = f["value"]
        if val in (None, [], "", {}):
            continue
        print(f"  {f['field']}: {val}")
        print(f"      -> {f['drives']}")
    print(f"\n  Role clusters ({len(formation['role_clusters'])}):")
    for c in formation["role_clusters"]:
        print(f"    [{c['label']}] roles={c['roles']}")
        wt = c.get("weighted_text") or ""
        suffix = "..." if len(wt) > 200 else ""
        print(f"      weighted_text: {wt[:200]}{suffix}")


def _print_job_line(rec):
    axis_str = "".join("+" if rec["axes"][a] else "-" for a in _AXIS_ORDER)
    flag = ""
    if rec["llm_call_failed"]:
        flag = "  [LLM CALL FAILED -- fail-open]"
    elif rec["missing_from_response"]:
        flag = "  [OMITTED FROM RESPONSE -- fail-open]"
    title = (rec["title"] or "")[:55]
    company = rec["company"] or ""
    bucket = rec["bucket"] or ""
    print(f"    [{axis_str}] {bucket:<24} {title} @ {company}{flag}")


def _print_round_summary(cluster_label, r):
    print(f"  [{cluster_label}] round {r['round_index']}: {r['batch_size']} examined, "
          f"threshold={r['threshold']}+ soft-axis failures, buckets={r['bucket_counts']}")


def _print_final_summary(stats, sample_actual, n_available):
    print("\n=== Aggregate results ===")
    print(f"  sampled {sample_actual} of {n_available} cached listings")
    print(f"  overall buckets: {stats['bucket_counts_overall']}")
    for cluster_idx, counts in stats["bucket_counts_by_cluster"].items():
        print(f"    cluster {cluster_idx}: {counts}")
    print(f"  soft-fail-count histogram: {stats['soft_fail_histogram']}")
    print(f"  most common reason codes: {list(stats['reason_code_frequency'].items())[:10]}")
    print(f"  most common key_requirements: {list(stats['key_requirements_frequency'].items())[:10]}")
    if stats["gate_error_count"] or stats["missing_decision_count"]:
        print(f"  ** RELIABILITY WARNING: {stats['gate_error_count']} LLM call failure(s), "
              f"{stats['missing_decision_count']} listing(s) omitted from a response ** "
              f"identities: {stats['flagged_job_identities'][:20]}")


def _print_ground_truth(gt, text_mode):
    print("\n=== Gate vs the final judge (ground truth) ===")
    print(f"  text mode: {text_mode}  |  {gt['n_labelled']} labelled listing(s): "
          f"{gt['n_strong']} strong, {gt['n_reject']} reject"
          + (f", {gt['n_other_verdict']} other" if gt["n_other_verdict"] else ""))
    print(f"\n  GOOD JOBS KEPT   {gt['strong_kept']}/{gt['n_strong']} "
          f"({gt['strong_retention_pct']}%) -- the gate let these reach the judge")
    if gt["strong_dropped_detail"]:
        print(f"  ** {gt['strong_dropped']} judge-approved listing(s) the gate would now DROP: **")
        for j in gt["strong_dropped_detail"]:
            failed = [a for a, ok in (j["axes"] or {}).items() if ok is False]
            print(f"     - {(j['title'] or '')[:60]} @ {j['company']}  [{j['bucket']}] "
                  f"failed={failed}")
    print(f"\n  BAD JOBS CAUGHT  {gt['reject_dropped']}/{gt['n_reject']} "
          f"({gt['reject_catch_pct']}%) -- caught before rank/judge spend")
    if gt["missed_reject_visibility"]:
        print(f"  of the {gt['reject_kept']} the gate let through:")
        for k, v in sorted(gt["missed_reject_visibility"].items(), key=lambda kv: -kv[1]):
            print(f"     {v:>3}  {k}")


def _write_json_report(path, report):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)


_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: -apple-system, "Segoe UI", Arial, sans-serif; margin: 0; padding: 24px;
       background: #0b0d10; color: #e6e6e6; }
@media (prefers-color-scheme: light) {
  body { background: #f7f7f8; color: #16181d; }
}
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 17px; margin-top: 28px; }
h3 { font-size: 14px; }
.banner { padding: 10px 14px; border-radius: 8px; margin: 10px 0; font-size: 13px; }
.banner.dry { background: #3a3410; color: #f2d675; }
.banner.live { background: #1a3a2e; color: #7be0a8; }
.banner.warn { background: #3a1a1a; color: #f28b8b; }
.stat-row { display: flex; gap: 10px; flex-wrap: wrap; margin: 14px 0; }
.stat-tile { background: rgba(127,127,127,0.12); border-radius: 10px; padding: 10px 16px; min-width: 130px; }
.stat-tile .num { font-size: 22px; font-weight: 700; }
.stat-tile .label { font-size: 11px; opacity: 0.75; }
.bar-row { display: flex; align-items: center; gap: 8px; margin: 3px 0; font-size: 12px; }
.bar-row .label { width: 170px; flex-shrink: 0; }
.bar-row .bar { height: 11px; border-radius: 6px; background: #4c8bf5; }
details { margin: 6px 0; border: 1px solid rgba(127,127,127,0.25); border-radius: 8px; padding: 6px 12px; }
summary { cursor: pointer; font-weight: 600; font-size: 13px; }
table { border-collapse: collapse; width: 100%; font-size: 12.5px; margin-top: 10px; }
th, td { border-bottom: 1px solid rgba(127,127,127,0.2); padding: 5px 7px; text-align: left; vertical-align: top; }
th { cursor: pointer; user-select: none; }
th:hover { opacity: 0.7; }
.chip { display: inline-block; width: 16px; text-align: center; border-radius: 4px; margin-right: 2px;
        font-size: 10px; font-family: monospace; }
.chip.ok { background: #1a3a2e; color: #7be0a8; }
.chip.bad { background: #3a1a1a; color: #f28b8b; }
.bucket { padding: 2px 7px; border-radius: 6px; font-size: 11px; font-weight: 600; white-space: nowrap; }
.bucket.survivor { background: #1a3a2e; color: #7be0a8; }
.bucket.dropped_hard_filter { background: #3a1a1a; color: #f28b8b; }
.bucket.dropped_off_sector { background: #3a1a1a; color: #f28b8b; }
.bucket.dropped_soft_threshold { background: #3a2f10; color: #f2c675; }
.flag { color: #f2c675; font-weight: 600; font-size: 11px; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; margin: 10px 0; }
input, select { padding: 5px 8px; border-radius: 6px; border: 1px solid rgba(127,127,127,0.4);
                background: transparent; color: inherit; font-size: 12.5px; }
tr.job-row { cursor: pointer; }
tr.job-row:hover { background: rgba(127,127,127,0.08); }
.row-detail { display: none; }
.row-detail.open { display: table-row; }
.row-detail td { background: rgba(127,127,127,0.06); }
pre { white-space: pre-wrap; word-break: break-word; font-size: 11.5px; max-height: 400px; overflow-y: auto; }
a { color: #7bb0f2; }
code { font-size: 11.5px; }
"""

_JS = """
const AXIS_KEYS = ["sector_ok","hard_gate_ok","listing_ok","seniority_ok","requirements_ok","skills_ok","salary_ok","work_arrangement_ok"];
const AXIS_LABELS = {sector_ok:"Sec", hard_gate_ok:"Hrd", listing_ok:"Lst", seniority_ok:"Sen", requirements_ok:"Req",
                      skills_ok:"Skl", salary_ok:"Sal", work_arrangement_ok:"Wrk"};

function escapeHtml(s) {
  return String(s === undefined || s === null ? "" : s).replace(/[&<>"']/g, function(m) {
    return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[m];
  });
}

function renderDashboard(r) {
  const stats = r.aggregate_stats;
  const overall = stats.bucket_counts_overall;
  const total = Object.values(overall).reduce(function(a,b){ return a+b; }, 0) || 1;
  let html = '<div class="stat-row">';
  html += '<div class="stat-tile"><div class="num">' + r.sample_actual + '</div><div class="label">sampled / ' + r.n_available_in_db + ' available</div></div>';
  html += '<div class="stat-tile"><div class="num">' + r.clusters.length + '</div><div class="label">cluster(s)</div></div>';
  for (const bucket in overall) {
    const count = overall[bucket];
    html += '<div class="stat-tile"><div class="num">' + count + '</div><div class="label">' + escapeHtml(bucket) + ' (' + Math.round(100*count/total) + '%)</div></div>';
  }
  html += '</div>';
  for (const bucket in overall) {
    const count = overall[bucket];
    const pct = Math.round(100*count/total);
    html += '<div class="bar-row"><span class="label">' + escapeHtml(bucket) + '</span><div class="bar" style="width:' + (pct*2.5) + 'px"></div><span>' + count + ' (' + pct + '%)</span></div>';
  }
  if (stats.gate_error_count || stats.missing_decision_count) {
    html += '<div class="banner warn">RELIABILITY WARNING: ' + stats.gate_error_count + ' LLM call failure(s), ' +
            stats.missing_decision_count + ' listing(s) omitted from a response (both fail open -- treated as all-axes-pass).</div>';
  }
  if (r.warnings && r.warnings.length) {
    for (const w of r.warnings) {
      html += '<div class="banner dry">' + escapeHtml(w) + '</div>';
    }
  }
  document.getElementById('dashboard').innerHTML = html;
}

function renderFormation(r) {
  let html = '';
  for (const f of r.profile_formation.fields) {
    const isEmpty = f.value === null || f.value === '' ||
      (Array.isArray(f.value) && f.value.length === 0) ||
      (typeof f.value === 'object' && f.value !== null && Object.keys(f.value).length === 0);
    if (isEmpty) continue;
    html += '<details><summary>' + escapeHtml(f.field) + '</summary><div><b>Value:</b> <code>' +
            escapeHtml(JSON.stringify(f.value)) + '</code><br><b>Drives:</b> ' + escapeHtml(f.drives) + '</div></details>';
  }
  html += '<h3>Role clusters</h3>';
  for (const c of r.profile_formation.role_clusters) {
    html += '<details><summary>' + escapeHtml(c.label) + ' (' + escapeHtml((c.roles||[]).join(', ')) + ')</summary>' +
            '<pre>' + escapeHtml(c.weighted_text) + '</pre></details>';
  }
  document.getElementById('formation').innerHTML = html;
}

function axisChips(axes) {
  let html = '';
  for (const k of AXIS_KEYS) {
    html += '<span class="chip ' + (axes[k] ? 'ok' : 'bad') + '" title="' + k + '">' + AXIS_LABELS[k] + '</span>';
  }
  return html;
}

function jobDetailHtml(j) {
  return '<div><b>Snippet:</b> ' + escapeHtml(j.snippet) + '</div>' +
    '<div style="margin-top:4px"><b>URL:</b> <a href="' + escapeHtml(j.url) + '" target="_blank" rel="noopener">' + escapeHtml(j.url) + '</a></div>' +
    '<div style="margin-top:4px"><b>Key requirements:</b> ' + escapeHtml(JSON.stringify(j.key_requirements)) + '</div>' +
    '<div style="margin-top:4px"><b>Decoded reason:</b> ' + escapeHtml(JSON.stringify(j.gate_reason_decoded)) + '</div>' +
    (j.seniority_signal ? '<div style="margin-top:4px"><b>Seniority signal (model-cited anchor):</b> ' + escapeHtml(j.seniority_signal) + '</div>' : '') +
    '<div style="margin-top:4px"><b>Soft-fail count this round:</b> ' + (j.soft_fail_count === null || j.soft_fail_count === undefined ? 'n/a (dropped before soft-axis scoring)' : j.soft_fail_count) + '</div>' +
    '<div style="margin-top:4px"><b>Prior final-judge verdict (if any):</b> ' + escapeHtml(j.prior_eval_verdict || 'none on record') + '</div>';
}

let sortKey = null, sortDir = 1;

function renderJobs() {
  const q = document.getElementById('search').value.toLowerCase();
  const bucketF = document.getElementById('bucketFilter').value;
  const clusterF = document.getElementById('clusterFilter').value;
  const axisF = document.getElementById('axisFilter').value;
  let rows = REPORT.jobs.filter(function(j) {
    if (q && ((j.title||'') + ' ' + (j.company||'')).toLowerCase().indexOf(q) === -1) return false;
    if (bucketF && j.bucket !== bucketF) return false;
    if (clusterF && String(j.cluster_idx) !== clusterF) return false;
    if (axisF && j.axes[axisF] !== false) return false;
    return true;
  });
  if (sortKey) {
    rows = rows.slice().sort(function(a, b) {
      let av = a[sortKey], bv = b[sortKey];
      if (av === null || av === undefined) av = '';
      if (bv === null || bv === undefined) bv = '';
      if (av === bv) return 0;
      return (av > bv ? 1 : -1) * sortDir;
    });
  }
  document.getElementById('rowCount').textContent = rows.length + ' / ' + REPORT.jobs.length;
  const body = document.getElementById('jobsBody');
  body.innerHTML = '';
  for (const j of rows) {
    const tr = document.createElement('tr');
    tr.className = 'job-row';
    tr.innerHTML = '<td>' + escapeHtml(j.cluster_label) + '</td>' +
      '<td>' + escapeHtml(j.title) + '</td>' +
      '<td>' + escapeHtml(j.company) + '</td>' +
      '<td>' + (j.embed_score !== null && j.embed_score !== undefined ? j.embed_score.toFixed(3) : (j.embed_fallback ? 'no embedding' : '')) + '</td>' +
      '<td>' + axisChips(j.axes) + '</td>' +
      '<td><span class="bucket ' + j.bucket + '">' + j.bucket + '</span>' +
      (j.llm_call_failed ? ' <span class="flag">LLM FAIL</span>' : '') +
      (j.missing_from_response ? ' <span class="flag">OMITTED</span>' : '') + '</td>';
    const detailRow = document.createElement('tr');
    detailRow.className = 'row-detail';
    detailRow.innerHTML = '<td colspan="6">' + jobDetailHtml(j) + '</td>';
    tr.addEventListener('click', function() { detailRow.classList.toggle('open'); });
    body.appendChild(tr);
    body.appendChild(detailRow);
  }
}

function populateFilters() {
  const buckets = Array.from(new Set(REPORT.jobs.map(function(j){ return j.bucket; })));
  const bf = document.getElementById('bucketFilter');
  for (const b of buckets) bf.innerHTML += '<option value="' + b + '">' + b + '</option>';
  const clusters = Array.from(new Set(REPORT.jobs.map(function(j){ return j.cluster_idx; }))).sort();
  const cf = document.getElementById('clusterFilter');
  for (const c of clusters) {
    const label = (REPORT.clusters[c] && REPORT.clusters[c].label) || ('cluster ' + c);
    cf.innerHTML += '<option value="' + c + '">' + escapeHtml(label) + '</option>';
  }
  const af = document.getElementById('axisFilter');
  for (const a of AXIS_KEYS) af.innerHTML += '<option value="' + a + '">' + a + ' = false</option>';
}

function renderPromptLog() {
  let html = '';
  REPORT.llm_calls.forEach(function(c, i) {
    const label = 'cluster ' + c.cluster_idx + ' / round ' + c.round_index + ' / batch ' + c.batch_index +
      (c.dry_run ? ' (DRY RUN)' : '') + (c.error ? ' [ERROR]' : '');
    const meta = c.model + ', temp=' + c.temperature + (c.latency_seconds !== null && c.latency_seconds !== undefined ? ', ' + c.latency_seconds + 's' : '');
    html += '<details><summary>Call ' + (i+1) + ': ' + escapeHtml(label) + ' -- ' + escapeHtml(meta) + '</summary>';
    if (c.system) html += '<div><b>System:</b><pre>' + escapeHtml(c.system) + '</pre></div>';
    html += '<div><b>Prompt:</b><pre>' + escapeHtml(c.prompt) + '</pre></div>';
    if (c.error) {
      html += '<div><b>Error:</b><pre>' + escapeHtml(c.error) + '</pre></div>';
    } else if (c.dry_run) {
      html += '<div><i>Dry run -- no API call made, no response.</i></div>';
    } else {
      html += '<div><b>Raw response:</b><pre>' + escapeHtml(c.raw_response) + '</pre></div>';
    }
    html += '</details>';
  });
  document.getElementById('promptLog').innerHTML = html;
}

document.addEventListener('DOMContentLoaded', function() {
  renderDashboard(REPORT);
  renderFormation(REPORT);
  populateFilters();
  renderJobs();
  renderPromptLog();
  document.querySelectorAll('#jobsTable th[data-key]').forEach(function(th) {
    th.addEventListener('click', function() {
      const key = th.dataset.key;
      sortDir = (sortKey === key) ? -sortDir : 1;
      sortKey = key;
      renderJobs();
    });
  });
  document.getElementById('search').addEventListener('input', renderJobs);
  document.getElementById('bucketFilter').addEventListener('change', renderJobs);
  document.getElementById('clusterFilter').addEventListener('change', renderJobs);
  document.getElementById('axisFilter').addEventListener('change', renderJobs);
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
  <h1>Gate Harness Report</h1>
  <div>__BANNER__</div>
  <div id="dashboard"></div>
  <h2>Profile formation</h2>
  <div id="formation"></div>
  <h2>Results (<span id="rowCount"></span>)</h2>
  <div class="controls">
    <input id="search" placeholder="Search title/company...">
    <select id="bucketFilter"><option value="">All buckets</option></select>
    <select id="clusterFilter"><option value="">All clusters</option></select>
    <select id="axisFilter"><option value="">Any axis</option></select>
  </div>
  <table id="jobsTable">
    <thead><tr>
      <th data-key="cluster_label">Cluster</th>
      <th data-key="title">Title</th>
      <th data-key="company">Company</th>
      <th data-key="embed_score">Embed score</th>
      <th title="Sector / Hard-filter / Real-listing / Seniority / Requirements / Skills / Salary / Work-arrangement">Axes</th>
      <th data-key="bucket">Bucket</th>
    </tr></thead>
    <tbody id="jobsBody"></tbody>
  </table>
  <h2>Prompt / response log (__CALL_COUNT__ call(s))</h2>
  <div id="promptLog"></div>
<script>
const REPORT = __REPORT_JSON__;
__JS__
</script>
</body>
</html>
"""


def render_html_report(report) -> str:
    dry = report["dry_run"]
    banner_class = "dry" if dry else "live"
    banner_text = (
        "DRY RUN -- placeholder axis values, no live API calls were made."
        if dry else
        f"LIVE RUN -- real calls made to {report['model']}. {report['sample_actual']} listings judged."
    )
    report_json = json.dumps(report).replace("</", "<\\/")
    html = _HTML_SHELL
    html = html.replace("__TITLE__", f"Gate Harness - {report['profile_name']}")
    html = html.replace("__CSS__", _CSS)
    html = html.replace("__BANNER__", f'<div class="banner {banner_class}">{banner_text}</div>')
    html = html.replace("__CALL_COUNT__", str(len(report["llm_calls"])))
    html = html.replace("__REPORT_JSON__", report_json)
    html = html.replace("__JS__", _JS)
    return html


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()

    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))

    import app.database as database
    import app.models as models
    from app.services import snapshot
    from app.services import engine as engine_svc
    import full_auto as engine  # noqa: E402  -- after app.* imports so .env is loaded

    db = database.SessionLocal()
    try:
        profile = _resolve_profile(db, models, args.profile_id)
        print(f"[gate_harness] profile {profile.id} ({profile.name!r})")

        rows, n_available = _sample_jobs(db, models, profile.id, args.sample_size, args.seed,
                                          ground_truth=args.ground_truth)
        if len(rows) < args.sample_size:
            print(f"[gate_harness] WARNING: only {n_available} "
                  f"{'judge-labelled ' if args.ground_truth else ''}non-dead cached listings "
                  f"available for this profile; sampling all of them (requested "
                  f"{args.sample_size}).")
        if args.ground_truth:
            print(f"[gate_harness] --ground-truth: scoring the gate against {len(rows)} listing(s) "
                  f"the final judge has already ruled on; --text-mode={args.text_mode}")

        print("[gate_harness] building profile snapshot (small real API cost: region-inference "
              "+ role-clustering, same as analyze_embedding_gate.py already accepts)...")
        role_clusters, eng_profile, cluster_embeddings = _build_snapshot_and_clusters(
            db, profile.id, snapshot, engine)
        if not role_clusters:
            raise SystemExit("profile has no target-role clusters -- nothing for screen_gate to "
                              "group listings by. Add at least one target_role attribute first.")
        print(f"[gate_harness] {len(role_clusters)} role cluster(s): "
              + "; ".join(f"{c['label']}={c['roles']}" for c in role_clusters))

        scored = _score_sampled_rows(engine_svc, engine, rows, cluster_embeddings)
        _apply_text_mode(engine, scored, args.text_mode)
        _attach_ground_truth(scored)
        by_cluster = defaultdict(list)
        for d in scored:
            by_cluster[d.get("_cluster", 0)].append(d)

        formation = _annotate_profile_formation(eng_profile, role_clusters)

        est_calls = sum(math.ceil(len(by_cluster.get(idx, [])) / engine._GATE_BATCH)
                         for idx in range(len(role_clusters)))
        _cost_notice(est_calls, engine.CHEAP_MODEL, args.dry_run)
        _print_profile_formation(formation)

        llm_calls: list = []
        if not args.dry_run:
            _install_capture_hooks(engine, llm_calls)

        clusters_meta = []
        print("\n=== Running screen_gate per cluster ===")
        for idx, cluster in enumerate(role_clusters):
            cluster_jobs = by_cluster.get(idx, [])
            cluster_profile = _build_cluster_profile(eng_profile, role_clusters, idx)
            soft_axes, hard_axes = engine_svc._hard_enforced_axes(engine, cluster_profile)
            print(f"\n[{cluster['label']}] {len(cluster_jobs)} sampled listing(s), "
                  f"hard_axes_promoted={list(hard_axes)}")
            rounds = _run_cluster_rounds(engine, engine_svc, cluster_profile, idx,
                                         cluster_jobs, args.dry_run, llm_calls)
            for r in rounds:
                _print_round_summary(cluster["label"], r)
            clusters_meta.append({
                "cluster_idx": idx, "label": cluster.get("label"), "roles": cluster.get("roles"),
                "cluster_profile_search_terms": cluster_profile.get("search_terms"),
                "hard_axes_promoted": list(hard_axes), "soft_axes": list(soft_axes),
                "n_jobs": len(cluster_jobs), "rounds": rounds,
            })

        cluster_labels = [c["label"] for c in role_clusters]
        job_records = [_finalize_job_record(d, cluster_labels) for d in scored]

        if not args.quiet:
            print("\n=== Per-listing results ===")
            for idx, cluster in enumerate(role_clusters):
                print(f"\n[{cluster['label']}]")
                for rec in (r for r in job_records if r["cluster_idx"] == idx):
                    _print_job_line(rec)

        aggregate_stats = _build_aggregate_stats(job_records)
        ground_truth = _ground_truth_stats(job_records)
        if ground_truth:
            _print_ground_truth(ground_truth, args.text_mode)

        warnings = [
            f"Production's real candidate queues are pre-filtered to embed_score >= "
            f"RELEVANCE_FLOOR ({engine_svc.RELEVANCE_FLOOR}); this harness's sample "
            f"deliberately skips that filter, so results here are broader/noisier than what a "
            f"live search run would actually feed to the gate."
        ]
        if args.ground_truth:
            warnings.append(
                "Ground-truth labels are the expensive judge's own stored verdicts. They are a "
                "strong reference, not an oracle -- a 'reject' means the judge disqualified the "
                "role, which the candidate may still disagree with, and the judge saw scraped "
                "full text the gate did not."
            )
            warnings.append(
                f"Labelled rows are only those that got far enough to be judged, i.e. rows that "
                f"already survived the embedding pre-filter, the gate and the rank floor on some "
                f"earlier run. Jobs the gate dropped and nothing ever judged CANNOT appear here, "
                f"so 'strong_retention_pct' measures the gate's consistency on jobs it has "
                f"previously let through, and is an UPPER bound on its true recall."
            )
            if args.text_mode == "auto":
                warnings.append(
                    "--text-mode=auto on a judged sample hands the gate the Phase-5-scraped "
                    "full_text it did NOT have at gate time in production. Re-run with "
                    "--text-mode=snippet for the honest production comparison."
                )
        if len(rows) < args.sample_size:
            warnings.append(f"Requested sample_size={args.sample_size} but only {n_available} "
                             f"non-dead cached listings were available; sampled all {len(rows)}.")

        if args.show_prompts:
            print("\n=== Captured prompts & responses ===")
            for i, call in enumerate(llm_calls):
                model_label = "dry-run" if call.get("dry_run") else call.get("model")
                print(f"\n--- call {i + 1}: cluster {call.get('cluster_idx')} round "
                      f"{call.get('round_index')} batch {call.get('batch_index')} ({model_label}) ---")
                print("[system]", call.get("system"))
                print("[prompt]", call.get("prompt"))
                if call.get("error"):
                    print("[ERROR]", call["error"])
                else:
                    print("[raw response]", call.get("raw_response"))

        _print_final_summary(aggregate_stats, len(rows), n_available)

        out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "tests" / "gate_reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile.id, "profile_name": profile.name,
            "seed": args.seed, "dry_run": args.dry_run, "model": engine.CHEAP_MODEL,
            "sample_requested": args.sample_size, "sample_actual": len(rows),
            "n_available_in_db": n_available,
            "ground_truth_mode": args.ground_truth, "text_mode": args.text_mode,
            "ground_truth": ground_truth,
            "profile_formation": formation,
            "clusters": clusters_meta,
            "jobs": job_records,
            "llm_calls": llm_calls,
            "aggregate_stats": aggregate_stats,
            "warnings": warnings,
        }

        json_path = out_dir / f"report_{stamp}.json"
        html_path = out_dir / f"report_{stamp}.html"
        _write_json_report(json_path, report)
        html_path.write_text(render_html_report(report), encoding="utf-8")

        print(f"\n[gate_harness] wrote JSON report to {json_path}")
        print(f"[gate_harness] wrote HTML report to {html_path} -- open it in a browser")
    finally:
        db.close()


if __name__ == "__main__":
    main()
