"use client";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

// Mirrors backend/app/config.DEFAULT_MAX_LISTING_AGE_DAYS.
const DEFAULT_DAYS = "30";

// Weighted toward the short end: a listing open longer than a month has most of
// its shortlist decided already (see full_auto._listing_age_tag), so the useful
// choices are all below 30 days, and 60/90 were doing little that "No limit"
// didn't. DEFAULT_DAYS stays selected by default, unchanged.
const PRESETS: { value: string; label: string }[] = [
  { value: "7", label: "7 days" },
  { value: "14", label: "14 days" },
  { value: DEFAULT_DAYS, label: "30 days" },
  { value: "0", label: "No limit" },
];

/**
 * Single-value, like location_scope/seniority: at most one max_listing_age row.
 * No row at all means the backend default (30 days) applies -- so the 30-day
 * button reads as selected even before the candidate has ever touched this.
 */
export function MaxListingAgePicker({
  profileId,
  attributes,
}: {
  profileId: number;
  attributes: Attribute[];
}) {
  const { add, remove } = useAttributeMutations(profileId);
  const current = attributes[0]?.value ?? DEFAULT_DAYS;

  // A value the candidate picked under an older preset list (60/90 were offered
  // until the list was shortened) is still stored and still enforced by the
  // backend, which parses any integer. Show it as its own button rather than
  // leaving every button unlit, which would read as a broken control while the
  // filter was quietly still applying.
  const options = PRESETS.some((p) => p.value === current)
    ? PRESETS
    : [...PRESETS, { value: current, label: `${current} days` }];

  function setDays(value: string) {
    if (current === value) return;
    attributes.forEach((a) => remove.mutate(a.id));
    add.mutate({ type: "max_listing_age", value });
  }

  return (
    <div className="choice-row">
      {options.map((p) => (
        <button
          key={p.value}
          className={`toggle ${current === p.value ? "on" : "off"}`}
          onClick={() => setDays(p.value)}
        >
          {p.label}
        </button>
      ))}
    </div>
  );
}
