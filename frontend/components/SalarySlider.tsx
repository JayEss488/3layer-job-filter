"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

const MIN = 0;
const MAX = 200000;
const STEP = 5000;

function fmt(n: number) {
  if (n >= MAX) return "£200k+";
  return `£${Math.round(n / 1000)}k`;
}

function parse(value?: string): [number, number] {
  if (!value) return [MIN, MAX];
  const nums = value.replace(/[^0-9-]/g, "").split("-").map(Number);
  if (nums.length === 2 && !nums.some(isNaN)) return [nums[0], nums[1]];
  return [MIN, MAX];
}

/** Range slider persisted as one salary attribute "min-max". */
export function SalarySlider({
  profileId,
  attribute,
}: {
  profileId: number;
  attribute?: Attribute;
}) {
  const { invalidate } = useAttributeMutations(profileId);
  const [[lo, hi], setRange] = useState<[number, number]>(parse(attribute?.value));

  useEffect(() => {
    setRange(parse(attribute?.value));
  }, [attribute?.value]);

  async function persist(next: [number, number]) {
    const value = `${next[0]}-${next[1]}`;
    if (attribute) await api.updateAttribute(attribute.id, { value });
    else await api.addAttribute(profileId, { type: "salary", value, confirmed: true });
    invalidate();
  }

  const loPct = ((lo - MIN) / (MAX - MIN)) * 100;
  const hiPct = ((hi - MIN) / (MAX - MIN)) * 100;

  return (
    <div className="slider">
      <div className="track">
        <div className="rail" />
        <div className="fill" style={{ left: `${loPct}%`, width: `${hiPct - loPct}%` }} />
        <input
          type="range"
          min={MIN}
          max={MAX}
          step={STEP}
          value={lo}
          onChange={(e) => setRange([Math.min(Number(e.target.value), hi - STEP), hi])}
          onMouseUp={() => persist([lo, hi])}
          onTouchEnd={() => persist([lo, hi])}
        />
        <input
          type="range"
          min={MIN}
          max={MAX}
          step={STEP}
          value={hi}
          onChange={(e) => setRange([lo, Math.max(Number(e.target.value), lo + STEP)])}
          onMouseUp={() => persist([lo, hi])}
          onTouchEnd={() => persist([lo, hi])}
        />
      </div>
      <div className="scale">
        <span>{fmt(lo)}</span>
        <span>{fmt(hi)}</span>
      </div>
    </div>
  );
}
