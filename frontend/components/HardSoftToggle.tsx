"use client";

import type { Enforcement } from "@/lib/types";

const TITLES: Record<Enforcement, string> = {
  hard: "Non-negotiable — a role that clearly fails this is dropped outright.",
  soft: "A preference — counted against a role, but never enough on its own to drop it.",
};

/**
 * Hard vs soft is a real pipeline distinction, not a label: hard means an
 * unconditional drop at the cheap screen plus a disqualifier at the final
 * judge; soft means one ordinary signal both stages weigh. See CLAUDE.md.
 */
export function HardSoftToggle({
  value,
  onChange,
  disabled,
  disabledReason,
}: {
  value: Enforcement;
  onChange: (v: Enforcement) => void;
  /** Greys the control out when there's nothing stated for it to enforce. */
  disabled?: boolean;
  disabledReason?: string;
}) {
  return (
    <div className="choice-row hardsoft" title={disabled ? disabledReason : undefined}>
      {(["hard", "soft"] as const).map((v) => (
        <button
          key={v}
          className={`toggle sm ${value === v ? "on" : "off"}`}
          onClick={() => onChange(v)}
          disabled={disabled}
          title={disabled ? disabledReason : TITLES[v]}
        >
          {v === "hard" ? "Hard" : "Soft"}
        </button>
      ))}
    </div>
  );
}
