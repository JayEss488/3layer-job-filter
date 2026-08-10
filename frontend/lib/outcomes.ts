import type { ApplicationStatus } from "@/lib/types";

/**
 * Application-outcome reporting: shared between the /my-roles Applied tab (where
 * the answer is given) and the /search banner (where the question is asked).
 *
 * Why any of this exists: Role.application_status has been in the schema since
 * the beginning and had never once been set past "pending". That was never a
 * problem with the control — it was that nobody had a reason to navigate back to
 * /my-roles → Applied and say what happened. It is also the only ground truth
 * this app will ever collect about whether a listing was real, and unlike a
 * ranking it cannot be reconstructed after the fact.
 */

/** The four outcomes that are EVENTS. "no_response" is an absence and is
 *  presented separately — see the /my-roles Applied tab. */
export const PRIMARY_OUTCOMES: ApplicationStatus[] = [
  "pending",
  "interview",
  "offer",
  "rejected",
];

/**
 * After this many days with no reported outcome, ask.
 *
 * Three weeks: long enough that silence has started to mean something, short
 * enough that the user still remembers applying. Note what this number is NOT —
 * it is not a claim that no reply within 21 days indicates a ghost listing.
 * Most applications get no reply for entirely ordinary reasons, which is why
 * `no_response` is only ever read as a rate across many rows conditioned on a
 * fired signal, never as proof about one listing.
 */
export const NO_RESPONSE_PROMPT_DAYS = 21;

export function daysSince(iso?: string | null): number {
  if (!iso) return 0;
  return Math.floor((Date.now() - new Date(iso).getTime()) / 86400000);
}

/** An applied role that has sat at "pending" long enough to be worth asking
 *  about. */
export function awaitingOutcome(r: {
  application_status?: ApplicationStatus | null;
  applied_at?: string | null;
}): boolean {
  return (
    (r.application_status ?? "pending") === "pending" &&
    daysSince(r.applied_at) >= NO_RESPONSE_PROMPT_DAYS
  );
}
