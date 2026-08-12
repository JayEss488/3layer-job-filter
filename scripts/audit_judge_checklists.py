"""Measure the final judge's requirements checklist against the live store --
read-only, offline, free (no API, no LLM, no writes).

This is the regression bar for any change to full_auto's reasoning step D, and the
cheap half of the pair: tests/judge_harness.py A/Bs two prompts live over a small
sample, this one characterises what the judge has ACTUALLY been producing over
every verdict ever persisted.

WHY THE CHECKLIST IS WORTH ITS OWN AUDIT. "fit_level" and "concerns" are derived
MECHANICALLY from the step-D checklist -- that is the point of the v16-v18
checklist-discipline rework. So an ask that never reaches the checklist cannot
become a concern and cannot move the grade, however plainly the posting states it.
An under-extracted checklist is therefore silent, and it errs in one direction
only: the rubric reads "core" items ONLY, so a short checklist produces an
INFLATED grade, not a cautious one.

WHAT TO READ, AND IN WHAT ORDER:
  * The PER-RUN table first, not the store-wide mean. The store-wide number pools
    several prompt versions and several judge-pool sizes and hides the trend that
    matters. Checklist size fell 6.00 -> 3.67 items over runs 20-24 while the
    postings got LONGER.
  * The BUDGET column beside it. `tokens_judge_completion_tokens / pick objects`
    is what the model had to spend per pick, and checklist size tracks it
    monotonically: ~1300 tokens/pick gave 6-item checklists, ~490 gave 3.7. The
    judge pool grew 22 -> 40 and FINAL_EVAL_PROMPT_VERSION 26 widened "backup"
    from 3 to FINAL_PICKS, roughly doubling the pick objects one call must emit.
    A thin checklist under a collapsed budget is a CAPACITY finding and no prompt
    edit will fix it; a thin one under a comfortable budget is a prompt finding.
    Reading the size without the budget cannot tell those apart.
  * Size against JD LENGTH last. A checklist that does not grow with the posting
    is a fixed-size habit rather than a reading of the text.

Only strong/backup rows carry a checklist -- _persist_verdicts stores no
"requirements" for a reject -- so every count here is over PICKS, which is also
the only population the metric means anything for.

Usage:
    venv/Scripts/python scripts/audit_judge_checklists.py
    venv/Scripts/python scripts/audit_judge_checklists.py --profile-id 1 --since-run 20
"""
import argparse
import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "backend" / "jobmatch.db"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default=str(DB))
    p.add_argument("--profile-id", type=int, default=None,
                   help="Restrict to one profile (default: every profile).")
    p.add_argument("--since-run", type=int, default=0,
                   help="Only include the per-run table from this SearchRun id on (default 0).")
    p.add_argument("--show-thin", type=int, default=3,
                   help="List every pick whose checklist has this many items or fewer (default 3).")
    return p.parse_args()


# The prompt's own stated shape, restated here so a drift between the two is
# visible rather than assumed. Keep in sync with reasoning step D.
PROMPT_CORE_MIN, PROMPT_SECONDARY_MIN, PROMPT_MAX_TOTAL = 4, 2, 12


def main():
    args = parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    where = "js.eval_analysis IS NOT NULL AND js.eval_analysis != ''"
    params = []
    if args.profile_id:
        where += " AND js.profile_id = ?"
        params.append(args.profile_id)

    rows = list(conn.execute(
        f"""SELECT js.profile_id, js.title, js.company, js.eval_verdict, js.eval_analysis,
                   LENGTH(COALESCE(js.full_text, js.snippet, '')) AS text_chars
            FROM jobs_seen js WHERE {where}""", params))

    items, cores, secs, unmet, lens, thin = [], [], [], [], [], []
    grades = Counter()
    for r in rows:
        try:
            a = json.loads(r["eval_analysis"] or "{}")
        except (ValueError, TypeError):
            continue
        req = a.get("requirements") or []
        if not req:
            continue
        core = [x for x in req if (x or {}).get("category") == "core"]
        items.append(len(req))
        cores.append(len(core))
        secs.append(sum(1 for x in req if (x or {}).get("category") == "secondary"))
        unmet.append(sum(1 for x in core if not (x or {}).get("met")))
        lens.append(r["text_chars"])
        grades[a.get("fit_level") or "-"] += 1
        if len(req) <= args.show_thin:
            thin.append((len(req), r["text_chars"], a.get("fit_level"),
                         r["title"], r["company"],
                         [x.get("text") for x in req]))

    if not items:
        raise SystemExit("no persisted checklists found -- only strong/backup verdicts carry one.")

    n = len(items)
    print(f"STORE-WIDE  {n} picks with a persisted checklist "
          f"(of {len(rows)} judged rows; rejects carry none)")
    print(f"  items      min {min(items)}  median {statistics.median(items)}  "
          f"mean {statistics.mean(items):.2f}  max {max(items)}")
    print(f"  core       median {statistics.median(cores)}  mean {statistics.mean(cores):.2f}")
    print(f"  secondary  median {statistics.median(secs)}  mean {statistics.mean(secs):.2f}"
          f"   ({sum(1 for s in secs if s == 0)} picks with NONE)")
    print(f"  unmet core median {statistics.median(unmet)}  mean {statistics.mean(unmet):.2f}")
    ok = sum(1 for c, s in zip(cores, secs)
             if c >= PROMPT_CORE_MIN and s >= PROMPT_SECONDARY_MIN)
    print(f"  meeting the prompt's own '{PROMPT_CORE_MIN}-10 core, {PROMPT_SECONDARY_MIN}-6 "
          f"secondary': {ok}/{n} ({100.0 * ok / n:.1f}%)")
    print(f"  grades     {dict(grades)}")

    # Size against JD length. A flat profile here means the checklist is a habit,
    # not a reading of the posting.
    print("\nCHECKLIST SIZE vs JD TEXT LENGTH")
    buckets = defaultdict(list)
    for size, tl in zip(items, lens):
        b = ("<1k" if tl < 1000 else "1-2k" if tl < 2000 else "2-4k" if tl < 4000
             else "4-6k" if tl < 6000 else "6k+")
        buckets[b].append(size)
    for b in ("<1k", "1-2k", "2-4k", "4-6k", "6k+"):
        v = buckets.get(b)
        if v:
            print(f"  {b:5} n={len(v):4}  median {statistics.median(v):>4}  mean {statistics.mean(v):.2f}")
    if len(items) > 2 and statistics.pstdev(lens) and statistics.pstdev(items):
        mx, my = statistics.mean(lens), statistics.mean(items)
        cov = sum((x - mx) * (y - my) for x, y in zip(lens, items)) / len(items)
        print(f"  pearson r(JD length, checklist size) = "
              f"{cov / (statistics.pstdev(lens) * statistics.pstdev(items)):.3f}")

    # Per run, with the output budget beside it -- see the module docstring.
    print("\nPER RUN  (checklist size against the output budget the judge had per pick)")
    per_run = defaultdict(list)
    q = """SELECT r.search_run_id AS run, js.eval_analysis
           FROM roles r JOIN jobs_seen js
             ON js.identity_hash = r.external_id AND js.profile_id = r.profile_id
           WHERE r.fit_rank IS NOT NULL AND r.provisional = 0
             AND js.eval_analysis IS NOT NULL AND r.search_run_id >= ?"""
    qp = [args.since_run]
    if args.profile_id:
        q += " AND r.profile_id = ?"
        qp.append(args.profile_id)
    for r in conn.execute(q, qp):
        try:
            req = (json.loads(r["eval_analysis"] or "{}") or {}).get("requirements") or []
        except (ValueError, TypeError):
            continue
        if req:
            per_run[r["run"]].append(len(req))

    print(f"  {'run':>5} {'judged':>7} {'picks':>6} {'calls':>6} {'tok/pick':>9} {'items':>7} {'core':>6}")
    for run in sorted(per_run):
        fr = conn.execute("SELECT funnel_counts FROM search_runs WHERE id = ?", (run,)).fetchone()
        f = json.loads((fr["funnel_counts"] if fr else None) or "{}")
        picks = (f.get("final_strong") or 0) + (f.get("final_backup") or 0)
        comp = f.get("tokens_judge_completion_tokens") or 0
        calls = f.get("tokens_judge_calls") or 0
        v = per_run[run]
        budget = f"{comp // picks}" if picks else "-"
        print(f"  {run:>5} {f.get('final_fresh_judged', '-'):>7} {picks:>6} {calls:>6} "
              f"{budget:>9} {statistics.mean(v):>7.2f} {'':>6}")

    if thin:
        print(f"\nTHIN CHECKLISTS (<= {args.show_thin} items) -- {len(thin)} picks")
        for size, tl, fl, title, company, texts in sorted(thin)[:25]:
            print(f"  {size} item(s), {tl} chars of JD, graded {fl or '-'}: "
                  f"{(title or '')[:44]} @ {(company or '')[:26]}")
            for t in texts:
                print(f"        - {t}")
    conn.close()


if __name__ == "__main__":
    main()
