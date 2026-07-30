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
 *
 * Under the default Soft enforcement an unpicked arrangement is DEPRIORITISED,
 * not excluded (see config.enforcement_for): the cheap gate only demotes on it,
 * and the final judge is told to prefer a matching role rather than to reject a
 * non-matching one. That's deliberate — someone who ticks these without much
 * thought and then searches should still see a strong role they'd probably take.
 * But nothing on the card says so (the judge is barred from mentioning
 * arrangement in prose by NO LOCATION COMMENTARY, and `work_style` renders as a
 * bare fact chip), so a Remote result under an On-site/Hybrid profile reads as a
 * bug rather than as the setting working. This note is the only place that gap
 * is closed.
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

  // Only meaningful when the user has narrowed to SOME arrangements under Soft:
  // nothing picked is "no preference" (nothing is being let through), all three
  // picked excludes nothing, and Hard genuinely does exclude.
  const unpicked = WORK_TYPES.filter((w) => !selected.has(w.toLowerCase()));
  const showSoftNote =
    enforcement === "soft" && selected.size > 0 && unpicked.length > 0;

  return (
    <div className="location-row">
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
      {showSoftNote && (
        <div className="hint-detected">
          Soft filter: you may still be shown <strong>{formatList(unpicked)}</strong>{" "}
          roles when they&rsquo;re a strong match otherwise. Switch to Hard to exclude
          them.
        </div>
      )}
    </div>
  );
}

/** "Remote" / "Remote and On-site" / "Remote, On-site and Hybrid". */
function formatList(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}
