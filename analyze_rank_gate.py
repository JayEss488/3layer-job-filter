"""Read-only-ish diagnostic report for the medium-tier (MID_MODEL / rank_gate)
numeric fit-scoring stage of the search pipeline (see CLAUDE.md's "search
pipeline" section, step 2, `full_auto.rank_gate`).

rank_gate's 0-100 fit_score per job is cached in gate_cache (boards_cache.db)
under gate="rank_v2", keyed by SHA1(gate|profile_signature|job_id) -- a
one-way hash. Recomputing that key against the CURRENT profile signature and
hoping for a cache hit was tried first and abandoned: every signature-relevant
field except `sectors` is deterministic (read straight off ProfileAttribute
rows), but `sectors` is itself an LLM call (build_snapshot's _infer_region,
temperature 0.2) that reliably returns a DIFFERENT phrasing each time it's
re-run -- 14 back-to-back build_snapshot() calls against this repo's own
profile 11 produced 6 distinct sector-list variants and zero cache hits, so
the exact historical score genuinely isn't recoverable byte-for-byte after
the fact.

Instead this script mirrors what the real pipeline does, at a small bounded
cost, and reports REAL, freshly-computed scores rather than guessing at old
ones:
  1. build_snapshot()               -- 1 cheap-model call (sector inference)
  2. embed the 1-3 role-cluster texts -- 1 embeddings call (batched)
  3. cosine-score every already-embedded JobSeen row locally (free) to get
     each job's real cluster assignment + embed_score, exactly like
     engine.py's _score_rows
  4. take the top ~15 embed-ranked jobs per cluster (plus this run's actual
     final picks, guaranteed included) and run them through the REAL
     `full_auto.rank_gate()` -- at most one MID_MODEL batch call per cluster
     (_GATE_BATCH=20), NOT a full search: no discovery, no scraping, no
     screen_gate, no final judge.

This is a small, bounded, real API cost (a handful of cheap/embedding calls
plus 1-2 MID_MODEL batch calls) -- much less than a full end-to-end search,
but it is not free. Scores shown are what the medium model rates these
listings RIGHT NOW, not necessarily bit-identical to what it said during the
historical run (screen_gate/hard-drop decisions from that run are untouched;
this only re-derives the numeric rank score for visibility).

Usage:
    venv/Scripts/python analyze_rank_gate.py --profile-id 11
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile-id", type=int, default=None,
                    help="Defaults to the highest-numbered profile.")
    p.add_argument("--run-id", type=int, default=None,
                    help="Which SearchRun to cross-reference for final picks (default: latest 'done' run).")
    p.add_argument("--sample-per-cluster", type=int, default=15,
                    help="How many real, embed-ranked listings per cluster to send through a live "
                         "rank_gate call (default 15; capped to fit in one _GATE_BATCH=20 call/cluster).")
    p.add_argument("--out", default=None,
                    help="Output JSON path (default: .embedding_analysis/rank_report_<timestamp>.json)")
    return p.parse_args()


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()

    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))

    import app.database as database
    import app.models as models
    from app.services import snapshot
    from app.services import engine as engine_svc
    import full_auto as engine  # noqa: E402 -- after app.* import so .env is loaded

    db = database.SessionLocal()

    try:
        profile_id = args.profile_id
        if profile_id is None:
            profile_id = db.query(models.Profile.id).order_by(models.Profile.id.desc()).first()[0]
        profile = db.get(models.Profile, profile_id)
        if profile is None:
            raise SystemExit(f"profile {profile_id} not found")
        print(f"[analyze] profile {profile_id} ({profile.name!r})")

        run_q = db.query(models.SearchRun).filter(
            models.SearchRun.profile_id == profile_id, models.SearchRun.status == "done"
        )
        run = (run_q.filter(models.SearchRun.id == args.run_id).first() if args.run_id
               else run_q.order_by(models.SearchRun.id.desc()).first())
        if run is None:
            raise SystemExit(f"no completed run found for profile {profile_id}")
        print(f"[analyze] cross-referencing run {run.id} "
              f"({run.started_at} -> {run.finished_at}, {run.result_count} results)")

        # ── Step 1: real snapshot (1 cheap call) ────────────────────────────────
        snap = snapshot.build_snapshot(db, profile_id)
        eng_profile = snap["engine_profile"]
        role_clusters = snap["role_clusters"]
        print(f"[analyze] seniority={eng_profile.get('seniority')!r} sectors={eng_profile.get('sectors')} | "
              f"{len(role_clusters)} role cluster(s): "
              + "; ".join(f"{c['label']}={c['roles']}" for c in role_clusters))

        cluster_profiles = []
        for idx, rc in enumerate(role_clusters):
            cp = dict(eng_profile)
            cp["search_terms"] = rc.get("roles") or eng_profile.get("search_terms")
            cp["_multi_cluster"] = len(role_clusters) > 1
            cluster_profiles.append(cp)

        # ── Step 2+3: real cluster embeddings + free local cosine scoring ───────
        cluster_texts = [c["weighted_text"] for c in role_clusters]
        cluster_embeddings = engine.get_embeddings_batch(cluster_texts)
        print(f"[analyze] embedded {len(cluster_texts)} cluster text(s) via {engine.EMBED_MODEL}")

        all_rows = db.query(models.JobSeen).filter(models.JobSeen.profile_id == profile_id).all()
        rows_with_embedding = [r for r in all_rows if r.embedding]
        print(f"[analyze] {len(rows_with_embedding)}/{len(all_rows)} jobs_seen rows have a cached embedding")
        scored = engine_svc._score_rows(engine, rows_with_embedding, cluster_embeddings)

        # ── Final picks from the cross-referenced run, for guaranteed inclusion ─
        roles = (
            db.query(models.Role)
            .filter(models.Role.search_run_id == run.id)
            .order_by(models.Role.fit_rank)
            .all()
        )
        final_pick_urls = {r.url for r in roles if r.url}

        # ── Step 4: bounded real sample per cluster -> live rank_gate() call ────
        by_cluster: dict[int, list[dict]] = {i: [] for i in range(len(role_clusters))}
        for d in scored:
            by_cluster.setdefault(d["_cluster"], []).append(d)

        sample_by_cluster: dict[int, list[dict]] = {}
        for idx, jobs in by_cluster.items():
            jobs_sorted = sorted(jobs, key=lambda d: d["embed_score"], reverse=True)
            top = jobs_sorted[:args.sample_per_cluster]
            top_urls = {d["url"] for d in top}
            forced = [d for d in jobs_sorted if d["url"] in final_pick_urls and d["url"] not in top_urls]
            sample_by_cluster[idx] = top + forced

        total_sample = sum(len(v) for v in sample_by_cluster.values())
        print(f"[analyze] live-scoring {total_sample} real listing(s) across {len(sample_by_cluster)} "
              f"cluster(s) via {engine.MID_MODEL} (bounded, real API cost -- not a full search)")

        matched = []
        for idx, jobs in sample_by_cluster.items():
            if not jobs:
                continue
            ranked = engine.rank_gate(list(jobs), cluster_profiles[idx])
            for d in ranked:
                matched.append({
                    "cluster_idx": idx,
                    "cluster_label": role_clusters[idx]["label"],
                    "title": d["title"],
                    "company": d.get("company") or "",
                    "location": d.get("location") or "",
                    "source": d.get("board") or "",
                    "url": d.get("url") or "",
                    "snippet": (d.get("snippet") or "")[:450],
                    "embed_score": round(d.get("embed_score", 0.0), 4),
                    "rank_score": round(d.get("_rank_score", 50.0), 1),
                    "rank_gate_failed": bool(d.get("_rank_gate_failed")),
                    "eval_verdict": d.get("_eval_verdict"),
                    "is_final_pick": d.get("url") in final_pick_urls,
                })
        matched.sort(key=lambda m: m["rank_score"], reverse=True)
        print(f"[analyze] scored {len(matched)} real listings")

        matched_by_url = {m["url"]: m for m in matched if m["url"]}
        final_picks = []
        for role in roles:
            m = matched_by_url.get(role.url)
            final_picks.append({
                "title": role.title, "company": role.company, "fit_rank": role.fit_rank,
                "verdict": role.verdict, "source": role.source,
                "rank_score": m["rank_score"] if m else None,
                "cluster_label": m["cluster_label"] if m else None,
            })

        # ── Reconstruct the exact prompt sent to MID_MODEL, with the real batch ─
        sample_prompts = []
        for idx, jobs in sample_by_cluster.items():
            cluster_matches = [m for m in matched if m["cluster_idx"] == idx]
            if not cluster_matches:
                continue
            listing_block = "\n".join(
                f"{i+1}. {m['title']} @ {m['company']} | "
                f"{m['location'] or 'location unknown'} | {m['snippet']}"
                for i, m in enumerate(cluster_matches)
            )
            prompt_text = engine._rank_prompt(cluster_profiles[idx], listing_block)
            sample_prompts.append({
                "cluster_idx": idx,
                "cluster_label": role_clusters[idx]["label"],
                "n_sampled": len(cluster_matches),
                "prompt": prompt_text,
            })

        def score_stats(items):
            if not items:
                return None
            scores = [m["rank_score"] for m in items]
            return {
                "n": len(scores), "min": min(scores), "max": max(scores),
                "avg": round(sum(scores) / len(scores), 1),
                "median": sorted(scores)[len(scores) // 2],
            }

        hist_buckets = list(range(0, 101, 10))
        histogram = [0] * (len(hist_buckets) - 1)
        for m in matched:
            b = min(int(m["rank_score"] // 10), 9)
            histogram[b] += 1

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile_id,
            "profile_name": profile.name,
            "run_id": run.id,
            "run_window": {"started_at": str(run.started_at), "finished_at": str(run.finished_at)},
            "run_funnel_counts": json.loads(run.funnel_counts or "{}"),
            "run_phase_timings": json.loads(run.phase_timings or "{}"),
            "mid_model": engine.MID_MODEL,
            "role_clusters": [{"label": c["label"], "roles": c["roles"]} for c in role_clusters],
            "profile_context": {
                "sectors": eng_profile.get("sectors"),
                "seniority": eng_profile.get("seniority"),
                "key_skills": eng_profile.get("key_skills"),
                "target_role_weight_tiers": eng_profile.get("target_role_weight_tiers"),
                "skill_weight_tiers": eng_profile.get("skill_weight_tiers"),
                "skill_evidence_tiers": eng_profile.get("skill_evidence_tiers"),
            },
            "live_sample_note": (
                "Scores below are freshly computed via a real, bounded rank_gate() call over "
                f"{total_sample} embed-ranked real listings (<= {args.sample_per_cluster}/cluster + "
                "this run's final picks) -- not a byte-exact replay of the historical run's numbers, "
                "see module docstring."
            ),
            "overall_stats": score_stats(matched),
            "per_cluster_stats": {
                role_clusters[i]["label"]: score_stats([m for m in matched if m["cluster_idx"] == i])
                for i in range(len(role_clusters))
            },
            "histogram_buckets": hist_buckets,
            "histogram_counts": histogram,
            "final_picks": final_picks,
            "sample_prompts": sample_prompts,
            "matched_jobs": matched,
        }

        out_path = Path(args.out) if args.out else (
            ROOT / ".embedding_analysis" / f"rank_report_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)
        print(f"[analyze] wrote report to {out_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
