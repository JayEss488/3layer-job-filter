"use client";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

// Mirrors backend/app/config.DEFAULT_MAX_LISTING_AGE_DAYS.
const DEFAULT_DAYS = "30";

const PRESETS: { value: string; label: string }[] = [
  { value: "14", label: "14 days" },
  { value: DEFAULT_DAYS, label: "30 days" },
  { value: "60", label: "60 days" },
  { value: "90", label: "90 days" },
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

  function setDays(value: string) {
    if (current === value) return;
    attributes.forEach((a) => remove.mutate(a.id));
    add.mutate({ type: "max_listing_age", value });
  }

  return (
    <div className="choice-row">
      {PRESETS.map((p) => (
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
