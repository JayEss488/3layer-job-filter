"""Marginal-value analysis of the THREE AI tiers, scored against the same labelled set.

Answers the question a single-stage harness can't: what does each tier actually add
over the one before it, and is any of them carrying its cost?

  weak   screen_gate           CHEAP_MODEL   binary axes, 2000-char window
  medium rank_gate             MID_MODEL     0-100 fit score, 3000-char window
  strong final_evaluation      EXP_MODEL     verdict + reasoning, full text

Method. Sample listings the strong judge has ALREADY ruled on (JobSeen.eval_verdict,
balanced across verdicts), then run the weak and medium tiers live over that same
sample and compare all three. The judge side costs nothing -- its verdicts are the
labels, read straight off the rows -- so the whole run is 6 CHEAP + 6 MID calls at
--sample-size 113. Nothing is written to gate_cache (read AND write bypassed, same
as gate_harness.py) and no Role/SearchRun rows are created.

Two things make this different from analyze_rank_gate.py / analyze_medium_vs_strong.py,
which predate the ground-truth method:
  * rank_gate is run over EVERY sampled listing, not just gate survivors. Measuring
    what the gate adds requires knowing what the medium tier would have said about
    the listings the gate removed -- otherwise the gate looks load-bearing purely
    because nothing downstream ever got to disagree with it.
  * Every disagreement is checked for whether the disqualifying text was inside the
    tier's own window (_disqualifier_visibility, shared with gate_harness so the two
    reports can't drift apart on what "could have known" means). A tier that misses
    something it could not read is not miscalibrated, and prompt work will not fix it.

The text-supply caveat from gate_harness applies here too and matters MORE, because
the tiers have different windows: a judged row has been scraped, so it carries a
full_text neither cheap tier had at the time. --text-mode snippet is the honest
production comparison; --text-mode full measures the ceiling if text were supplied.

Usage:
    venv/Scripts/python tests/tier_analysis.py --profile-id 1 --sample-size 113
    venv/Scripts/python tests/tier_analysis.py --profile-id 1 --text-mode full
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gate_harness import (  # noqa: E402
    _apply_text_mode, _attach_ground_truth, _build_cluster_profile,
    _disqualifier_visibility, _install_capture_hooks, _resolve_profile,
    _sample_jobs, _score_sampled_rows,
)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile-id", type=int, default=None)
    p.add_argument("--sample-size", type=int, default=113)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--text-mode", choices=("auto", "snippet", "full"), default="snippet")
    p.add_argument("--population-check", type=int, default=0, metavar="N",
                    help="Additionally draw N RANDOM stored listings (not judge-labelled), push "
                         "them through the embedding floor and the gate, and report rank_gate's "
                         "score distribution over the survivors. Needed because the labelled "
                         "sample is range-restricted -- every row in it already passed the rank "
                         "floor once, so the floor looks toothless there whatever it really does. "
                         "Suggested N=600, which yields roughly 40 survivors.")
    p.add_argument("--out-dir", default=None)
    return p.parse_args()


def population_check(db, models, engine, engine_svc, snapshot_mod, profile_id, n, seed):
    """rank_gate's score distribution over a PRODUCTION-SHAPED intake.

    The ground-truth sample cannot answer "is RANK_REJECT_SCORE_FLOOR doing
    anything?" -- it contains only rows that already cleared that floor on an
    earlier run, so almost nothing in it can score low, and the floor will look
    like a no-op however well it is set. This draws random stored listings
    instead and applies the real upstream filters (RELEVANCE_FLOOR, then the
    gate) before scoring, so the resulting distribution is the one the floor
    actually sees. Measured live: on the labelled set only 2 of 89 survivors fell
    below 50; here it is closer to a third."""
    import random as _random
    rows = db.query(models.JobSeen).filter(
        models.JobSeen.profile_id == profile_id,
        models.JobSeen.dead_reason.is_(None),
    ).all()
    if not rows:
        return None
    sample = _random.Random(seed).sample(rows, min(n, len(rows)))
    snap = snapshot_mod.build_snapshot(db, profile_id)
    rc, ep = snap["role_clusters"], snap["engine_profile"]
    if not rc:
        return None
    cemb = engine.get_embeddings_batch([c["weighted_text"] for c in rc])
    scored = _score_sampled_rows(engine_svc, engine, sample, cemb)
    _apply_text_mode(engine, scored, "snippet")
    eligible = [d for d in scored if (d.get("embed_score") or 0) >= engine_svc.RELEVANCE_FLOOR]
    if not eligible:
        return None
    cp = _build_cluster_profile(ep, rc, 0)
    engine.screen_gate(eligible, cp)
    soft, hard = engine_svc._hard_enforced_axes(engine, cp)
    in_sector = [j for j in eligible
                 if j.get("_sector_ok", True) and j.get("_hard_gate_ok", True)
                 and j.get("_listing_ok", True) and all(j.get(a, True) for a in hard)]
    counts = [sum(1 for a in soft if not j.get(a, True)) for j in in_sector]
    thr = engine.dynamic_hard_drop_threshold(counts)
    survivors = [j for j, c in zip(in_sector, counts) if c < thr]
    if not survivors:
        return None
    engine.rank_gate(survivors, cp)
    scores = sorted(float(j.get("_rank_score", 50.0)) for j in survivors)
    return {
        "sampled": len(sample), "above_relevance_floor": len(eligible),
        "gate_survivors": len(survivors),
        "distribution": _dist(scores),
        "below_threshold": {str(t): sum(1 for s in scores if s < t)
                            for t in (40, 50, 55, 60, 65, 70)},
    }


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else None


def _auc(pos_scores, neg_scores):
    """Probability a randomly chosen judge-approved listing outranks a randomly
    chosen judge-rejected one, under this tier's score. 0.5 is a coin flip (the
    score carries no information about the judge's verdict); 1.0 is perfect
    separation. Reported instead of accuracy-at-a-threshold because the threshold
    is a separate, tunable decision -- this measures whether the ORDERING is
    informative at all, which is what the stage is actually for. Computed directly
    (count of concordant pairs, ties at half) rather than via a trapezoid over a
    ROC curve, so ties -- and this stage produces many, it emits round numbers --
    are handled explicitly instead of silently."""
    if not pos_scores or not neg_scores:
        return None
    wins = ties = 0
    for p in pos_scores:
        for n in neg_scores:
            if p > n:
                wins += 1
            elif p == n:
                ties += 1
    return round((wins + 0.5 * ties) / (len(pos_scores) * len(neg_scores)), 3)


def _dist(scores):
    if not scores:
        return None
    s = sorted(scores)
    return {
        "n": len(s), "min": min(s), "max": max(s),
        "median": statistics.median(s),
        "mean": round(statistics.fmean(s), 1),
        "p25": s[len(s) // 4], "p75": s[(3 * len(s)) // 4],
    }


def analyse(records, engine, engine_svc):
    """All the tier comparisons. `records` carry gate + rank + judge for one listing."""
    floor = engine_svc.RANK_REJECT_SCORE_FLOOR
    strong = [r for r in records if r["judge_verdict"] == "strong"]
    reject = [r for r in records if r["judge_verdict"] == "reject"]

    # --- separation: does each tier's signal order the judge's verdicts? ---
    separation = {
        "rank_auc_all": _auc([r["rank_score"] for r in strong],
                             [r["rank_score"] for r in reject]),
        "embed_auc_all": _auc([r["embed_score"] for r in strong if r["embed_score"] is not None],
                              [r["embed_score"] for r in reject if r["embed_score"] is not None]),
        "rank_scores_strong": _dist([r["rank_score"] for r in strong]),
        "rank_scores_reject": _dist([r["rank_score"] for r in reject]),
    }
    survivors = [r for r in records if r["gate_kept"]]
    sv_strong = [r for r in survivors if r["judge_verdict"] == "strong"]
    sv_reject = [r for r in survivors if r["judge_verdict"] == "reject"]
    separation["rank_auc_among_gate_survivors"] = _auc(
        [r["rank_score"] for r in sv_strong], [r["rank_score"] for r in sv_reject])

    # --- what the MEDIUM tier adds over the weak gate ---
    # Only listings the gate passed can be caught by rank in production.
    rank_drops_sv = [r for r in survivors if r["rank_score"] < floor]
    medium_adds = {
        "gate_survivors": len(survivors),
        "of_which_judge_reject": len(sv_reject),
        "of_which_judge_strong": len(sv_strong),
        "rank_floor": floor,
        "rank_drops_at_floor": len(rank_drops_sv),
        "rank_drops_that_judge_rejected": sum(1 for r in rank_drops_sv
                                              if r["judge_verdict"] == "reject"),
        "rank_drops_that_judge_liked": sum(1 for r in rank_drops_sv
                                           if r["judge_verdict"] == "strong"),
        "rank_kills_detail": [
            {"title": r["title"], "company": r["company"], "rank_score": r["rank_score"],
             "rank_note": r["rank_note"], "judge_verdict": r["judge_verdict"],
             "judge_reason": r["judge_reason"], "url": r["url"]}
            for r in rank_drops_sv
        ],
    }

    # --- what the WEAK gate adds over the medium tier ---
    # A gate drop is only load-bearing if rank would NOT have removed it anyway.
    gate_drops = [r for r in records if not r["gate_kept"]]
    redundant = [r for r in gate_drops if r["rank_score"] < floor]
    weak_adds = {
        "gate_drops": len(gate_drops),
        "rank_would_also_have_dropped": len(redundant),
        "rank_would_have_kept": len(gate_drops) - len(redundant),
        "unique_gate_catches": [
            {"title": r["title"], "company": r["company"], "rank_score": r["rank_score"],
             "judge_verdict": r["judge_verdict"], "gate_axes_failed": r["gate_axes_failed"],
             "url": r["url"]}
            for r in gate_drops if r["rank_score"] >= floor
        ],
    }

    # --- what the STRONG judge adds over the medium tier ---
    # The judge's real input is the top JUDGE_POOL of the rank-ordered survivors.
    pool = sorted([r for r in survivors if r["rank_score"] >= floor],
                  key=lambda r: -r["rank_score"])[:engine_svc.JUDGE_POOL]
    pool_reject = [r for r in pool if r["judge_verdict"] == "reject"]
    vis = Counter()
    for r in pool_reject:
        v = r["rank_visibility"]
        if v is None:
            vis["no_quoted_requirement"] += 1
        elif v["in_gate_text"]:          # here: inside RANK's own window
            vis["visible_to_rank_but_scored_high"] += 1
        elif v["in_full_text"]:
            vis["only_in_scraped_text"] += 1
        else:
            vis["not_found_in_any_text"] += 1
    strong_adds = {
        "judge_pool_size": len(pool),
        "pool_judge_strong": sum(1 for r in pool if r["judge_verdict"] == "strong"),
        "pool_judge_reject": len(pool_reject),
        "pool_precision_pct": _pct(sum(1 for r in pool if r["judge_verdict"] == "strong"), len(pool)),
        "rejects_in_pool_visibility": dict(vis),
        "high_scored_rejects": [
            {"title": r["title"], "company": r["company"], "rank_score": r["rank_score"],
             "rank_note": r["rank_note"], "judge_reason": r["judge_reason"],
             "visibility": r["rank_visibility"], "rank_text_chars": r["rank_text_chars"],
             "url": r["url"]}
            for r in sorted(pool_reject, key=lambda r: -r["rank_score"])
        ],
    }

    # --- could the medium tier stand in for the judge? ---
    # Sweep the threshold rather than asserting one: the question is whether ANY
    # cut on this score reproduces the judge's strong/reject split well enough to
    # drop a tier, and the sweep shows plainly that it doesn't rather than resting
    # on one arbitrarily chosen number.
    sweep = []
    for t in range(30, 100, 5):
        kept_strong = sum(1 for r in strong if r["rank_score"] >= t)
        kept_reject = sum(1 for r in reject if r["rank_score"] >= t)
        sweep.append({
            "threshold": t,
            "strong_kept": kept_strong, "strong_kept_pct": _pct(kept_strong, len(strong)),
            "reject_kept": kept_reject, "reject_kept_pct": _pct(kept_reject, len(reject)),
            "precision_pct": _pct(kept_strong, kept_strong + kept_reject),
        })

    return {
        "n": len(records), "n_strong": len(strong), "n_reject": len(reject),
        "separation": separation,
        "medium_over_weak": medium_adds,
        "weak_over_medium": weak_adds,
        "strong_over_medium": strong_adds,
        "rank_threshold_sweep": sweep,
    }


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))

    import app.database as database
    import app.models as models
    from app.services import snapshot as snapshot_mod
    from app.services import engine as engine_svc
    import full_auto as engine  # noqa: E402

    db = database.SessionLocal()
    try:
        profile = _resolve_profile(db, models, args.profile_id)
        rows, n_available = _sample_jobs(db, models, profile.id, args.sample_size,
                                         args.seed, ground_truth=True)
        print(f"[tier_analysis] profile {profile.id} ({profile.name!r}); "
              f"{len(rows)} of {n_available} judge-labelled listings; text-mode={args.text_mode}")

        snap = snapshot_mod.build_snapshot(db, profile.id)
        role_clusters, eng_profile = snap["role_clusters"], snap["engine_profile"]
        cluster_embeddings = engine.get_embeddings_batch(
            [c["weighted_text"] for c in role_clusters]) if role_clusters else []
        if not role_clusters:
            raise SystemExit("profile has no target-role clusters.")

        scored = _score_sampled_rows(engine_svc, engine, rows, cluster_embeddings)
        _apply_text_mode(engine, scored, args.text_mode)
        _attach_ground_truth(scored)

        n_batches = -(-len(scored) // engine._GATE_BATCH)
        print(f"[tier_analysis] LIVE: ~{n_batches} {engine.CHEAP_MODEL} call(s) + "
              f"~{n_batches} {engine.MID_MODEL} call(s). The judge tier costs nothing "
              f"(its verdicts are the labels).")

        llm_calls: list = []
        _install_capture_hooks(engine, llm_calls)

        by_cluster = defaultdict(list)
        for d in scored:
            by_cluster[d.get("_cluster", 0)].append(d)

        for idx, cluster in enumerate(role_clusters):
            jobs = by_cluster.get(idx, [])
            if not jobs:
                continue
            cp = _build_cluster_profile(eng_profile, role_clusters, idx)
            print(f"\n[{cluster['label']}] {len(jobs)} listing(s) -> screen_gate ...")
            engine.screen_gate(jobs, cp)
            # Deliberately over the WHOLE cluster sample, not just gate survivors --
            # see the module docstring: the gate's own contribution can't be measured
            # without knowing what the medium tier thought of what it removed.
            print(f"[{cluster['label']}] {len(jobs)} listing(s) -> rank_gate ...")
            engine.rank_gate(jobs, cp)

        soft_axes_by_cluster = {
            idx: engine_svc._hard_enforced_axes(engine, _build_cluster_profile(eng_profile, role_clusters, idx))
            for idx in range(len(role_clusters))
        }

        records = []
        for idx, jobs in by_cluster.items():
            soft_axes, hard_axes = soft_axes_by_cluster[idx]
            # Mirror engine.py's real drop logic for this cluster, exactly as
            # gate_harness does, so "gate_kept" here means what it means in a run.
            in_sector = [j for j in jobs
                         if j.get("_sector_ok", True) and j.get("_hard_gate_ok", True)
                         and j.get("_listing_ok", True)
                         and all(j.get(a, True) for a in hard_axes)]
            counts = [sum(1 for a in soft_axes if not j.get(a, True)) for j in in_sector]
            threshold = engine.dynamic_hard_drop_threshold(counts)
            kept = {id(j) for j, n in zip(in_sector, counts) if n < threshold}
            for j in jobs:
                rank_text = (j.get("full_text") or j.get("snippet") or "")[:engine.RANK_LISTING_TEXT_CHARS]
                records.append({
                    "identity": j.get("_identity"),
                    "title": j.get("title"), "company": j.get("company"), "url": j.get("url"),
                    "cluster": role_clusters[idx].get("label"),
                    "embed_score": j.get("embed_score"),
                    "gate_kept": id(j) in kept,
                    "gate_unjudged": bool(j.get("_gate_unjudged")),
                    "gate_axes_failed": [a.lstrip("_") for a in
                                         ("_sector_ok", "_hard_gate_ok", "_listing_ok") + engine.SOFT_GATE_AXES
                                         if j.get(a, True) is False],
                    "rank_score": float(j.get("_rank_score", 50.0)),
                    "rank_note": j.get("_rank_note") or "",
                    "rank_text_chars": len(rank_text),
                    "judge_verdict": j.get("_eval_verdict"),
                    "judge_reason": j.get("_judge_reason"),
                    # Visibility recomputed against RANK's own (larger) window --
                    # the gate-window answer would understate what rank could see.
                    "rank_visibility": _disqualifier_visibility(
                        j.get("_judge_reason"), rank_text, j.get("snippet") or "",
                        j.get("_full_text_original") or ""),
                })

        result = analyse(records, engine, engine_svc)
        _print(result, engine_svc)

        pop = None
        if args.population_check:
            print(f"\n[tier_analysis] population check: {args.population_check} random listings "
                  f"through the real embedding floor + gate, then rank_gate ...")
            pop = population_check(db, models, engine, engine_svc, snapshot_mod,
                                   profile.id, args.population_check, args.seed)
            if pop:
                d = pop["distribution"]
                print(f"  {pop['sampled']} sampled -> {pop['above_relevance_floor']} above "
                      f"RELEVANCE_FLOOR -> {pop['gate_survivors']} gate survivors")
                print(f"  rank scores: min {d['min']:.0f} p25 {d['p25']:.0f} "
                      f"median {d['median']:.0f} p75 {d['p75']:.0f} max {d['max']:.0f}")
                for t, below in pop["below_threshold"].items():
                    print(f"    below {t}: {below} of {pop['gate_survivors']} "
                          f"({_pct(below, pop['gate_survivors'])}%)")

        out_dir = Path(args.out_dir) if args.out_dir else (ROOT / "tests" / "tier_reports")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now(timezone.utc):%Y%m%d_%H%M%S}"
        path = out_dir / f"tiers_{stamp}.json"
        path.write_text(json.dumps({
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile.id, "text_mode": args.text_mode,
            "models": {"weak": engine.CHEAP_MODEL, "medium": engine.MID_MODEL,
                       "strong": engine.EXP_MODEL},
            "constants": {"RANK_REJECT_SCORE_FLOOR": engine_svc.RANK_REJECT_SCORE_FLOOR,
                          "JUDGE_POOL": engine_svc.JUDGE_POOL,
                          "GATE_LISTING_TEXT_CHARS": engine.GATE_LISTING_TEXT_CHARS,
                          "RANK_LISTING_TEXT_CHARS": engine.RANK_LISTING_TEXT_CHARS},
            "analysis": result, "population_check": pop, "jobs": records,
        }, indent=1), encoding="utf-8")
        print(f"\n[tier_analysis] wrote {path}")
    finally:
        db.close()


def _print(r, engine_svc):
    s, mw, wm, sm = (r["separation"], r["medium_over_weak"],
                     r["weak_over_medium"], r["strong_over_medium"])
    print(f"\n=== {r['n']} labelled listings: {r['n_strong']} strong, {r['n_reject']} reject ===")
    print("\n-- Does each tier's signal track the judge? (AUC; 0.5 = no information) --")
    print(f"  embedding cosine        {s['embed_auc_all']}")
    print(f"  rank score, all         {s['rank_auc_all']}")
    print(f"  rank score, gate survivors only  {s['rank_auc_among_gate_survivors']}")
    print(f"  rank score on judge-strong: {s['rank_scores_strong']}")
    print(f"  rank score on judge-reject: {s['rank_scores_reject']}")

    print(f"\n-- What MEDIUM adds over the weak gate --")
    print(f"  gate passed {mw['gate_survivors']} ({mw['of_which_judge_strong']} strong, "
          f"{mw['of_which_judge_reject']} reject)")
    print(f"  rank floor ({mw['rank_floor']}) then removes {mw['rank_drops_at_floor']}: "
          f"{mw['rank_drops_that_judge_rejected']} the judge rejected, "
          f"{mw['rank_drops_that_judge_liked']} the judge liked")

    print(f"\n-- What the WEAK gate adds over medium --")
    print(f"  gate dropped {wm['gate_drops']}; rank would also have dropped "
          f"{wm['rank_would_also_have_dropped']}, would have KEPT {wm['rank_would_have_kept']}")

    print(f"\n-- What STRONG adds over medium --")
    print(f"  judge pool: {sm['judge_pool_size']} ({sm['pool_judge_strong']} strong, "
          f"{sm['pool_judge_reject']} reject) -> precision {sm['pool_precision_pct']}%")
    for k, v in sorted(sm["rejects_in_pool_visibility"].items(), key=lambda kv: -kv[1]):
        print(f"     {v:>3}  {k}")

    print(f"\n-- Could a rank threshold replace the judge? --")
    print(f"  {'thresh':>7}{'strong kept':>14}{'reject kept':>14}{'precision':>11}")
    for row in r["rank_threshold_sweep"]:
        print(f"  {row['threshold']:>7}{str(row['strong_kept'])+' ('+str(row['strong_kept_pct'])+'%)':>14}"
              f"{str(row['reject_kept'])+' ('+str(row['reject_kept_pct'])+'%)':>14}"
              f"{str(row['precision_pct'])+'%':>11}")


if __name__ == "__main__":
    main()
