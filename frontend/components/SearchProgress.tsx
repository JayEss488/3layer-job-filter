"use client";

import { useEffect, useState } from "react";

import { apiTime } from "@/lib/dates";

/**
 * The three-stage timeline shown while a search is running.
 *
 * The pipeline paints the /search page three times (see CLAUDE.md's Role
 * data-model note: stage "embed" -> stage "rank" -> final), and each paint is
 * both slower and considerably better-informed than the last. Before this the
 * only time signal was "This takes a couple of minutes", which set one
 * expectation for three very different waits and gave no way to tell a run
 * that's progressing normally from one that's stuck — the user would see eight
 * cosine-similarity cards at 45s and reasonably read them as the result.
 *
 * The per-stage targets are no longer a hand-tuned guess: they're derived from
 * the PREVIOUS finished run's own `phase_timings` (see settings.py's
 * RunTimingsOut / GET /settings/run-timings), rounded up to the nearest 15s so
 * a run that's a hair faster than last time never reads as "taking longer".
 * `deriveStageTargets` maps `_RUN_PHASE_LABELS`' phase keys onto the three
 * paint checkpoints; a profile with no finished run yet falls back to the
 * original measured defaults (90s/180s/360s) so the very first search still
 * has something to show. Stage state is driven by what has actually been
 * painted (`stage1Done`/`stage2Done`, passed down from the sections the page
 * is already rendering) rather than by the clock — the elapsed counter only
 * ever reports real time. A stage whose target has passed without landing is
 * marked "taking longer" instead of silently sitting at its estimate, so a
 * slow source or a retrying model reads as slow rather than as broken.
 */

const DEFAULT_TARGET_SECONDS: readonly [number, number, number] = [60, 180, 360];

// Which of engine.py's _lap() phase keys land before each paint checkpoint.
// "embed" (early matches) appears the moment the cosine pre-filter returns —
// after discovery/category-expansion/embedding/scoring. "rank" (top
// candidates) appears once the cheap gate+rank round(s) finish and
// fair-allocate to the judge pool completes. "final" is the run's own total.
const STAGE1_PHASES = new Set(["discovery", "category_expand", "embed", "score"]);
const STAGE2_EXTRA_PHASES = new Set([
  "enrich",
  "enrich_reed",
  "enrich_adzuna",
  "gate",
  "judge_floor_topup",
  "verify_liveness",
  "rank",
]);

/** Rounds a target up to the nearest 15s so last run's finish time never reads
 *  as "overdue" the moment this run matches it. */
function roundUpTo15(seconds: number): number {
  return Math.max(15, Math.ceil(seconds / 15) * 15);
}

export function deriveStageTargets(
  phases: readonly { name: string; seconds: number }[] | undefined,
  totalSeconds: number | undefined,
): [number, number, number] {
  if (!phases || !phases.length || !totalSeconds) return [...DEFAULT_TARGET_SECONDS];

  let stage1 = 0;
  let stage2Extra = 0;
  for (const ph of phases) {
    if (STAGE1_PHASES.has(ph.name)) stage1 += ph.seconds;
    else if (STAGE2_EXTRA_PHASES.has(ph.name)) stage2Extra += ph.seconds;
  }
  const stage2 = stage1 + stage2Extra;
  // total_seconds already covers every phase, including the tail (scrape,
  // final_eval/"scrape+judge", verify_final_picks) — no need to sum those too.
  const stage3 = Math.max(totalSeconds, stage2);

  return [roundUpTo15(stage1), roundUpTo15(stage2), roundUpTo15(stage3)];
}

function formatTargetLabel(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m === 0) return `~${s}s`;
  const minPart = `${m} min${m === 1 ? "" : "s"}`;
  return s === 0 ? `~${minPart}` : `~${minPart} ${s}s`;
}

const STAGE_META = [
  {
    key: "embed",
    label: "Early matches",
    detail: "keyword + semantic similarity only — no AI has read these",
  },
  {
    key: "rank",
    label: "Top candidates",
    detail: "quick AI scoring — a first real read of each listing",
  },
  {
    key: "final",
    label: "Full AI review",
    detail: "the deep review that decides your final picks",
  },
] as const;

/** Server timestamps are naive UTC (no offset suffix, sometimes space-separated).
 *  `new Date()` would read those as LOCAL time, so elapsed would be out by the
 *  viewer's UTC offset — an hour of phantom progress in British Summer Time.
 *  This rule now lives in lib/dates (three other call sites needed it and did
 *  not have it); the alias is kept so the reader here still sees why. */
const parseServerTime = apiTime;

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function SearchProgress({
  startedAt,
  stage1Done,
  stage2Done,
  lastRunPhases,
  lastRunTotalSeconds,
}: {
  startedAt?: string | null;
  stage1Done: boolean;
  stage2Done: boolean;
  /** The previous finished run's per-phase wall time (RunTimings.phases) —
   *  used to derive this run's stage targets. Omit/undefined falls back to
   *  the original fixed defaults. */
  lastRunPhases?: readonly { name: string; seconds: number }[];
  lastRunTotalSeconds?: number;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);

  const startMs = startedAt ? parseServerTime(startedAt) : null;
  // Clamped at 0: a small server/client clock skew shouldn't render as a
  // negative timer on a run that genuinely just started.
  const elapsed =
    startMs !== null && Number.isFinite(startMs)
      ? Math.max(0, Math.floor((now - startMs) / 1000))
      : null;

  const targets = deriveStageTargets(lastRunPhases, lastRunTotalSeconds);
  const done = [stage1Done || stage2Done, stage2Done, false];
  const activeIndex = done.findIndex((d) => !d);

  return (
    <div className="search-progress">
      <div className="search-progress-head">
        <span>
          <span className="spinner">◴</span> Building your matches — results appear below in
          three passes, each better informed than the last.
        </span>
        {elapsed !== null && <span className="search-progress-clock">{formatElapsed(elapsed)}</span>}
      </div>
      <ol className="search-progress-steps">
        {STAGE_META.map((stage, i) => {
          const targetSeconds = targets[i];
          const isDone = done[i];
          const isActive = i === activeIndex;
          const overdue =
            isActive && elapsed !== null && elapsed > targetSeconds;
          return (
            <li
              key={stage.key}
              className={`search-progress-step${isDone ? " is-done" : ""}${
                isActive ? " is-active" : ""
              }`}
            >
              <span className="search-progress-mark">{isDone ? "✓" : isActive ? "◴" : "○"}</span>
              <span className="search-progress-body">
                <span className="search-progress-label">
                  {stage.label}
                  <span className="search-progress-target">
                    {isDone ? "done" : overdue ? "taking longer than usual…" : formatTargetLabel(targetSeconds)}
                  </span>
                </span>
                <span className="search-progress-detail">{stage.detail}</span>
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
