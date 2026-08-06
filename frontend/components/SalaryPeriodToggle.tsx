"use client";

import { DISPLAY_PERIODS, setSalaryPeriod, useSalaryPeriod } from "@/lib/salary";

const LABEL: Record<string, string> = { year: "Yearly", hour: "Hourly" };

/**
 * Switches every salary chip on the page between yearly and hourly.
 *
 * Only rendered when at least one visible role has a PARSED salary — a toggle
 * over a page of "Competitive" chips does nothing, and offering it implies the
 * app knows more than it does. The state lives in lib/salary.ts rather than
 * here so /search and /my-roles share one choice.
 */
export function SalaryPeriodToggle({ show = true }: { show?: boolean }) {
  const period = useSalaryPeriod();
  if (!show) return null;
  return (
    <div className="choice-row" title="Show pay per year or per hour">
      {DISPLAY_PERIODS.map((p) => (
        <button
          key={p}
          className={`toggle sm ${period === p ? "on" : "off"}`}
          onClick={() => setSalaryPeriod(p)}
        >
          {LABEL[p]}
        </button>
      ))}
    </div>
  );
}
