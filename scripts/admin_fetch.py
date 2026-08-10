#!/usr/bin/env python3
"""The admin fetch command: read everything the beta is telling you, in one place.

Hits GET /admin/analytics and GET /admin/signups with the ADMIN_TOKEN header and
prints a readable digest. Before this existed the only way in was a hand-written
curl, which returns several hundred lines of JSON that nobody reads twice.

Four sections, in the order you actually want them:

  1. Headline totals + where each account is in its 7-day beta window.
  2. ROLES FOUND PER RUN -- the number that decides whether the pipeline is
     working at all. Reported as a distribution and an average, because "runs
     average 7" and "one run found 12 and three found 1" are very different
     situations that an average alone cannot tell apart. Runs that were
     cancelled or errored are listed but EXCLUDED from the stats: their
     result_count is 0 because the engine never wrote one, not because the
     search found nothing.
  3. Feedback from all three surfaces (sign-up, in-run prompts, wrap-up
     survey), grouped by question.
  4. Free-text bug reports / ideas (the /search feedback box).

Usage (from repo root, with the committed venv):
    venv/Scripts/python scripts/admin_fetch.py
    venv/Scripts/python scripts/admin_fetch.py --host https://<host>
    venv/Scripts/python scripts/admin_fetch.py --question results_quality
    venv/Scripts/python scripts/admin_fetch.py --user 51
    venv/Scripts/python scripts/admin_fetch.py --json > snapshot.json

ADMIN_TOKEN is read from the repo-root .env by default (the same file the
backend loads), or from --token.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))  # for the `app` package
sys.path.insert(0, REPO_ROOT)

# Imported for its side effect: config.py loads the repo-root .env regardless of
# cwd, which is where ADMIN_TOKEN lives.
from app.config import ADMIN_TOKEN as ENV_ADMIN_TOKEN  # noqa: E402

DEFAULT_HOST = "http://127.0.0.1:8000"

# This script prints text people typed, which routinely contains curly quotes,
# em dashes and emoji -- and a Windows console defaults to cp1252, which cannot
# encode any of them. Without this the report dies on the first such character,
# i.e. exactly when someone has finally left useful feedback. Same failure
# full_auto.emit() guards against; `replace` is the right call here because a
# mangled character is infinitely better than losing the report.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - non-reconfigurable stream
        pass


def _get(host: str, path: str, token: str) -> dict:
    req = urllib.request.Request(f"{host.rstrip('/')}{path}", headers={"X-Admin-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 403:
            raise SystemExit(
                f"403 from {path}. ADMIN_TOKEN is wrong, or unset on the server "
                f"(which disables these endpoints entirely).\n  {body}"
            )
        raise SystemExit(f"HTTP {e.code} from {path}: {body}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Could not reach {host}{path}: {e.reason}")


def _rule(title: str) -> None:
    print(f"\n{title}\n{'─' * max(len(title), 60)}")


def _fmt_dt(v: str | None) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM'. Never crashes on an unexpected shape."""
    if not v:
        return "—"
    return v.replace("T", " ")[:16]


def print_overview(analytics: dict, signups: dict) -> None:
    t = analytics.get("totals", {})
    s = signups.get("totals", {})
    _rule("OVERVIEW")
    print(f"  accounts            {s.get('accounts', 0)}  "
          f"({s.get('self_serve', 0)} self-serve, {s.get('legacy_password', 0)} legacy)")
    print(f"  active last 7 days  {t.get('active_last_7d', 0)}")
    print(f"  beta window         {s.get('in_window', 0)} in window, "
          f"{s.get('expired', 0)} expired, {s.get('legacy_no_window', 0)} exempt "
          f"({s.get('beta_window_days', '?')}-day window)")
    print(f"  signup survey       {s.get('survey_answered', 0)} answered, "
          f"{s.get('survey_outstanding', 0)} outstanding")
    print(f"  wrap-up survey      {s.get('exit_survey_answered', 0)} answered "
          f"of {s.get('exit_survey_asked', 0)} asked")
    print(f"  activity            {t.get('searches', 0)} searches, {t.get('ticks', 0)} ticks, "
          f"{t.get('crosses', 0)} crosses, {t.get('applies', 0)} applies, "
          f"{t.get('outcomes', 0)} outcomes")
    print(f"  feedback            {t.get('feedback_responses', 0)} answers, "
          f"{t.get('comments', 0)} free-text comments")

    _rule("ACCOUNTS")
    print(f"  {'user':<20}{'method':<10}{'window':<14}{'last active':<18}{'runs':>6}{'applies':>9}")
    by_id = {u["user_id"]: u for u in analytics.get("users", [])}
    for row in signups.get("signups", []):
        a = by_id.get(row["user_id"], {})
        if row["beta_expired"]:
            window = "expired"
        elif row["beta_started_at"]:
            window = f"{row['beta_days_left']}d left"
        else:
            window = "exempt"
        print(f"  {row['username'][:19]:<20}{row['method']:<10}{window:<14}"
              f"{_fmt_dt(a.get('last_active')):<18}{a.get('searches', 0):>6}{a.get('applies', 0):>9}")


def print_runs(analytics: dict, limit: int) -> None:
    runs = analytics.get("runs", [])
    st = analytics.get("run_stats", {})

    _rule("ROLES FOUND PER RUN")
    if not st.get("runs_done"):
        print("  No completed runs yet.")
    else:
        dist = st.get("roles_found_distribution", {})
        widest = max((int(v) for v in dist.values()), default=1)
        print(f"  completed runs      {st['runs_done']}"
              f"   (cancelled {st.get('runs_cancelled', 0)}, "
              f"errored {st.get('runs_error', 0)}, running {st.get('runs_running', 0)})")
        print(f"  roles found         avg {st.get('roles_found_avg')}, "
              f"median {st.get('roles_found_median')}, "
              f"range {st.get('roles_found_min')}–{st.get('roles_found_max')}")
        if st.get("runs_done_with_zero"):
            print(f"  ⚠ ran fine, found nothing: {st['runs_done_with_zero']} run(s)")
        print("\n  distribution (completed runs only):")
        for n, count in sorted(dist.items(), key=lambda kv: int(kv[0])):
            bar = "█" * max(1, round(int(count) / widest * 34))
            print(f"    {n:>3} roles  {bar} {count}")

    if runs:
        print(f"\n  most recent {min(limit, len(runs))} run(s):")
        print(f"    {'run':>5} {'user':<18}{'status':<11}{'roles':>6}{'secs':>8}  started")
        for r in runs[:limit]:
            print(f"    {r['run_id']:>5} {r['username'][:17]:<18}{r['status']:<11}"
                  f"{r['roles_found']:>6}{(r['duration_s'] or 0):>8.0f}  {_fmt_dt(r['started_at'])}")


# Human-readable labels for the question slugs. Kept here rather than imported
# so this stays a pure HTTP client that works against a remote host without the
# backend package needing to match.
_SURFACE_ORDER = ["signup", "in_run", "exit"]
_SURFACE_LABEL = {
    "signup": "Sign-up questions",
    "in_run": "In-product prompts",
    "exit": "Wrap-up survey",
}


def print_feedback(analytics: dict, question: str | None, user: int | None) -> None:
    rows = analytics.get("feedback", [])
    if question:
        rows = [r for r in rows if r["question_id"] == question]
    if user is not None:
        rows = [r for r in rows if r["user_id"] == user]

    _rule("FEEDBACK")
    if not rows:
        print("  Nothing recorded yet."
              + (" (with those filters)" if question or user is not None else ""))
        return

    by_surface: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by_surface[r["surface"]][r["question_id"]].append(r)

    for surface in _SURFACE_ORDER + [s for s in by_surface if s not in _SURFACE_ORDER]:
        questions = by_surface.get(surface)
        if not questions:
            continue
        print(f"\n  ── {_SURFACE_LABEL.get(surface, surface)} ──")
        for question_id, answers in sorted(questions.items()):
            print(f"\n  {question_id}  ({len(answers)})")
            # Short, repeated answers are worth tallying; free text is not, so
            # anything long or multi-valued is printed verbatim instead.
            values = [a["answer"] for a in answers]
            tallyable = all(isinstance(v, str) and len(v) <= 40 for v in values)
            if tallyable and len(set(values)) < len(values):
                counts: dict[str, int] = defaultdict(int)
                for v in values:
                    counts[v] += 1
                for v, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                    print(f"      {n:>3} x  {v}")
            else:
                for a in answers:
                    ans = a["answer"]
                    if isinstance(ans, list):
                        ans = ", ".join(ans)
                    run = f" run {a['run_id']}" if a.get("run_id") else ""
                    print(f"      [{a['username']}{run} {_fmt_dt(a['created_at'])}] {ans}")


def print_comments(analytics: dict, user: int | None) -> None:
    comments = analytics.get("comments", [])
    if user is not None:
        comments = [c for c in comments if c["user_id"] == user]
    _rule("BUG REPORTS / IDEAS (free text)")
    if not comments:
        print("  None.")
        return
    for c in comments:
        print(f"\n  [{c['username']} {_fmt_dt(c['created_at'])}]")
        print(f"  {c['text']}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.getenv("ADMIN_HOST", DEFAULT_HOST),
                    help=f"API base URL (default {DEFAULT_HOST})")
    ap.add_argument("--token", default="", help="ADMIN_TOKEN (default: read from the repo-root .env)")
    ap.add_argument("--question", help="only show feedback for this question_id")
    ap.add_argument("--user", type=int, help="only show feedback/comments from this user id")
    ap.add_argument("--runs", type=int, default=20, help="how many recent runs to list (default 20)")
    ap.add_argument("--json", action="store_true", help="dump the raw payloads instead")
    args = ap.parse_args()

    token = args.token or ENV_ADMIN_TOKEN
    if not token:
        raise SystemExit(
            "No ADMIN_TOKEN. Set it in the repo-root .env (the same value the server has) "
            "or pass --token. An unset token on the SERVER disables these endpoints entirely."
        )

    analytics = _get(args.host, "/admin/analytics", token)
    signups = _get(args.host, "/admin/signups", token)

    if args.json:
        print(json.dumps({"analytics": analytics, "signups": signups}, indent=2))
        return

    print(f"Four in a Thousand — beta report  ({args.host})")
    print(f"generated {_fmt_dt(analytics.get('generated_at'))}")
    print_overview(analytics, signups)
    print_runs(analytics, args.runs)
    print_feedback(analytics, args.question, args.user)
    print_comments(analytics, args.user)
    print()


if __name__ == "__main__":
    main()
