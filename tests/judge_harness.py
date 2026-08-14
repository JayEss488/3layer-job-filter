"""Read-only diagnostic harness for the FINAL JUDGE's requirements checklist
(full_auto's reasoning step D -- see CLAUDE.md's "search pipeline" section, the
Phase 6 notes on checklist discipline and the fit_level rubric).

WHY THIS EXISTS, AND WHY IT MEASURES THE CHECKLIST SPECIFICALLY. "concerns" and
"fit_level" are derived MECHANICALLY from the step-D checklist -- that is the
whole point of the v16-v18 checklist-discipline rework. The direct consequence is
that a JD ask which never reaches the checklist can never become a concern and can
never move the grade, however plainly the posting states it. So an under-extracted
checklist is not a cosmetic problem: it is silent, and it always errs toward
flattering the role.

The persisted store says this is happening. Over 160 judged rows carrying a
checklist (offline, free -- JobSeen.eval_analysis["requirements"]):
    median 4 items, mean 4.4, max 9; median 3 core, 46 rows with ZERO secondary
    only 20 of 160 (12.5%) reach the prompt's OWN stated "4-10 core, 2-6 secondary"
    checklist size vs JD text length: pearson r = 0.25, and a 6k-char JD gets a
    median of 5 items against a sub-1k JD's 3
i.e. the checklist is a roughly fixed-size habit rather than a reading of the
posting. Run scripts/audit_judge_checklists.py for the current numbers.

WHAT THIS HARNESS ADDS that the offline audit can't: a PAIRED, live A/B of two
judge system prompts over the SAME listings, so a prompt edit can be checked
before it is shipped. That matters more here than anywhere else in the pipeline,
because bumping FINAL_EVAL_PROMPT_VERSION re-opens every persisted verdict in the
store for re-judging at EXP_MODEL prices -- an unmeasured edit is the single most
expensive mistake available in this repo.

TWO RATES, NEVER ONE NUMBER -- the same discipline gate_harness.py's --ground-truth
mode follows, for the same reason: the two ways this can go wrong have opposite
costs and opposite fixes.
  * EXTRACTION (coverage / category fidelity): does the checklist contain the JD's
    stated asks, tagged the way the JD tags them? This is the defect being fixed.
  * SELECTION (picks, grade distribution): does the run still return roles? A
    prompt that "fixes" extraction by finding a dozen unmet requirements in every
    posting scores perfectly on the first rate and empties the results page. That
    failure is invisible in any coverage number and is the reason both are printed
    side by side and neither is ever averaged into the other.

GROUND TRUTH FOR "WHAT THE JD ASKS" IS CANDIDATE-BLIND, DELIBERATELY. The
denominator is screen_gate's own `key_requirements` extraction, which is run here
against the same listings (CHEAP_MODEL, ~1 call per 20). That pass has never seen
the candidate, so it cannot have been bent to fit them -- which is precisely the
failure mode step D's rules exist to prevent, and the reason the real prompt
already anchors on it as the "[key requirements]" hint.

WHICH IS WHY screen_gate IS RUN TWICE, AND THE FIRST VERSION OF THIS HARNESS WAS
WRONG. In production screen_gate runs BEFORE Phase 5 scraping, so for most
listings it extracts its asks from a ~455-char API teaser -- and the judge is told
to START FROM that hint. Extracting the hint here from the SCRAPED text instead
hands the judge a far richer starting list than it ever gets on a real run, and
measures a stage that does not exist (the first run of this harness scored the
unmodified v27 prompt at 6.55 checklist items against a production median of 4,
entirely on that artefact). So:
    --gate-text-mode snippet   the hint the judge REALLY receives   (default)
    --gate-text-mode full      the hint it could receive
and the ground-truth ask list is ALWAYS extracted from the full scraped text,
independently, whichever mode the hint is built in. The judge itself always reads
the full text either way -- that part is production-accurate in both modes, since
Phase 5 has already run by the time the judge sees a listing.

That split is the finding, not either number alone, and it separates two defects
needing opposite work. A required ask that is in the full text, ABSENT from the
thin hint, and absent from the checklist means the judge is not doing the "then
ADD whatever further requirements that pass could not see" half of its anchoring
instruction -- a prompt problem. A required ask missing in BOTH gate modes means
the extraction itself is miscalibrated -- also a prompt problem, but in
screen_gate's prompt, not the judge's. Reported separately as
`missed_and_not_in_hint` vs `missed_though_in_hint`.

The match between a JD ask and a checklist item is a text heuristic
(_ask_is_covered) and is not exact. It is applied IDENTICALLY to both arms, so the
DELTA is meaningful even where an absolute rate is not -- and every unmatched ask
is written to the report verbatim, so a suspicious number can be hand-checked
instead of trusted. Do not quote the absolute coverage rate as if it were exact.

SAMPLING FLATTERS THE JUDGE THE SAME WAY IT FLATTERS THE GATE, but far less: a row
with a verdict is a row that was scraped, so it carries real text. That is what
this stage gets in production too (the judge runs AFTER Phase 5), so unlike
gate_harness there is no snippet/full distinction to draw. Rows are still required
to carry real text (--min-text-chars), because a checklist built from a 400-char
teaser is a text-supply finding, not a prompt finding.

COST. One build_snapshot call (its small region-inference call), one embeddings
call, 2 x ceil(N/20) CHEAP_MODEL screen_gate calls (the production hint and the
ground-truth ask list), and ceil(N/20) EXP_MODEL judge calls PER ARM. At the
default --sample-size 16 that is 1 + 1 + 2 + 2 = 6 calls.
Nothing is written: gate_cache is bypassed (read AND write, same as
gate_harness._install_capture_hooks), no verdict is persisted back onto JobSeen,
and no Role/SearchRun rows are created. No discovery and no scraping happen --
only cached JobSeen rows already in the database are used.

Usage:
    venv/Scripts/python tests/judge_harness.py --dry-run
    venv/Scripts/python tests/judge_harness.py --sample-size 16 --seed 1
    venv/Scripts/python tests/judge_harness.py --arms candidate      # candidate only
    venv/Scripts/python tests/judge_harness.py --baseline-prompt tests/judge_prompt_baseline_v27.txt

--dry-run resolves the sample, builds the real job blocks and prints what each arm
WOULD send, making no screen_gate/judge calls (build_snapshot's own small call
still runs, the same negligible cost gate_harness --dry-run already accepts).

Writes a JSON report to --out-dir (default: tests/judge_reports/).
"""
import argparse
import json
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

DEFAULT_BASELINE = ROOT / "tests" / "judge_prompt_baseline_v27.txt"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile-id", type=int, default=None,
                   help="Defaults to the single active profile.")
    p.add_argument("--sample-size", type=int, default=16,
                   help="Judged JobSeen rows to re-judge, balanced across verdicts (default 16). "
                        "Kept at or below full_auto.FINAL_EVAL_MAX_JOBS_PER_CALL keeps each arm "
                        "to one call, so both arms read the same cross-listing context.")
    p.add_argument("--seed", type=int, default=1, help="Random seed for the sample (default 1).")
    p.add_argument("--worst-checklists", action="store_true",
                   help="Sample the listings whose PERSISTED checklist was thinnest, instead of a "
                        "balanced random draw. This is the highest-signal sample available for a "
                        "step-D change: a random draw mostly redraws listings the judge already "
                        "handled well, and at n~15 the delta on those is inside run-to-run noise. "
                        "It is deliberately NOT the default -- these rows are selected on the "
                        "outcome being measured, so an improvement here is regression-to-the-mean "
                        "until the balanced sample agrees on direction. Read both.")
    p.add_argument("--gate-text-mode", choices=("snippet", "full"), default="snippet",
                   help="Which text the candidate-blind pass may read when building the "
                        "'[key requirements]' HINT the judge is handed. 'snippet' (default) is "
                        "production reality -- screen_gate runs before Phase 5 scraping. 'full' "
                        "measures the ceiling. The ground-truth ask list is always built from the "
                        "full text, and the judge always reads the full text, in both modes. See "
                        "the module docstring: this split is the finding.")
    p.add_argument("--min-text-chars", type=int, default=1200,
                   help="Skip rows whose best available text is shorter than this (default 1200). "
                        "A thin checklist off a 400-char teaser is a text-supply finding, not a "
                        "prompt finding, and would only add noise here.")
    p.add_argument("--arms", default="baseline,candidate",
                   help="Comma-separated: 'baseline' (the frozen prompt file) and/or 'candidate' "
                        "(full_auto's current _FINAL_EVAL_SYSTEM). Default both.")
    p.add_argument("--baseline-prompt", default=str(DEFAULT_BASELINE),
                   help=f"File holding the frozen baseline system prompt (default {DEFAULT_BASELINE.name}).")
    p.add_argument("--out-dir", default=None, help="Output directory (default: tests/judge_reports/).")
    p.add_argument("--dry-run", action="store_true",
                   help="Build and show the real prompts; make NO screen_gate/judge calls.")
    p.add_argument("--show-prompts", action="store_true",
                   help="Print the full system prompt and user payload per arm (always captured "
                        "in the JSON report regardless).")
    return p.parse_args()


# ── ask/checklist-item matching ──────────────────────────────────────────────────
# Deliberately crude and deliberately symmetric. See the module docstring: the
# delta between two arms is the finding, not the absolute rate, and every miss is
# reported verbatim so the number can be checked rather than believed.
#
# THESE NOW LIVE IN full_auto AND ARE IMPORTED, NOT REDEFINED. The same matcher is
# what _sanitize_requirements_checklist uses to promote a required ask the judge
# filed as "secondary" back to "core" -- so the harness is measuring a decision
# made with this exact function. A local copy would let the fix and the metric for
# the fix drift apart and still agree with each other, which is the one way this
# measurement could go quietly wrong. Same reasoning as SOFT_GATE_AXES having one
# home. Do not re-inline them here to "decouple the test": the coupling IS the
# point, and the thing being measured (does the promotion fire on the right items)
# is not the matcher's own behaviour.
#
# Imported at module scope. main() imports full_auto again as a local and passes it
# into _run_arm -- that stays exactly as it is. Safe here only because the sys.path
# inserts near the top already ran; keep this below them.
import full_auto as _fa_match

_STOP = _fa_match._ASK_MATCH_STOP
_tokens = _fa_match.ask_tokens
_content_set = _fa_match.ask_content_set
_ask_is_covered = _fa_match.ask_is_covered


# A checklist item that names the ROLE'S WHOLE DOMAIN rather than an individual
# judgeable ask ("Full stack / software engineering" standing in for Node.js,
# TypeScript, Ruby/Rails, Nuxt and AWS CDK). This is the defect the existing
# specificity rule does NOT catch, because such an item is not a CAPACITY -- it
# reads as a legitimate skill name, and it is unfailable for anyone in the field.
# Flagged for eyeballing, never used as a pass/fail: the report lists every one.
_DOMAIN_WORDS = {
    "engineering", "engineer", "development", "developer", "analysis", "analyst",
    "analytics", "programming", "software", "stack", "science", "scientist", "data",
}


def _looks_like_domain_label(item_text):
    toks = set(_tokens(item_text))
    return bool(toks) and toks <= _DOMAIN_WORDS


# An UNFAILABLE item -- a capacity, an attitude, or advert boilerplate no applicant
# is ever screened out on. Step D bans these outright, and they are the specific way
# a push for a longer checklist backfires: every one comes back "met", the fit_level
# rubric counts it toward "every core requirement met", and the grade inflates. A
# first cut of the v28 rules raised the checklist on 14 of 14 listings and moved two
# picks to "very_strong" entirely on items like these, which is why this is measured
# rather than eyeballed. Pattern-matched, so it under-counts -- treat the number as a
# floor and read `unfailable_items` in the report for the verbatim list.
_UNFAILABLE_RE = re.compile(
    r"\b(?:attention to detail|problem[- ]solving|communication skills|interpersonal|"
    r"work(?:ing)? (?:independently|collaboratively|as part of a team)|team ?player|"
    r"willingness|eagerness|enthusias|genuine interest|an interest in|passion|proactive|"
    r"ability to learn|keen to|motivat|can-do|self[- ]starter|adaptab|"
    r"strong work ethic|positive attitude|willing to)\b",
    re.I,
)


def _looks_unfailable(item_text):
    return bool(_UNFAILABLE_RE.search(item_text or ""))


def _score_checklist(entry, blind_asks, hint_asks):
    """Per-job extraction metrics for one arm's output on one listing.

    `blind_asks` is the ground-truth ask list, always extracted from the full
    scraped text. `hint_asks` is what the judge was actually given as
    "[key requirements]" -- in the default snippet mode, a much thinner list. A
    missed ask is filed against whichever of those two it was, because the fix
    differs: see the module docstring."""
    items = entry.get("requirements") or []
    core = [i for i in items if (i or {}).get("category") == "core"]
    sec = [i for i in items if (i or {}).get("category") == "secondary"]

    covered, missed, mistiered = [], [], []
    for ask in blind_asks:
        hit = _ask_is_covered(ask.get("item", ""), items)
        if hit is None:
            missed.append(ask)
            continue
        covered.append({"ask": ask, "matched_item": hit})
        # CATEGORY FIDELITY: a candidate-blind pass tagged this ask "required";
        # did the judge keep it "core"? A required ask filed as "secondary" is
        # invisible to the fit_level rubric, which reads core items only -- so
        # this is the same silent failure as dropping the item, one step later.
        if ask.get("necessity") == "required" and (hit or {}).get("category") != "core":
            mistiered.append({"ask": ask, "matched_item": hit})

    required_asks = [a for a in blind_asks if a.get("necessity") == "required"]
    required_missed = [a for a in missed if a.get("necessity") == "required"]
    # Was the missed ask in the hint the judge was handed, or did it exist only
    # in the fuller text it was told to ADD from? Two different failures.
    missed_though_in_hint, missed_and_not_in_hint = [], []
    for a in required_missed:
        (missed_though_in_hint if _ask_is_covered(
            a.get("item", ""), [{"text": h.get("item", "")} for h in hint_asks])
         else missed_and_not_in_hint).append(a)
    return {
        "missed_though_in_hint": missed_though_in_hint,
        "missed_and_not_in_hint": missed_and_not_in_hint,
        "items": items,
        "n_items": len(items),
        "n_core": len(core),
        "n_secondary": len(sec),
        "n_unmet_core": sum(1 for i in core if not (i or {}).get("met")),
        "domain_label_items": [(i or {}).get("text") for i in items
                               if _looks_like_domain_label((i or {}).get("text"))],
        "unfailable_items": [(i or {}).get("text") for i in items
                             if _looks_unfailable((i or {}).get("text"))],
        "unfailable_core": sum(1 for i in core if _looks_unfailable((i or {}).get("text"))),
        "blind_asks_total": len(blind_asks),
        "blind_asks_required": len(required_asks),
        "covered": len(covered),
        "missed": missed,
        "required_missed": required_missed,
        "mistiered_required": mistiered,
        "fit_level": entry.get("fit_level"),
        "n_concerns": len(entry.get("concerns") or []),
        "n_strengths": len(entry.get("strengths") or []),
    }


def _aggregate(per_job):
    """Roll per-job metrics into the two rates. Kept apart on purpose -- see the
    module docstring's TWO RATES note."""
    if not per_job:
        return {}
    judged = [m for m in per_job.values() if m.get("n_items") is not None]
    sizes = [m["n_items"] for m in judged]
    cores = [m["n_core"] for m in judged]
    secs = [m["n_secondary"] for m in judged]
    asks = sum(m["blind_asks_total"] for m in judged)
    req_asks = sum(m["blind_asks_required"] for m in judged)
    covered = sum(m["covered"] for m in judged)
    req_missed = sum(len(m["required_missed"]) for m in judged)
    mistiered = sum(len(m["mistiered_required"]) for m in judged)
    missed_in_hint = sum(len(m["missed_though_in_hint"]) for m in judged)
    missed_not_in_hint = sum(len(m["missed_and_not_in_hint"]) for m in judged)
    grades = Counter(m["fit_level"] or "-" for m in judged)
    return {
        "n_jobs_scored": len(judged),
        # EXTRACTION
        "checklist_items_mean": round(statistics.mean(sizes), 2) if sizes else 0,
        "checklist_items_median": statistics.median(sizes) if sizes else 0,
        "core_mean": round(statistics.mean(cores), 2) if cores else 0,
        "secondary_mean": round(statistics.mean(secs), 2) if secs else 0,
        "rows_with_zero_secondary": sum(1 for s in secs if s == 0),
        "blind_asks_total": asks,
        "blind_asks_covered": covered,
        "ask_coverage_pct": round(100.0 * covered / asks, 1) if asks else None,
        "required_asks_total": req_asks,
        "required_asks_missed": req_missed,
        "required_coverage_pct": round(100.0 * (req_asks - req_missed) / req_asks, 1) if req_asks else None,
        "required_asks_mistiered_secondary": mistiered,
        # The split that says whose prompt to fix -- see the module docstring.
        "required_missed_though_in_hint": missed_in_hint,
        "required_missed_and_not_in_hint": missed_not_in_hint,
        "domain_label_rows": sum(1 for m in judged if m["domain_label_items"]),
        # PADDING -- the way a completeness push backfires. Counted per ITEM, not
        # per row, because the damage scales with how many unfailable "met" items
        # the rubric ends up counting.
        "unfailable_items_total": sum(len(m["unfailable_items"]) for m in judged),
        "unfailable_core_total": sum(m["unfailable_core"] for m in judged),
        "unfailable_per_checklist": round(
            sum(len(m["unfailable_items"]) for m in judged) / len(judged), 2) if judged else 0,
        # SELECTION -- the guard rail. A prompt that improves every number above
        # while emptying this one has not improved anything.
        "grades": dict(grades),
        "concerns_mean": round(statistics.mean([m["n_concerns"] for m in judged]), 2) if judged else 0,
    }


def _run_arm(fa, name, system_prompt, jobs, eng_profile, cv_text, llm_calls, dry_run):
    """One judge call over the whole sample under one system prompt.

    _FINAL_EVAL_SYSTEM is patched on the MODULE rather than passed as an argument
    because full_auto._run_final_eval reads it as a global -- so this exercises the
    real call path byte for byte, including the prompt-cache key routing, instead
    of a parallel copy that could drift from production."""
    original = fa._FINAL_EVAL_SYSTEM
    original_key = fa._FINAL_EVAL_CACHE_KEY
    fa._FINAL_EVAL_SYSTEM = system_prompt
    # Route each arm to its own prompt-cache entry. The API requires an exact
    # prefix match anyway, so a shared key could only ever cost a miss -- but a
    # per-arm key keeps the two arms' cache accounting readable in the usage log.
    fa._FINAL_EVAL_CACHE_KEY = f"judge_harness_{name}"
    t0 = time.monotonic()
    try:
        if dry_run:
            return {"arm": name, "dry_run": True, "strong": [], "backup": [], "excluded": [],
                    "seconds": 0.0, "system_chars": len(system_prompt)}
        strong, backup, excluded = fa.final_evaluation_split(jobs, eng_profile, cv_text=cv_text)
        return {
            "arm": name, "dry_run": False,
            "strong": strong or [], "backup": backup or [], "excluded": excluded or [],
            "call_failed": strong is None and backup is None and excluded is None,
            "seconds": round(time.monotonic() - t0, 1),
            "system_chars": len(system_prompt),
            "llm_calls": len(llm_calls),
        }
    finally:
        fa._FINAL_EVAL_SYSTEM = original
        fa._FINAL_EVAL_CACHE_KEY = original_key


def main():
    args = parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for a in arms:
        if a not in ("baseline", "candidate"):
            raise SystemExit(f"unknown arm {a!r} (expected 'baseline' and/or 'candidate')")

    import full_auto as fa
    from app.database import SessionLocal
    from app import models
    from app.services import engine as engine_svc
    from app.services import snapshot as snapshot_mod
    from gate_harness import (_build_cluster_profile, _install_capture_hooks,
                              _resolve_profile, _sample_jobs, _score_sampled_rows)

    baseline_text = None
    if "baseline" in arms:
        bp = Path(args.baseline_prompt)
        if not bp.exists():
            raise SystemExit(
                f"baseline prompt file not found: {bp}\n"
                "Freeze the pre-edit prompt first, e.g.:\n"
                "  venv/Scripts/python -c \"import full_auto,pathlib; "
                "pathlib.Path('tests/judge_prompt_baseline_v27.txt')"
                ".write_text(full_auto._FINAL_EVAL_SYSTEM, encoding='utf-8')\""
            )
        baseline_text = bp.read_text(encoding="utf-8")

    db = SessionLocal()
    try:
        profile = _resolve_profile(db, models, args.profile_id)
        print(f"profile {profile.id} ({profile.name or 'unnamed'})")

        snap = snapshot_mod.build_snapshot(db, profile.id)
        role_clusters = snap["role_clusters"]
        eng_profile = snap["engine_profile"]
        cv_text_base = snap["cv_text"]
        cluster_texts = [c["weighted_text"] for c in role_clusters]
        cluster_embeddings = fa.get_embeddings_batch(cluster_texts) if cluster_texts else []

        rows, n_available = _sample_jobs(db, models, profile.id,
                                         args.sample_size * 40 if args.worst_checklists
                                         else args.sample_size * 4,
                                         args.seed, ground_truth=True)
        scored = _score_sampled_rows(engine_svc, fa, rows, cluster_embeddings)
        # Require real text: see the module docstring's sampling note.
        scored = [d for d in scored
                  if len((d.get("full_text") or d.get("snippet") or "")) >= args.min_text_chars]
        if args.worst_checklists:
            # Ordered by the persisted checklist's own size. Rows with no stored
            # checklist (every reject) are excluded rather than sorted to the
            # front as zeroes -- a reject was never given one, which is not the
            # same defect and would swamp the sample.
            def _persisted_len(d):
                try:
                    return len((json.loads(d.get("_eval_analysis") or "{}")
                                or {}).get("requirements") or [])
                except (ValueError, TypeError):
                    return 0
            withlist = [(n, d) for d in scored for n in (_persisted_len(d),) if n]
            withlist.sort(key=lambda t: t[0])
            sample = [d for _, d in withlist[:args.sample_size]]
        else:
            # Re-balance across the judge's own prior verdicts AFTER the text filter,
            # or the filter silently re-skews the sample toward whichever verdict
            # happens to carry longer text.
            by_verdict = defaultdict(list)
            for d in scored:
                by_verdict[d.get("_eval_verdict") or "?"].append(d)
            sample, keys = [], sorted(by_verdict)
            while len(sample) < min(args.sample_size, len(scored)):
                took = False
                for k in keys:
                    if by_verdict[k] and len(sample) < args.sample_size:
                        sample.append(by_verdict[k].pop(0))
                        took = True
                if not took:
                    break
        if not sample:
            raise SystemExit(
                f"no judged rows with >= {args.min_text_chars} chars of text for profile "
                f"{profile.id} (of {n_available} judged rows) -- lower --min-text-chars."
            )

        # Judge everything as ONE cluster/group: the harness is measuring the
        # checklist, and per-cluster CV scoping is a separate mechanism with its
        # own reasons (see CLAUDE.md on cv_text_for_cluster). Using the whole
        # profile's CV keeps both arms reading exactly the same candidate.
        cluster_profile = _build_cluster_profile(eng_profile, role_clusters, 0) if role_clusters \
            else dict(eng_profile)
        cv_text = cv_text_base

        print(f"sample: {len(sample)} judged rows with real text "
              f"(of {n_available} judged rows for this profile)")
        print(f"  prior verdicts: {dict(Counter(d.get('_eval_verdict') for d in sample))}")

        llm_calls = []
        _install_capture_hooks(fa, llm_calls)

        # Candidate-blind requirement extraction, TWICE -- see the module
        # docstring. Pass 1 builds the ground-truth ask list from the full
        # scraped text. Pass 2 builds the "[key requirements]" hint the judge is
        # actually handed, under --gate-text-mode. Blinding the gate to
        # full_text is how screen_gate's own `full_text or snippet` falls back to
        # the teaser (the same mechanism gate_harness._apply_text_mode uses); the
        # full text is restored afterwards, because the JUDGE always reads it in
        # production regardless of what the gate saw.
        if args.dry_run:
            for d in sample:
                d.setdefault("_key_requirements", [])
            blind_by_ident = {d.get("_identity"): [] for d in sample}
            hint_by_ident = dict(blind_by_ident)
            print("dry run: skipping screen_gate (no [key requirements] hint will be built)")
        else:
            print(f"screen_gate pass 1/2: ground-truth ask list from the full scraped text "
                  f"({fa.CHEAP_MODEL})…")
            truth_jobs = [dict(d) for d in sample]
            fa.screen_gate(truth_jobs, cluster_profile)
            blind_by_ident = {d.get("_identity"): (d.get("_key_requirements") or [])
                              for d in truth_jobs}

            if args.gate_text_mode == "full":
                hint_by_ident = dict(blind_by_ident)
                for d in sample:
                    d["_key_requirements"] = blind_by_ident.get(d.get("_identity")) or []
                print("screen_gate pass 2/2: skipped (--gate-text-mode full reuses pass 1)")
            else:
                print(f"screen_gate pass 2/2: production hint from the "
                      f"{args.gate_text_mode} text ({fa.CHEAP_MODEL})…")
                saved = {}
                for d in sample:
                    saved[id(d)] = d.get("full_text")
                    d["full_text"] = ""
                try:
                    fa.screen_gate(sample, cluster_profile)
                finally:
                    for d in sample:
                        d["full_text"] = saved[id(d)]
                hint_by_ident = {d.get("_identity"): (d.get("_key_requirements") or [])
                                 for d in sample}

        n_blind = sum(len(v) for v in blind_by_ident.values())
        n_hint = sum(len(v) for v in hint_by_ident.values())
        n_req = sum(1 for v in blind_by_ident.values() for a in v if a.get("necessity") == "required")
        print(f"  ground-truth asks: {n_blind} ({n_req} tagged required)")
        print(f"  hint the judge gets ({args.gate_text_mode}): {n_hint} asks")

        if args.dry_run:
            print("\n--- job block that would be sent (job 1 of "
                  f"{len(sample)}) ---")
            print(fa._final_eval_job_block(1, sample[0],
                                           eng_profile.get("store_age_days"),
                                           eng_profile.get("max_listing_age_days"),
                                           eng_profile.get("max_listing_age_hard", True))[:2500])
            for name in arms:
                text = baseline_text if name == "baseline" else fa._FINAL_EVAL_SYSTEM
                print(f"\n--- arm {name}: system prompt {len(text)} chars ---")
                if args.show_prompts:
                    print(text)
            return

        arm_results, arm_metrics, arm_raw = {}, {}, {}
        for name in arms:
            system_prompt = baseline_text if name == "baseline" else fa._FINAL_EVAL_SYSTEM
            print(f"\n=== arm: {name} ({fa.EXP_MODEL}, system {len(system_prompt)} chars) ===")
            # A fresh copy per arm: final_evaluation_split annotates the dicts it
            # is handed, and a second arm inheriting the first's annotations would
            # not be judging the same input.
            jobs = [dict(d) for d in sample]
            # Per ARM, not per run: the rollup is a module-level accumulator, so
            # without this the second arm reports the first arm's tokens too.
            fa.reset_llm_usage()
            res = _run_arm(fa, name, system_prompt, jobs, cluster_profile, cv_text,
                           llm_calls, args.dry_run)
            arm_raw[name] = res
            picks = list(res["strong"]) + list(res["backup"])
            per_job = {}
            for entry in picks:
                ident = entry.get("_identity")
                if not ident:
                    continue
                per_job[ident] = _score_checklist(entry, blind_by_ident.get(ident) or [],
                                                  hint_by_ident.get(ident) or [])
            arm_results[name] = per_job
            arm_metrics[name] = _aggregate(per_job)
            m = arm_metrics[name]
            print(f"  picks: {len(res['strong'])} strong + {len(res['backup'])} backup, "
                  f"{len(res['excluded'])} excluded  ({res['seconds']}s)")
            print(f"  EXTRACTION  checklist items mean {m.get('checklist_items_mean')} "
                  f"(core {m.get('core_mean')} / secondary {m.get('secondary_mean')})")
            print(f"              required-ask coverage {m.get('required_coverage_pct')}% "
                  f"({m.get('required_asks_total', 0) - m.get('required_asks_missed', 0)}"
                  f"/{m.get('required_asks_total')}), "
                  f"required-but-tagged-secondary {m.get('required_asks_mistiered_secondary')}")
            print(f"              of those misses: {m.get('required_missed_though_in_hint')} were "
                  f"IN the hint (judge dropped it), "
                  f"{m.get('required_missed_and_not_in_hint')} were not "
                  f"(judge failed to ADD from the full text)")
            print(f"              rows with a domain-label item: {m.get('domain_label_rows')}")
            print(f"  PADDING     unfailable items {m.get('unfailable_items_total')} "
                  f"({m.get('unfailable_core_total')} of them core), "
                  f"{m.get('unfailable_per_checklist')} per checklist")
            print(f"  SELECTION   grades {m.get('grades')}, concerns mean {m.get('concerns_mean')}")
            # The output-budget side of the same question. `length_capped` is the
            # only thing that separates "the model wrote less" from "we truncated
            # it" -- see full_auto._record_llm_usage. `completion/pick` is the
            # number that tracks checklist size across production runs, so it is
            # what a chunk-size change is trying to move.
            jrow = (fa.llm_usage_snapshot() or {}).get("judge") or {}
            n_picks = max(1, len(res["strong"]) + len(res["backup"]))
            pt, ct = jrow.get("prompt_tokens", 0), jrow.get("cached_tokens", 0)
            print(f"  BUDGET      {jrow.get('calls', 0)} judge call(s) "
                  f"(jobs/call cap {fa.FINAL_EVAL_MAX_JOBS_PER_CALL}), "
                  f"{jrow.get('completion_tokens', 0)} completion tokens "
                  f"= {jrow.get('completion_tokens', 0) // n_picks}/pick"
                  f" | cut off by an output ceiling: {jrow.get('length_capped', 0)}")
            # The COST of splitting into more calls: each one re-pays the ~12k
            # system prefix, and the whole argument for splitting rests on that
            # prefix coming back from the prompt cache. If this ratio falls as
            # the cap comes down, the extra calls are being billed in full.
            print(f"              prompt {pt} tokens, {ct} from cache "
                  f"({(100.0 * ct / pt):.0f}% hit)" if pt else "              prompt 0 tokens")
            arm_metrics[name]["judge_calls"] = jrow.get("calls", 0)
            arm_metrics[name]["completion_tokens"] = jrow.get("completion_tokens", 0)
            arm_metrics[name]["completion_per_pick"] = jrow.get("completion_tokens", 0) // n_picks
            arm_metrics[name]["length_capped"] = jrow.get("length_capped", 0)
            arm_metrics[name]["seconds"] = res["seconds"]

        if len(arms) == 2:
            print("\n=== paired delta (candidate - baseline) ===")
            b, c = arm_metrics.get("baseline", {}), arm_metrics.get("candidate", {})
            for k in ("checklist_items_mean", "core_mean", "secondary_mean",
                      "required_coverage_pct", "required_missed_though_in_hint",
                      "required_missed_and_not_in_hint", "required_asks_mistiered_secondary",
                      "domain_label_rows", "unfailable_items_total", "unfailable_core_total",
                      "concerns_mean"):
                bv, cv_ = b.get(k), c.get(k)
                if isinstance(bv, (int, float)) and isinstance(cv_, (int, float)):
                    print(f"  {k:38} {bv:>7} -> {cv_:>7}   ({cv_ - bv:+.2f})")
            print(f"  {'picks shown':38} "
                  f"{len(arm_raw['baseline']['strong']) + len(arm_raw['baseline']['backup']):>7} -> "
                  f"{len(arm_raw['candidate']['strong']) + len(arm_raw['candidate']['backup']):>7}")
            print(f"  {'grades':38} {b.get('grades')} -> {c.get('grades')}")

        out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "tests" / "judge_reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile.id,
            "judge_model": fa.EXP_MODEL,
            "final_eval_prompt_version": fa.FINAL_EVAL_PROMPT_VERSION,
            "args": vars(args),
            "sample": [{"identity": d.get("_identity"), "title": d.get("title"),
                        "company": d.get("company"),
                        "prior_verdict": d.get("_eval_verdict"),
                        "text_chars": len(d.get("full_text") or d.get("snippet") or ""),
                        "blind_asks": blind_by_ident.get(d.get("_identity")) or [],
                        "hint_asks": hint_by_ident.get(d.get("_identity")) or []}
                       for d in sample],
            "metrics": arm_metrics,
            "per_job": {arm: {k: {kk: vv for kk, vv in v.items() if kk != "items"}
                              for k, v in jobs.items()}
                        for arm, jobs in arm_results.items()},
            "checklists": {arm: {k: v.get("items") for k, v in jobs.items()}
                           for arm, jobs in arm_results.items()},
            "llm_calls": llm_calls,
        }
        path = out_dir / f"judge_harness_{stamp}.json"
        path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nreport: {path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
