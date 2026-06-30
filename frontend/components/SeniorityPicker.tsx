"use client";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

const LEVELS = ["Junior", "Mid", "Senior", "Lead", "Director", "C-Suite"];

/** Multi-choice buttons. Each selected level is one seniority attribute row. */
export function SeniorityPicker({
  profileId,
  attributes,
}: {
  profileId: number;
  attributes: Attribute[];
}) {
  const { add, remove } = useAttributeMutations(profileId);
  const byValue = new Map(attributes.map((a) => [a.value.toLowerCase(), a]));

  function toggle(level: string) {
    const existing = byValue.get(level.toLowerCase());
    if (existing) remove.mutate(existing.id);
    else add.mutate({ type: "seniority", value: level });
  }

  return (
    <div className="choice-row">
      {LEVELS.map((level) => (
        <button
          key={level}
          className={`choice-btn${byValue.has(level.toLowerCase()) ? " selected" : ""}`}
          onClick={() => toggle(level)}
        >
          {level}
        </button>
      ))}
    </div>
  );
}
