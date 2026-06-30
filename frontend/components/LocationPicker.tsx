"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

const WORK_TYPES = ["On-site", "Hybrid", "Remote"];
const WORK_SET = new Set(WORK_TYPES.map((w) => w.toLowerCase()));

/** City/region free text + work-type multi-choice. Each stored as a location attr. */
export function LocationPicker({
  profileId,
  attributes,
}: {
  profileId: number;
  attributes: Attribute[];
}) {
  const { add, remove, invalidate } = useAttributeMutations(profileId);

  const cityAttr = attributes.find((a) => !WORK_SET.has(a.value.toLowerCase()));
  const workTypes = new Map(
    attributes
      .filter((a) => WORK_SET.has(a.value.toLowerCase()))
      .map((a) => [a.value.toLowerCase(), a])
  );

  const [city, setCity] = useState(cityAttr?.value ?? "");
  useEffect(() => setCity(cityAttr?.value ?? ""), [cityAttr?.value]);

  async function persistCity() {
    const v = city.trim();
    if (cityAttr && v && v !== cityAttr.value) {
      await api.updateAttribute(cityAttr.id, { value: v });
      invalidate();
    } else if (cityAttr && !v) {
      await api.deleteAttribute(cityAttr.id);
      invalidate();
    } else if (!cityAttr && v) {
      add.mutate({ type: "location", value: v });
    }
  }

  function toggleWork(w: string) {
    const existing = workTypes.get(w.toLowerCase());
    if (existing) remove.mutate(existing.id);
    else add.mutate({ type: "location", value: w });
  }

  return (
    <div className="location-row">
      <input
        className="inline-input"
        style={{ width: 160 }}
        placeholder="City or region…"
        value={city}
        onChange={(e) => setCity(e.target.value)}
        onBlur={persistCity}
        onKeyDown={(e) => e.key === "Enter" && persistCity()}
      />
      <div className="choice-row">
        {WORK_TYPES.map((w) => (
          <button
            key={w}
            className={`choice-btn${workTypes.has(w.toLowerCase()) ? " selected" : ""}`}
            onClick={() => toggleWork(w)}
          >
            {w}
          </button>
        ))}
      </div>
    </div>
  );
}
