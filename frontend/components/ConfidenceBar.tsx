"use client";

import type { Confidence } from "@/lib/types";

export function ConfidenceBar({ confidence }: { confidence?: Confidence }) {
  const score = confidence?.score ?? 0;
  return (
    <div className="conf-block">
      <div className="conf-label">Profile confidence</div>
      <div className="conf-row">
        <div className="conf-track">
          <div className="conf-fill" style={{ width: `${score}%` }} />
        </div>
        <div className="conf-pct">{score}%</div>
      </div>
      {confidence?.tip && <div className="conf-tip">{confidence.tip}</div>}
    </div>
  );
}
