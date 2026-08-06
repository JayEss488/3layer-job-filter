"use client";

import { useSyncExternalStore } from "react";

import type { Role, SalaryPeriod } from "./types";

/**
 * Rendering normalised pay, and the yearly/hourly unit the user has chosen.
 *
 * Boards state pay in whatever unit suits them — "£32,000 per annum" next to
 * "£200 per day" next to "£25.50 per hour" — which makes two listings on the
 * same screen genuinely incomparable at a glance. The backend
 * (services/salary.py) parses each into {min, max, period}; this converts
 * between periods for display.
 *
 * MUST STAY IN SYNC with services/salary.py's ANNUAL_MULTIPLIER. The two are
 * separate because the conversion is needed on both sides (the backend for the
 * salary floor, here for the toggle) and there is no shared module between
 * them — same reason config.WORK_TYPE_VALUES is mirrored in LocationPicker.
 */
export const ANNUAL_MULTIPLIER: Record<SalaryPeriod, number> = {
  year: 1,
  month: 12,
  week: 52,
  day: 260,
  hour: 1950, // 37.5h/week x 52
};

const PERIOD_SUFFIX: Record<SalaryPeriod, string> = {
  year: "a year",
  month: "a month",
  week: "a week",
  day: "a day",
  hour: "an hour",
};

const CURRENCY_SYMBOL: Record<string, string> = {
  GBP: "£",
  USD: "$",
  EUR: "€",
  INR: "₹",
  JPY: "¥",
};

/** The two units worth offering. Anything else a listing states is converted
 *  into one of them rather than adding a button nobody asked for. */
export const DISPLAY_PERIODS: SalaryPeriod[] = ["year", "hour"];

// ── The chosen display period, shared across every card on the page ─────────
// A module-level store read through useSyncExternalStore, deliberately not a
// context: the toggle sits on /search and /my-roles and the cards are rendered
// several components deep in both, so a provider would have to be threaded
// through two page trees to move one enum. Persisted so the choice survives a
// navigation between those two pages (and a reload).

const STORAGE_KEY = "fiat.salaryPeriod";
let current: SalaryPeriod = "year";
let hydrated = false;
const listeners = new Set<() => void>();

function subscribe(fn: () => void) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function getSnapshot(): SalaryPeriod {
  if (!hydrated && typeof window !== "undefined") {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (stored === "year" || stored === "hour") current = stored;
    hydrated = true;
  }
  return current;
}

/** Server render has no localStorage; returning the default keeps the first
 *  client render identical to the server's and avoids a hydration mismatch. */
function getServerSnapshot(): SalaryPeriod {
  return "year";
}

export function setSalaryPeriod(period: SalaryPeriod) {
  if (period === current) return;
  current = period;
  hydrated = true;
  if (typeof window !== "undefined") window.localStorage.setItem(STORAGE_KEY, period);
  listeners.forEach((fn) => fn());
}

export function useSalaryPeriod(): SalaryPeriod {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}

// ── Formatting ──────────────────────────────────────────────────────────────

function convert(amount: number, from: SalaryPeriod, to: SalaryPeriod): number {
  return (amount * ANNUAL_MULTIPLIER[from]) / ANNUAL_MULTIPLIER[to];
}

function round(amount: number, period: SalaryPeriod): string {
  // Whole pounds for anything annual-ish; pence matter at an hourly rate, where
  // £25.50 and £25 are a £975/year difference.
  if (period === "hour") return amount.toFixed(2).replace(/\.00$/, "");
  return Math.round(amount).toLocaleString("en-GB");
}

/**
 * The salary chip's text, or null when there's nothing to say.
 *
 * Falls back to `salary_text` verbatim whenever the parse produced no numbers —
 * "Competitive", "Negotiable" and "National Minimum Wage" state no figure, and
 * showing what the employer actually wrote beats showing nothing.
 *
 * A converted figure is marked with "~" and the source period is named, because
 * the conversion assumes full-time hours the listing never stated: an hourly
 * rate shown as a year is our estimate, not the employer's offer.
 */
export function formatSalary(role: Role, display: SalaryPeriod): string | null {
  const stated = role.salary_period;
  const min = role.salary_min ?? null;
  const max = role.salary_max ?? null;
  if (!stated || (min == null && max == null)) {
    return role.salary_text?.trim() || null;
  }
  const symbol = role.salary_currency ? CURRENCY_SYMBOL[role.salary_currency] ?? "" : "";
  const unit = role.salary_currency && !symbol ? `${role.salary_currency} ` : symbol;
  const show = (v: number) => `${unit}${round(convert(v, stated, display), display)}`;

  const body =
    min != null && max != null
      ? min === max
        ? show(min)
        : `${show(min)}–${show(max)}`
      : min != null
        ? `From ${show(min)}`
        : `Up to ${show(max as number)}`;

  const converted = stated !== display;
  return `${converted ? "~" : ""}${body} ${PERIOD_SUFFIX[display]}${
    converted ? ` (stated ${PERIOD_SUFFIX[stated]})` : ""
  }`;
}
