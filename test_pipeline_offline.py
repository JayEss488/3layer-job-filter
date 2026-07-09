"""Offline accuracy test harness for the search pipeline (NOT wired into the app).

Feeds a frozen sample of raw discovery listings (an .xlsx export shaped like
`jobs_seen`: title/company/location/source/snippet/url) through the REAL
pipeline components -- backend.app.services.snapshot.build_snapshot,
backend.app.services.engine._run_engine_pipeline, and full_auto's
screen_gate/rank_gate/final_evaluation_split -- instead of live discovery, so
you can tell whether the current algorithm actually surfaces the roles you
already know are good.

Isolation: runs against a throwaway SQLite DB and a throwaway full_auto
cache file (set via DATABASE_URL / engine.DB_PATH before anything is
imported), and writes the synthesised CV to a throwaway file instead of the
real exp.txt. backend/jobmatch.db, boards_cache.db, and exp.txt are never
opened. The one real profile involved (profile_id) is read-only copied in
from the production DB.

Costs real API credits: one embedding call per role cluster + per job
row, one region/cluster-inference LLM call (build_snapshot), one cheap
screen_gate + one mid-tier rank_gate LLM batch call per role cluster, and
one expensive final-evaluation call per cluster. Safe to re-run -- gate/
rank/final-eval decisions are cached per (profile signature, job id) in the
isolated cache DB, so a repeat run against the same sample only pays for
whatever changed.

Usage:
    venv/Scripts/python test_pipeline_offline.py
    venv/Scripts/python test_pipeline_offline.py --profile-id 7 --xlsx profile7_raw_sample.xlsx
    venv/Scripts/python test_pipeline_offline.py --scrape   # also do real Phase-5 page scraping
    venv/Scripts/python test_pipeline_offline.py --fresh    # wipe the isolated DB/cache and start over

Writes a full per-row trace to <workdir>/report.json alongside the printed
table; run scoring.py separately against that report once you know which
rows are the good ones.
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_WORKDIR = ROOT / ".offline_test"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profile-id", type=int, default=7)
    p.add_argument("--xlsx", default=str(ROOT / "profile7_raw_sample.xlsx"))
    p.add_argument("--prod-db", default=str(ROOT / "backend" / "jobmatch.db"),
                    help="Real DB to read the profile from (read-only, never written).")
    p.add_argument("--workdir", default=str(DEFAULT_WORKDIR),
                    help="Where the isolated test DB/cache/CV file live.")
    p.add_argument("--scrape", action="store_true",
                    help="Enable real Phase-5 full-page scraping (default: off, snippet-only).")
    p.add_argument("--fresh", action="store_true",
                    help="Delete the isolated DB/cache before running (loses cached gate/eval decisions).")
    return p.parse_args()


def read_xlsx_jobs(path: str) -> list[dict]:
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.active
    jobs = []
    for i, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=1):
        title, company, location, source, snippet, url, _state = (list(row) + [None] * 7)[:7]
        if not title:
            continue
        jobs.append({
            "_row": i,
            "board": source or "",
            "title": title,
            "company": company or "",
            "location": location or "",
            "url": url or "",
            "snippet": snippet or "",
        })
    return jobs


def copy_profile(prod_db_path: str, test_db_session, profile_id: int, models) -> int:
    """Read-only copy of one profile + its attributes from the production DB
    into the isolated test DB. Never opens the production DB for writing.
    No-ops if this profile was already copied in on a prior run against the
    same isolated DB (idempotent, so --fresh isn't required every re-run)."""
    if test_db_session.get(models.Profile, profile_id) is not None:
        return test_db_session.query(models.ProfileAttribute).filter(
            models.ProfileAttribute.profile_id == profile_id
        ).count()
    conn = sqlite3.connect(f"file:{prod_db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    prof = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not prof:
        conn.close()
        raise SystemExit(f"profile {profile_id} not found in {prod_db_path}")
    test_db_session.add(models.Profile(
        id=prof["id"], user_id=prof["user_id"], name=prof["name"],
        is_active=bool(prof["is_active"]), cv_text=prof["cv_text"], cv_summary=prof["cv_summary"],
    ))
    attrs = conn.execute(
        "SELECT * FROM profile_attributes WHERE profile_id=?", (profile_id,)
    ).fetchall()
    for a in attrs:
        test_db_session.add(models.ProfileAttribute(
            id=a["id"], profile_id=a["profile_id"], type=a["type"], value=a["value"],
            weight=a["weight"], source=a["source"], confirmed=bool(a["confirmed"]),
            proficiency=a["proficiency"],
        ))
    test_db_session.commit()
    conn.close()
    return len(attrs)


def main():
    # Match full_auto.emit()'s ASCII-safe fallback -- some job text / LLM output
    # contains characters this console's cp1252 stdout can't encode.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = parse_args()
    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    test_db_path = workdir / "test.db"
    cache_db_path = workdir / "boards_cache_test.db"
    cv_path = workdir / "cv_test.txt"
    report_path = workdir / "report.json"

    if args.fresh:
        for f in (test_db_path, cache_db_path):
            if f.exists():
                f.unlink()
        print(f"[harness] --fresh: wiped {test_db_path.name} and {cache_db_path.name}")

    # Must happen before ANY `import app...` -- config.py reads this at import time.
    os.environ["DATABASE_URL"] = f"sqlite:///{test_db_path}"

    sys.path.insert(0, str(ROOT / "backend"))
    sys.path.insert(0, str(ROOT))

    import app.database as database
    import app.models as models
    from app.services import snapshot
    from app.services import engine as engine_svc
    from app.services import sources

    database.init_db()
    db = database.SessionLocal()

    n_attrs = copy_profile(args.prod_db, db, args.profile_id, models)
    print(f"[harness] copied profile {args.profile_id} ({n_attrs} attributes) "
          f"read-only from {args.prod_db}")

    raw_jobs = read_xlsx_jobs(args.xlsx)
    print(f"[harness] loaded {len(raw_jobs)} raw listings from {args.xlsx}")

    import full_auto as engine
    engine.DB_PATH = str(cache_db_path)   # isolate full_auto's own gate/embedding cache
    engine.CV_PATH = str(cv_path)         # never touch the real exp.txt
    engine.init_db()

    # Fixed-sample injection: no live discovery. This is the only real-pipeline
    # call this script skips -- everything downstream (filters, store, scoring,
    # gates, ranking, scraping, final judge) is the unmodified production code.
    engine.gather_jobs = lambda profile: [dict(j) for j in raw_jobs]

    sources.set_full_scrape_enabled(db, args.scrape)
    print(f"[harness] full-page scraping: {'ON (real network fetch)' if args.scrape else 'OFF (snippet-only)'}")

    snap = snapshot.build_snapshot(db, args.profile_id)
    with open(engine.CV_PATH, "w", encoding="utf-8") as f:
        f.write(snap["cv_text"])
    print(f"[harness] role clusters ({len(snap['role_clusters'])}): "
          + "; ".join(str(c["roles"]) for c in snap["role_clusters"]))

    run = models.SearchRun(profile_id=args.profile_id, status="running")
    db.add(run)
    db.commit()

    print("\n[harness] ── running the real pipeline ──\n")
    final, harsh, marks, timings, warning, funnel = asyncio.run(
        engine_svc._run_engine_pipeline(
            engine, snap["engine_profile"], snap["weighted_text"], snap["cv_text_base"],
            db, args.profile_id, run,
        )
    )
    print(f"\n[harness] ── pipeline done: {len(final)} final picks ──\n")

    print("[harness] funnel:", json.dumps(funnel, indent=2))
    print("[harness] timings (s):", json.dumps(timings, indent=2))
    if warning:
        print(f"[harness] warning: {warning}")

    # Mirror run_search_task's post-pipeline bookkeeping so a second run against
    # this same isolated store sees realistic new/enriched/shown state (backlog
    # top-up, re-queue semantics) instead of every row looking untouched.
    processed_ids, shown_ids = marks if marks else ([], [])
    if marks:
        engine_svc._mark(db, args.profile_id, processed_ids, "enriched")
        engine_svc._mark(db, args.profile_id, shown_ids, "shown")

    # ── Build the per-row trace ────────────────────────────────────────────
    processed_set, shown_set = set(processed_ids), set(shown_ids)
    final_by_identity = {f.get("_identity"): f for f in final}

    jobseen_rows = db.query(models.JobSeen).filter(
        models.JobSeen.profile_id == args.profile_id
    ).all()
    jobseen_by_identity = {r.identity_hash: r for r in jobseen_rows}

    cluster_texts = [c["weighted_text"] for c in snap["role_clusters"]]
    cluster_embeddings = engine.get_embeddings_batch(cluster_texts)
    scored_all = engine_svc._score_rows(engine, jobseen_rows, cluster_embeddings)
    scored_by_identity = {d["_identity"]: d for d in scored_all}

    role_clusters = snap["role_clusters"]

    def cluster_profile_for(idx: int) -> dict:
        cp = dict(snap["engine_profile"])
        cp["search_terms"] = role_clusters[idx].get("roles") or snap["engine_profile"].get("search_terms")
        return cp

    trace = []
    for job in raw_jobs:
        identity = engine_svc.identity_hash(job)
        row = {
            "row": job["_row"], "title": job["title"], "company": job["company"],
            "source": job["board"], "location": job["location"], "url": job["url"],
        }
        js = jobseen_by_identity.get(identity)
        if js is None:
            row["outcome"] = "hard_filtered"
            row["detail"] = "dropped before entering the store (blocklist / training-scheme / country / salary filter)"
        elif js.dead_reason:
            # Checked ahead of the generic processed_set branch below: a dead-confirmed
            # job's identity IS in processed_set (it was already in `pool` before Phase 5
            # scraping ran) but never reaches Phase 6, so without this check it would
            # fall into the gate/rank re-check branch and get mislabeled rank_or_cap_dropped.
            row["outcome"] = "confirmed_dead"
            row["detail"] = f"Phase 5 confirmed this listing is dead/expired ({js.dead_reason}); excluded before final judge"
        elif identity in shown_set:
            f = final_by_identity.get(identity, {})
            row["outcome"] = "FINAL_PICK"
            row["detail"] = f"{'strong' if f.get('strong_fit') else 'backup/fallback'} " \
                             f"(cluster: {f.get('_cluster_label') or 'n/a'})"
            row["ai_analysis"] = engine_svc._compose_analysis(f)
            row["embed_score"] = f.get("embed_score")
        elif js.eval_verdict and identity in processed_set:
            # Gate on processed_set (THIS run's pool), not just a persisted verdict --
            # eval_verdict survives across harness re-runs against the same store, so a
            # job already 'shown' on a prior run (and thus excluded from this run's
            # candidate pool entirely -- see _new_rows/_backlog_rows) would otherwise
            # report a stale verdict as if this run had just judged it.
            row["outcome"] = f"final_eval_{js.eval_verdict}"
            try:
                analysis = json.loads(js.eval_analysis or "{}")
            except (ValueError, TypeError):
                analysis = {}
            row["detail"] = (f"reached the final AI judge and was {js.eval_verdict}"
                              + (f" ({'; '.join(analysis.get('concerns', []))})" if analysis.get("concerns") else ""))
            if js.eval_verdict == "strong":
                # A strong verdict always enters its cluster's pick list; the only way
                # it's absent from `final` is the cross-cluster FINAL_PICKS cap.
                row["detail"] += " -- but cut by the cross-cluster FINAL_PICKS cap during fair-allocation"
            elif js.eval_verdict == "backup":
                # Backup entries are only promoted to output when their cluster's
                # strong list is EMPTY that run -- if this fired, some other job in
                # the same cluster scored strong instead, not a pool/cap issue.
                row["detail"] += " -- but this cluster already had a strong pick this run, so its backups weren't surfaced"
            d = scored_by_identity.get(identity, {})
            row["embed_score"] = d.get("embed_score")
        elif identity in processed_set:
            d = scored_by_identity.get(identity)
            row["embed_score"] = d.get("embed_score") if d else None
            row["cluster"] = d.get("_cluster") if d else None
            if d is not None:
                idx = d.get("_cluster", 0)
                gate_res = engine.screen_gate([d], cluster_profile_for(idx))[0]  # cache hit, no new LLM call
                sector_ok = gate_res.get("_sector_ok")
                seniority_ok = gate_res.get("_seniority_ok")
                row["sector_ok"] = sector_ok
                row["seniority_ok"] = seniority_ok
                row["gate_reason"] = gate_res.get("_gate_reason")
                if not sector_ok:
                    row["outcome"] = "gate_dropped_sector"
                    row["detail"] = f"cheap gate: off-sector ({row['gate_reason']})"
                else:
                    rank_res = engine.rank_gate([d], cluster_profile_for(idx))[0]  # may be a fresh cache miss
                    row["rank_score"] = rank_res.get("_rank_score")
                    row["outcome"] = "rank_or_cap_dropped"
                    row["detail"] = (f"passed the sector/seniority gate (seniority_ok={seniority_ok}), "
                                      f"rank_score={row['rank_score']:.0f} -- never reached the final AI judge "
                                      f"(cheap-rank bottom-20% autoreject, or JUDGE_POOL/TARGET_POOL cap)")
            else:
                row["outcome"] = "processed_no_score"
                row["detail"] = "in the pool but embedding/score missing (unexpected)"
        elif js.state == "shown":
            # Already surfaced as a final pick on an earlier harness run against this
            # same isolated store -- _new_rows/_backlog_rows never reconsider a 'shown'
            # row (see engine.py's _mark), so it wasn't even a candidate this run.
            row["outcome"] = "already_shown_prior_run"
            row["detail"] = f"excluded from this run's pool: shown as a final pick on an earlier run (verdict was {js.eval_verdict!r})"
        else:
            d = scored_by_identity.get(identity)
            row["embed_score"] = d.get("embed_score") if d else None
            row["cluster"] = d.get("_cluster") if d else None
            row["outcome"] = "below_pool_threshold"
            row["detail"] = (f"embed_score={row['embed_score']:.3f} was too low relative to other "
                              f"candidates to enter the adaptive pool" if d else "no embedding computed")
        trace.append(row)

    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"funnel": funnel, "timings": timings, "warning": warning, "rows": trace}, f, indent=2)

    print(f"\n[harness] wrote full per-row trace to {report_path}\n")
    print(f"{'#':>3}  {'OUTCOME':<24} {'SCORE':>6}  TITLE — COMPANY")
    for r in trace:
        score = f"{r['embed_score']:.3f}" if r.get("embed_score") is not None else "  -  "
        print(f"{r['row']:>3}  {r['outcome']:<24} {score:>6}  {r['title']} — {r['company']}")

    db.close()


if __name__ == "__main__":
    main()
