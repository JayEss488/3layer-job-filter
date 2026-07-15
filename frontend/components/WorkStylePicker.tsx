"use client";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute, Enforcement } from "@/lib/types";
import { WORK_SET, WORK_TYPES } from "./LocationPicker";

/**
 * On-site / Hybrid / Remote, multi-choice. Stored as `location` attribute rows
 * (the same type as the free-text city — the backend splits the two apart on
 * config.WORK_TYPE_VALUES), but rendered as their own preference row because
 * they're a separate constraint with their own Hard/Soft: these drive the
 * screen's work-arrangement axis, the city drives the country prefilter.
 *
 * Selecting nothing is meaningful — it means "no preference", which the screen
 * treats as nothing to conflict with, not as "wants remote".
 */
export function WorkStylePicker({
  profileId,
  attributes,
  enforcement,
}: {
  profileId: number;
  attributes: Attribute[];
  /** The Hard/Soft the row is currently showing, so a newly ticked work type
   *  joins at the same strictness rather than silently arriving at the default. */
  enforcement: Enforcement;
}) {
  const { add, remove } = useAttributeMutations(profileId);
  const selected = new Map(
    attributes
      .filter((a) => WORK_SET.has(a.value.toLowerCase()))
      .map((a) => [a.value.toLowerCase(), a])
  );

  function toggle(w: string) {
    const existing = selected.get(w.toLowerCase());
    if (existing) remove.mutate(existing.id);
    else add.mutate({ type: "location", value: w, enforcement });
  }

  return (
    <div className="choice-row">
      {WORK_TYPES.map((w) => (
        <button
          key={w}
          className={`toggle ${selected.has(w.toLowerCase()) ? "on" : "off"}`}
          onClick={() => toggle(w)}
        >
          {w}
        </button>
      ))}
    </div>
  );
}
