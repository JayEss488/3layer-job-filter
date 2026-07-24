"""Read-only diagnostic report for the embedding-score + heuristic-prescreen
stage of the search pipeline (see CLAUDE.md's "search pipeline" section,
step 2, up through `_heuristic_prescreen`).

Neither `embed_score` nor the heuristic verdict is persisted anywhere per
job -- they're computed transiently inside one `_run_engine_pipeline` call
and thrown away. This script re-derives both for every already-discovered
job cached on `JobSeen`, without running a real search: no discovery, no
Phase 5 scraping, no screen_gate/rank_gate/final-judge calls, and no
SearchRun/Role rows created.

Cost: job embeddings are cached forever on `JobSeen.embedding` the first
time a job is ever discovered, and re-scoring them against a cluster
embedding is pure local cosine math (free). The only real API calls are the
ones `build_snapshot`/cluster-embedding setup always makes: one cheap-model
region-inference call, and one embeddings-API call for the profile's 1-3
role-cluster texts. Both are negligible next to a full search run.

Usage:
    venv/Scripts/python analyze_embedding_gate.py
    venv/Scripts/python analyze_embedding_gate.py --profile-id 7 --sample-size 150

Writes a JSON report to --out (default: a timestamped file in the repo's
.embedding_analysis/ dir).
"""
import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile-id", type=int, default=None,
                    help="Defaults to the single active profile.")
    p.add_argument("--sample-size", type=int, default=100,
                    help="How many jobs to include in the detailed row table (default 100).")
    p.add_argument("--cluster", default=None,
                    help="Restrict the whole report to one role cluster (case-insensitive substring "
                         "match against its label, e.g. 'Data'). Default: all clusters.")
    p.add_argument("--seed", type=int, default=None, help="Random seed for the sample (default: unseeded).")
    p.add_argument("--out", default=None,
                    help="Output JSON path (default: .embedding_analysis/report_<timestamp>.json)")
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
    import full_auto as engine  # noqa: E402  -- must come after an app.* import so .env is loaded

    db = database.SessionLocal()

    try:
        profile_id = args.profile_id
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
        print(f"[analyze] profile {profile_id} ({profile.name!r})")

        # ── Rebuild the candidate side (small real API cost: one region-inference
        # LLM call inside build_snapshot + one embeddings call below) ──────────
        snap = snapshot.build_snapshot(db, profile_id)
        role_clusters = snap["role_clusters"]
        eng_profile = snap["engine_profile"]
        seniority = eng_profile.get("seniority") or ""
        print(f"[analyze] seniority={seniority!r} | {len(role_clusters)} role cluster(s): "
              + "; ".join(f"{c['label']}={c['roles']}" for c in role_clusters))

        cluster_texts = [c["weighted_text"] for c in role_clusters]
        cluster_embeddings = engine.get_embeddings_batch(cluster_texts)
        print(f"[analyze] embedded {len(cluster_texts)} cluster text(s) via {engine.EMBED_MODEL}")

        # ── Load every already-cached job for this profile (zero new cost) ─────
        all_rows = db.query(models.JobSeen).filter(models.JobSeen.profile_id == profile_id).all()
        rows_with_embedding = [r for r in all_rows if r.embedding]
        n_missing = len(all_rows) - len(rows_with_embedding)
        print(f"[analyze] {len(rows_with_embedding)} cached jobs with an embedding "
              f"({n_missing} skipped -- never embedded, would cost API credits to backfill)")
        if not rows_with_embedding:
            raise SystemExit("no cached job embeddings for this profile -- run a real search at least once first")

        # ── Re-score (free, local) and re-derive the heuristic verdict ─────────
        scored = engine_svc._score_rows(rows_with_embedding, cluster_embeddings)
        by_identity = {r.identity_hash: r for r in rows_with_embedding}

        junior_band, senior_band = engine_svc._JUNIOR_BAND, engine_svc._SENIOR_BAND
        seniority_lower = seniority.lower()
        is_junior = any(b in seniority_lower for b in junior_band)
        is_senior = (not is_junior) and any(b in seniority_lower for b in senior_band)
        heuristic_active = is_junior or is_senior
        reject_re = engine_svc._SENIOR_TITLE_RE if is_junior else engine_svc._JUNIOR_TITLE_RE

        primary, floor = engine_svc.RELEVANCE_PRIMARY, engine_svc.RELEVANCE_FLOOR

        def bucket(score: float) -> str:
            if score >= primary:
                return "strong"
            if score >= floor:
                return "broadened"
            return "below_floor"

        enriched = []
        for d in scored:
            title = d.get("title") or ""
            m = reject_re.search(title) if heuristic_active else None
            row_src = by_identity.get(d.get("_identity"))
            enriched.append({
                "title": title,
                "company": d.get("company") or "",
                "source": d.get("board") or "",
                "url": d.get("url") or "",
                "snippet": (d.get("snippet") or "")[:2000],
                "cluster_idx": d.get("_cluster"),
                "cluster_label": role_clusters[d["_cluster"]]["label"] if role_clusters else None,
                "embed_score": round(d.get("embed_score", 0.0), 4),
                "relevance_bucket": bucket(d.get("embed_score", 0.0)),
                "heuristic_active": heuristic_active,
                "heuristic_would_drop": bool(m),
                "heuristic_reason": m.group(0) if m else None,
                "prior_eval_verdict": row_src.eval_verdict if row_src else None,
                "dead_reason": row_src.dead_reason if row_src else None,
                "state": row_src.state if row_src else None,
            })

        # ── Optionally restrict to one role cluster (whole report, not just the sample) ──
        cluster_filter_label = None
        if args.cluster:
            needle = args.cluster.lower()
            matches = sorted({e["cluster_label"] for e in enriched
                               if e["cluster_label"] and needle in e["cluster_label"].lower()})
            if not matches:
                available = sorted({c["label"] for c in role_clusters})
                raise SystemExit(f"--cluster {args.cluster!r} matched no cluster; available: {available}")
            if len(matches) > 1:
                raise SystemExit(f"--cluster {args.cluster!r} matched multiple clusters: {matches}; be more specific")
            cluster_filter_label = matches[0]
            n_before = len(enriched)
            enriched = [e for e in enriched if e["cluster_label"] == cluster_filter_label]
            print(f"[analyze] --cluster {cluster_filter_label!r}: {n_before} -> {len(enriched)} jobs")

        # ── Aggregate stats over the FULL (possibly cluster-filtered) population ──
        total = len(enriched)
        bucket_counts = Counter(e["relevance_bucket"] for e in enriched)
        cluster_counts = Counter(e["cluster_label"] for e in enriched)
        heuristic_drop_count = sum(1 for e in enriched if e["heuristic_would_drop"])
        cross_tab = defaultdict(Counter)
        for e in enriched:
            cross_tab[e["relevance_bucket"]][e["heuristic_would_drop"]] += 1

        hist_edges = [i / 20 for i in range(21)]  # 0.00, 0.05, ..., 1.00
        histogram = [0] * 20
        for e in enriched:
            s = max(0.0, min(0.999999, e["embed_score"]))
            histogram[int(s * 20)] += 1

        scores = [e["embed_score"] for e in enriched]
        stats = {
            "total": total,
            "n_skipped_no_embedding": n_missing,
            "min": round(min(scores), 4),
            "max": round(max(scores), 4),
            "avg": round(sum(scores) / total, 4),
            "median": round(sorted(scores)[total // 2], 4),
            "bucket_counts": dict(bucket_counts),
            "cluster_counts": dict(cluster_counts),
            "heuristic_active": heuristic_active,
            "heuristic_drop_count": heuristic_drop_count,
            "heuristic_drop_pct": round(100 * heuristic_drop_count / total, 2) if heuristic_active else None,
            "cross_tab_bucket_vs_heuristic_drop": {
                bkt: {"dropped": counts.get(True, 0), "kept": counts.get(False, 0)}
                for bkt, counts in cross_tab.items()
            },
            "histogram_edges": hist_edges,
            "histogram_counts": histogram,
        }

        # ── Sample for the row-level table ───────────────────────────────────
        rng = random.Random(args.seed)
        sample = rng.sample(enriched, min(args.sample_size, total))
        sample.sort(key=lambda e: e["embed_score"], reverse=True)

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "profile_id": profile_id,
            "profile_name": profile.name,
            "seniority": seniority,
            "thresholds": {
                "RELEVANCE_PRIMARY": primary,
                "RELEVANCE_FLOOR": floor,
                "MIN_RESULTS": engine_svc.MIN_RESULTS,
            },
            "embedding_template": "{title} {company} {snippet[:2000]}",
            "cluster_filter": cluster_filter_label,
            "role_clusters": [
                {"label": c["label"], "roles": c["roles"], "weighted_text": c["weighted_text"]}
                for c in role_clusters
                if cluster_filter_label is None or c["label"] == cluster_filter_label
            ],
            "stats": stats,
            "sample_size": len(sample),
            "sample": sample,
        }

        out_path = Path(args.out) if args.out else (
            ROOT / ".embedding_analysis" / f"report_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2)

        print(f"\n[analyze] {total} jobs scored | avg={stats['avg']} min={stats['min']} max={stats['max']}")
        print(f"[analyze] buckets: {dict(bucket_counts)}")
        if heuristic_active:
            print(f"[analyze] heuristic prescreen ACTIVE (reject_re={reject_re.pattern!r}) "
                  f"-- would drop {heuristic_drop_count}/{total} ({stats['heuristic_drop_pct']}%)")
        else:
            print("[analyze] heuristic prescreen INACTIVE for this profile (seniority is mid-level)")
        print(f"[analyze] wrote report to {out_path}")
    finally:
        db.close()


if __name__ == "__main__":
    main()
