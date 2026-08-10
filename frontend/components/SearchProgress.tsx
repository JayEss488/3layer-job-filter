"use client";

import { useEffect, useState } from "react";

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
 * The per-stage times below are approximate wall-clock targets measured on real
 * runs, not guarantees, and are labelled as such. They exist to set the
 * expectation that the FIRST thing on screen is the weakest thing on screen.
 *
 * Stage state is driven by what has actually been painted (`stage1Done`/
 * `stage2Done`, passed down from the sections the page is already rendering)
 * rather than by the clock — the elapsed counter only ever reports real time.
 * A stage whose target has passed without landing is marked "taking longer"
 * instead of silently sitting at its estimate, so a slow source or a retrying
 * model reads as slow rather than as broken.
 */

const STAGES = [
  {
    key: "embed",
    label: "Early matches",
    detail: "keyword + semantic similarity only — no AI has read these",
    targetSeconds: 45,
    targetLabel: "~45s",
  },
  {
    key: "rank",
    label: "Top candidates",
    detail: "quick AI scoring — a first real read of each listing",
    targetSeconds: 180,
    targetLabel: "~3 min",
  },
  {
    key: "final",
    label: "Full AI review",
    detail: "the deep review that decides your final picks",
    targetSeconds: 330,
    targetLabel: "~5 min 30s",
  },
] as const;

/** Server timestamps are naive UTC (no offset suffix, sometimes space-separated).
 *  `new Date()` would read those as LOCAL time, so elapsed would be out by the
 *  viewer's UTC offset — an hour of phantom progress in British Summer Time. */
function parseServerTime(iso: string): number {
  const normalized = iso.replace(" ", "T");
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(normalized);
  return new Date(hasZone ? normalized : `${normalized}Z`).getTime();
}

function formatElapsed(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function SearchProgress({
  startedAt,
  stage1Done,
  stage2Done,
}: {
  startedAt?: string | null;
  stage1Done: boolean;
  stage2Done: boolean;
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
        {STAGES.map((stage, i) => {
          const isDone = done[i];
          const isActive = i === activeIndex;
          const overdue =
            isActive && elapsed !== null && elapsed > stage.targetSeconds;
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
                    {isDone ? "done" : overdue ? "taking longer than usual…" : stage.targetLabel}
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
