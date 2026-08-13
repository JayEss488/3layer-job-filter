/**
 * One place that knows how the API's timestamps are shaped.
 *
 * THE PROBLEM THIS EXISTS FOR. The backend stores naive UTC datetimes and
 * Pydantic serialises them with no offset suffix — "2026-08-12T16:21:18", and
 * from a raw SQLite read sometimes space-separated. ES2015+ specifies that a
 * date-TIME string without an offset is parsed as LOCAL time, so plain
 * `new Date(value)` mis-reads every one of them by the viewer's UTC offset,
 * always in the direction that makes the value look older than it is.
 *
 * An hour sounds harmless and is not, because these values are then rendered as
 * DAYS and DATES. An hour is enough to move a listing across midnight and
 * change the date on screen — which is exactly how a role Adzuna dated 12 Aug
 * came to be shown as "Posted today" on the 13th.
 *
 * The fix was already written once, privately, inside SearchProgress (its
 * elapsed clock needed it first). It now lives here because four call sites
 * needed the same rule and three of them did not have it — the failure mode
 * this repo names elsewhere as the SOFT_GATE_AXES lesson: two modules quietly
 * disagreeing about a shared fact.
 */

/** Parse an API timestamp as UTC. Null for anything unparseable, so callers can
 *  fall through to "say nothing" rather than rendering "Invalid Date". */
export function parseApiDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const normalized = value.trim().replace(" ", "T");
  // A DATE-only value ("2026-08-26") is already specified as UTC — appending a
  // Z would be harmless but appending it to an offset-bearing value would not.
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/.test(normalized);
  const isDateOnly = /^\d{4}-\d{2}-\d{2}$/.test(normalized);
  const d = new Date(hasZone || isDateOnly ? normalized : `${normalized}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Milliseconds since the epoch, or NaN. For arithmetic where a bad value is
 *  already handled downstream (sorting, elapsed clocks). */
export function apiTime(value: string | null | undefined): number {
  return parseApiDate(value)?.getTime() ?? NaN;
}

/**
 * Whole days between two instants, counted as CALENDAR days in the viewer's own
 * timezone — not as elapsed milliseconds divided by 86,400,000.
 *
 * The difference is the whole point. A listing stamped 12 Aug 16:21, opened at
 * 07:00 on 13 Aug, is 14.7 hours old: elapsed-time maths floors that to 0 and
 * the card said "Posted today" about an advert dated yesterday. Anyone who
 * clicks through to check sees a different date and reads the app as broken —
 * which it was. "Today" is a claim about the DATE, so it has to be computed
 * from dates.
 */
export function calendarDaysBetween(from: Date, to: Date): number {
  const startOfDay = (d: Date) =>
    new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  return Math.round((startOfDay(to) - startOfDay(from)) / 86400000);
}
