"use client";

import { useState } from "react";

import { calendarDaysBetween, parseApiDate } from "@/lib/dates";
import { useAttributes } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";
import { formatSalary, useSalaryPeriod } from "@/lib/salary";
import { VERDICT_DOTS } from "@/lib/types";
import type { Role, RoleVerdict, SalaryPeriod } from "@/lib/types";

interface Props {
  role: Role;
  /** Optional rank shown on the left (search page). */
  showRank?: boolean;
  /** Extra className for the card (crossed / ignored / dim). */
  variant?: "" | "crossed" | "ignored" | "dim";
  /** Render the analysis block (search page only). */
  showAnalysis?: boolean;
  /** Right-aligned meta in the header (e.g. "Applied 24 Jun"). */
  meta?: React.ReactNode;
  /** The action row content. */
  actions?: React.ReactNode;
  /** Indent actions under the rank column (search layout). */
  indentActions?: boolean;
}

/** Section markers emitted by engine._compose_analysis. */
const SIGNAL = "§signal";
const QUALIFICATION = "§qualification";
/** Retired at FINAL_EVAL_PROMPT_VERSION 23 — still parsed so verdicts judged
 *  under 22 or earlier keep rendering their narrative until re-judged. */
const AI_REASONING = "§ai-reasoning";
const APPLY_HIGHLIGHTS = "§apply-highlights";
const REQUIREMENTS = "§requirements";
const THE_ROLE = "§the-role";
const GHOST = "§ghost";
const CAUTION = "§caution";

interface Analysis {
  /** The judge's role-type + summary sentence pair, joined into one headline.
   *  Never rendered by the card-redesign layout, not even behind "Show more"
   *  — see the component below. Kept parsed only because it's free to keep
   *  parsing and a future design might want it back. */
  headline: string | null;
  /** Cluster note / "closest available match" warning — always precede the headline. */
  notes: string[];
  /** Un-sectioned body lines from a verdict judged before these markers existed
   *  (or under a retired marker, e.g. the old separately-shown role-type block). */
  legacy: string[];
  /** "+ .../- ..." pair (FINAL_EVAL_PROMPT_VERSION 31) — short compressions of
   *  can_do_fit and the first concerns item, written by the judge itself for
   *  exactly this line. The card's always-visible +/- line pair reads from
   *  here first; see posText/negText in the component below for the fallback
   *  on a pre-31 row that has no `§signal` block. */
  signal: string[];
  /** Qualified-or-not verdict ("✓ ..." line). Never rendered by the
   *  card-redesign layout (see `signal` above) — kept parsed for the same
   *  reason as `headline`. */
  qualificationVerdict: string[];
  /** Concern count + bullets ("⚠ ..."/"- ..." lines), plus the strengths list
   *  when present. Never rendered by the card-redesign layout — kept parsed
   *  as the fallback source for `negText` on a pre-31 row, see
   *  splitQualification below. */
  qualification: string[];
  /** What this employer screens on, mapped one line per ask to the candidate's own
   *  evidence for it (a named gap where they have none), plus an optional framing
   *  sentence — behind "Show more". Rows judged before FINAL_EVAL_PROMPT_VERSION 29
   *  carry the older single "This role likely filters on: …" lead instead. */
  applyHighlights: string[];
  /** The judge's full requirements checklist as "✓ ..."/"✗ ..." lines, one per ask
   *  — the card's always-visible "fit checklist" column. Distinct from
   *  `applyHighlights`, which is capped at 5 and pairs an ask with the candidate's
   *  evidence: this is the inventory, so a reader can see what was found rather
   *  than inferring it from a truncated mapping. Absent on rows judged before the
   *  marker existed, which simply don't render it. */
  requirements: string[];
  /** 2-3 short duty phrases (FINAL_EVAL_PROMPT_VERSION 30) — the card's
   *  always-visible "the role" column, beside `requirements`. Absent on any row
   *  judged before 30. */
  roleDuties: string[];
  /** Pre-v23 narrative paragraph — behind "Show more". Replaced by
   *  `applyHighlights`; only ever populated on a not-yet-re-judged row. */
  aiReasoning: string[];
  /** Why the ghost chip fired — behind "Show more". The chip on its own is an
   *  unexplainable accusation about a named employer, so it must always be
   *  backed by the specific facts. */
  ghost: string[];
  /** Why the caution chip fired — behind "Show more". Same contract as `ghost`:
   *  the chip alone is an unexplainable accusation about a named employer, so it
   *  is only ever shown backed by a sentence saying what was actually checked.
   *  Set when the judge flagged a possible CV-farming/lead-gen listing and
   *  cross-site corroboration came back inconclusive — which is not clearance. */
  caution: string[];
}

/**
 * Splits the analysis into its sections. `qualificationVerdict` always
 * renders; `qualification`/`aiReasoning` hide behind the "Show more" toggle
 * (see hasNewSections in the component below).
 *
 * Rows judged under an older FINAL_EVAL_PROMPT_VERSION carry none of these
 * markers (or an earlier version's now-unrecognised ones, e.g. the retired
 * always-visible `§role-type` block) and stay on screen until next
 * re-judged — anything after the headline that isn't one of the current
 * markers falls through to `legacy` and renders flat, so an old result
 * doesn't quietly lose its reasoning. An unrecognised `§`-prefixed marker
 * from a since-retired format is dropped rather than shown as literal
 * marker text.
 */
function parseAnalysis(text: string): Analysis {
  const out: Analysis = {
    headline: null, notes: [], legacy: [], signal: [], qualificationVerdict: [], qualification: [],
    applyHighlights: [], requirements: [], roleDuties: [], aiReasoning: [], ghost: [], caution: [],
  };
  let bucket: "lead" | "signal" | "qualification" | "applyHighlights" | "requirements" | "roleDuties"
    | "aiReasoning" | "ghost" | "caution" = "lead";
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    if (line === SIGNAL) {
      bucket = "signal";
    } else if (line === QUALIFICATION) {
      bucket = "qualification";
    } else if (line === APPLY_HIGHLIGHTS) {
      bucket = "applyHighlights";
    } else if (line === REQUIREMENTS) {
      bucket = "requirements";
    } else if (line === THE_ROLE) {
      bucket = "roleDuties";
    } else if (line === AI_REASONING) {
      bucket = "aiReasoning";
    } else if (line === GHOST) {
      bucket = "ghost";
    } else if (line === CAUTION) {
      bucket = "caution";
    } else if (bucket === "lead" && line.startsWith("§")) {
      // A marker from a retired format -- skip rather than show it as text.
      continue;
    } else if (bucket === "lead") {
      // engine._compose_analysis always emits notes before the headline, so the
      // first line that isn't one is the headline and everything after it is body.
      if (out.headline === null && !line.startsWith("Matched via:") && !line.startsWith("⚠"))
        out.headline = line;
      else if (out.headline === null) out.notes.push(line);
      else out.legacy.push(line);
    } else if (bucket === "qualification") {
      // Two kinds of "✓" line, and they belong in different buckets. The bare
      // verdict sentence (engine._compose_analysis's `can_do_fit`) is the
      // always-visible headline; "✓ You have:" is the heading of the strengths
      // bullet list, which must stay with its own "- " bullets behind "Show
      // more" — otherwise the heading renders up top with its list collapsed
      // somewhere else. A trailing colon is what distinguishes them.
      const isListHeading = line.endsWith(":");
      (line.startsWith("✓") && !isListHeading
        ? out.qualificationVerdict
        : out.qualification
      ).push(line);
    } else {
      out[bucket].push(line);
    }
  }
  return out;
}

/**
 * Splits `Analysis.qualification` (the flat "✓ You have: / - .. / ⚠ Note
 * that: / - .." sequence engine._compose_analysis emits) back into its two
 * source lists. Done here rather than by adding new §-markers on the backend
 * because a marker split would only cover rows judged AFTER the change —
 * every already-persisted verdict would still arrive in this flat shape, so
 * the parsing has to handle it either way. `concerns[0]` is what feeds the
 * card's always-visible "-" line (see negText in the component below); the
 * full lists still render behind "Show more" exactly as before, so the top
 * concern is deliberately shown twice rather than sliced out of the detail
 * view.
 */
function splitQualification(qualification: string[]): { strengths: string[]; concerns: string[] } {
  const strengths: string[] = [];
  const concerns: string[] = [];
  let bucket: "none" | "strengths" | "concerns" = "none";
  for (const line of qualification) {
    if (line === "✓ You have:") {
      bucket = "strengths";
    } else if (line === "⚠ Note that:") {
      bucket = "concerns";
    } else if (line.startsWith("- ")) {
      (bucket === "strengths" ? strengths : bucket === "concerns" ? concerns : null)?.push(
        line.slice(2),
      );
    }
  }
  return { strengths, concerns };
}

/** Mirrors full_auto._humanise_days: plain day count under 2 months, else months. */
function humaniseDays(days: number): string {
  const months = Math.floor(days / 30);
  return months >= 2 ? `${months} months` : `${days} day${days === 1 ? "" : "s"}`;
}

/**
 * Single age/closing-date chip, pure date math off Role.posted_at/expires_at/
 * posted_at_approx -- no AI involved, same fields the pipeline already carries
 * for its own prompts (full_auto._listing_age_tag), just finally rendered.
 * Closing-date info takes priority when it's actually actionable (passed or
 * imminent); otherwise falls back to posting age. Null on both means the
 * source stated no date at all, which is common and must render as no chip,
 * never a guessed one.
 *
 * WHAT THIS STILL CANNOT FIX, and should not pretend to: the chip is only ever
 * as good as the date the source claimed. An aggregator's date is when IT
 * ingested the ad, not when the employer posted it -- Adzuna reported 12 Aug
 * for a posting the employer's own page dates 11 Aug. Nothing here can recover
 * the employer's date, so the chip is deliberately worded as a plain day count
 * and never as a precise date the reader could check against the advert.
 */
function ageChip(role: Role): string | null {
  const now = new Date();
  if (role.expires_at) {
    const expires = parseApiDate(role.expires_at);
    // Calendar days, so an ad closing "on the 13th" is still open all of the
    // 13th rather than reading as passed from one minute after midnight.
    const daysLeft = expires ? calendarDaysBetween(now, expires) : null;
    if (daysLeft !== null) {
      if (daysLeft < 0) return "Closing date passed";
      if (daysLeft === 0) return "Closes today";
      if (daysLeft === 1) return "Closes tomorrow";
      if (daysLeft <= 7) return `Closes in ${daysLeft} days`;
    }
  }
  if (role.posted_at) {
    const posted = parseApiDate(role.posted_at);
    if (!posted) return null;
    const daysAgo = calendarDaysBetween(posted, now);
    if (daysAgo < 0) return null; // clock skew or a future-dated source value -- say nothing rather than guess
    const approx = !!role.posted_at_approx;
    // Greenhouse etc.: this is the board's last-updated time, not a stated
    // posting date -- must never claim "posted", see Role.posted_at_approx.
    const verb = approx ? "Updated" : "Posted";
    if (daysAgo === 0) return `${verb} today`;
    if (daysAgo === 1) return `${verb} yesterday`;
    return `${verb} ${approx ? "~" : ""}${humaniseDays(daysAgo)} ago`;
  }
  return null;
}

/**
 * Straight-line distance from the candidate's stated place, when it's known.
 *
 * Null distance means the listing's location couldn't be resolved (or the
 * candidate has stated no place) — the common case, and it renders as no chip.
 * That is deliberately NOT the same as "far": see services/geo.py.
 *
 * 0 is rendered as "Nearby" rather than "0 miles away", because outcode
 * centroids can't tell same-town from same-street and shouldn't pretend to.
 */
function distanceChip(role: Role): string | null {
  const miles = role.distance_miles;
  if (miles == null) return null;
  if (miles <= 1) return "Nearby";
  return `${miles} miles away`;
}

/*
 * There is deliberately no "Not checked live" / "Checked live" chip here any more.
 *
 * It was the inverse of an earlier positive badge: since engine._verify_final_picks
 * verifies every final pick unconditionally, "Checked live" was a constant and so
 * carried no information, and the chip was flipped to mark the absence instead. But
 * the absence turned out not to be worth a slot either — it is true of every
 * provisional and quick-scored row by construction, so it reads as a caveat about
 * THIS role while actually describing which section of the page the card is in.
 *
 * The backend is untouched: last_verified_at is still stamped, UNVERIFIED_RANK_PENALTY
 * still demotes an unverifiable row in _selection_score, and dead listings are still
 * dropped before they can be shown. This was display only.
 */

/**
 * Visa sponsorship, which is TWO different questions and used to be shown as
 * one badge reading "Visa sponsor":
 *
 *   1. Does this EMPLOYER hold a Home Office licence (`sponsor_licensed`)?
 *   2. Will THIS VACANCY be sponsored (`sponsor_statement`, the listing's own
 *      words)?
 *
 * Only (2) is what the candidate actually needs, and the register can never
 * answer it: 21 rows in the measured store are a licensed employer whose advert
 * states it will not sponsor the role, and every one of those carried the old
 * badge. So the listing's own statement outranks the register in both
 * directions, and the register's answer is now worded as what it is — a fact
 * about the employer, not a promise about the job.
 *
 * Three states earn a chip; silence earns none:
 *   "Sponsorship offered"      — the listing says so. Strongest, rare (7 rows).
 *   "No sponsorship"           — the listing says so. The most valuable of the
 *                                three: it is the one that saves an application.
 *   "Employer sponsors visas"  — licensed, listing silent. Deliberately NOT
 *                                "Visa sponsor": it says whose property this is.
 *
 * A false `sponsor_licensed` still earns nothing. "Not on the register" is not
 * "does not sponsor" for an agency-posted or vaguely-named listing, and null
 * means there was no employer name to check at all — an absent badge reads
 * correctly as "unknown", a "Not a sponsor" badge would not. That is why the
 * negative chip fires on the listing's STATEMENT and never on the register.
 *
 * All of it shown ONLY while the candidate's own sponsors-only filter is on.
 * The backend stamps these every run regardless (they are free, and the filter
 * can be switched on later), but for a candidate who does not need a visa they
 * answer a question never asked, in the same row as facts about the job.
 */
function sponsorChip(role: Role, filterOn: boolean): string | null {
  if (!filterOn) return null;
  if (role.sponsor_statement === "offered") return "Sponsorship offered";
  if (role.sponsor_statement === "not_offered") return "No sponsorship";
  return role.sponsor_licensed === true ? "Employer sponsors visas" : null;
}

/**
 * Whether the candidate has the sponsors-only filter switched on. Read here
 * rather than threaded down from the pages: RoleCard has twelve call sites
 * across /search and /my-roles, and TanStack dedupes the attributes query to
 * one request however many cards mount. Mirrors VisaSponsorToggle's read of the
 * same single-value attribute — no row at all means off, which is the default.
 */
function useSponsorFilterOn(): boolean {
  const { activeId } = useProfiles();
  const { data } = useAttributes(activeId ?? null);
  return (data?.by_type?.visa_sponsor_only ?? []).some(
    (a) => a.value.toLowerCase() === "true",
  );
}

/**
 * Ghost-listing risk — an advert that may have no real vacancy behind it
 * (already filled, a standing CV-collection pipeline, a cancelled req never
 * taken down). See backend services/ghost.py for the rules.
 *
 * Note the three-state semantics are INVERTED from sponsorChip above, and the
 * inversion is only honest because of a property of the rules. There, absence
 * must read as "unknown", so only a positive is badged. Here the badge is a
 * NEGATIVE, so absence has to read as "nothing fired" — which holds because
 * every ghost rule fires on POSITIVE evidence and none fires on missing data
 * (a listing with no posting date produces no signal at all). If a rule is ever
 * added that fires on absence, this chip and the "none flagged" line on /search
 * both become lies.
 *
 * "medium" deliberately does not say "ghost": one ordinary signal is not an
 * accusation, and the card's expandable reasons carry the specifics.
 *
 * `hasNotes` is the legacy path, and it exists because the two halves of this
 * feature reached the database one run apart: roles persisted while
 * _compose_analysis emitted §ghost but Role.ghost_level was not yet written
 * carry the reasons ("This advert has been running for over three months") with
 * no level beside them, so they rendered the explanation for a badge that never
 * appeared. Those rows fall back to the SOFTER chip even though the missing
 * level may have been "high": the reasons are recoverable from the stored text,
 * the tier is not, and understating an accusation about a named employer is the
 * safe direction to be wrong in. New rows always have a level and never take
 * this path.
 */
function ghostChip(role: Role, hasNotes: boolean): string | null {
  if (role.ghost_level === "high") return "Possible ghost listing";
  // Not "posted N ago" — ageChip already says that, from the same date.
  if (role.ghost_level === "medium") return "Long-running listing";
  if (role.ghost_level == null && hasNotes) return "Long-running listing";
  return null;
}

/**
 * The judge flagged this listing as a possible CV-farming / lead-generation
 * posting rather than a real vacancy, and cross-site corroboration came back
 * inconclusive. Inconclusive is NOT clearance — before this chip existed such a
 * listing was shown with no trace of the flag at all, and one reached the user
 * at fit_rank 1 badged "Very strong fit".
 *
 * Driven off the presence of the §caution block rather than a column, because
 * the block IS the payload: the chip is an accusation about a named employer and
 * may never appear without the sentence explaining what was actually checked.
 * Same contract as ghostChip's `hasNotes` path above.
 *
 * Wording states only what we failed to establish. A listing that was genuinely
 * corroborated as a scam never gets here — it is dropped upstream.
 */
function cautionChip(hasCautionNotes: boolean): string | null {
  return hasCautionNotes ? "Employer not verified" : null;
}

/** A fact chip. `warn` is a caveat about the listing rather than a fact about
 *  the job, and is styled apart — see the note in factChips. */
type FactChip = { text: string; warn?: boolean };

function factChips(
  role: Role,
  salaryPeriod: SalaryPeriod,
  sponsorFilterOn: boolean,
  hasGhostNotes: boolean,
  hasCautionNotes: boolean,
): FactChip[] {
  // Mostly what the AI actually read off the listing — a null means the
  // listing was silent, and no chip is better than a guessed one. While
  // provisional, the cheap rank stage's estimate is the only fit signal there
  // is — surface it honestly as an estimate (it disappears when the real
  // verdict lands). The estimate shows while provisional AND on a retained
  // "quick-scored only" row (provisional false, stage still "rank") -- there
  // it is the only fit signal that role will ever have, so hiding it would
  // leave a bare card. ageChip is the one exception: pure date math off the
  // source's own posted_at/expires_at, no AI involved — see ageChip above.
  //
  // The ghost chip carries `warn`, and that is the whole reason this returns
  // objects rather than strings. Every other entry here is a neutral fact about
  // the job, and rendered in the same grey pill as those, "Long-running
  // listing" sat between the salary and the work style and did not read as a
  // flag at all — the user's report was that the only visible trace of a
  // flagged listing was the explanation inside "Show more". Styling it apart is
  // what makes the chip do the job the explanation is backing up.
  const chip = (text: string | null, warn = false): FactChip | null =>
    text && text.trim() ? { text, warn } : null;
  return [
    chip(
      role.rank_score != null && (role.provisional || role.provisional_stage === "rank")
        ? `Fit estimate ${role.rank_score}/100`
        : null,
    ),
    chip(ageChip(role)),
    // These two are caveats about whether the listing is what it appears to be —
    // "is there a job behind it" and "is this employer what it says" — so they sit
    // together rather than scattered among the neutral facts, and both are styled
    // as warnings.
    chip(ghostChip(role, hasGhostNotes), true),
    chip(cautionChip(hasCautionNotes), true),
    chip(sponsorChip(role, sponsorFilterOn)),
    chip(distanceChip(role)),
    // Normalised into the user's chosen unit where the backend could parse it,
    // falling back to whatever the employer wrote when it couldn't.
    chip(formatSalary(role, salaryPeriod)),
    chip(role.work_style ?? null),
    chip(role.seniority_level ?? null),
    chip(role.deadline_text ? `Apply by ${role.deadline_text}` : null),
  ].filter((v): v is FactChip => v !== null);
}

/** Screen-reader-only text for FitDots below. Unlike VERDICT_LABEL this covers
 *  all four grades -- withholding a word for "ok"/"stretch" is specifically
 *  about not putting a discouraging label in front of a sighted reader
 *  alongside the role; it was never about hiding the grade from assistive
 *  tech, which has no such effect to avoid. */
const FIT_DOTS_LABEL: Record<RoleVerdict, string> = {
  very_strong: "Very strong fit",
  strong: "Strong fit",
  ok: "Ok fit",
  stretch: "Stretch fit",
};

/**
 * Non-verbal fit-grade meter — replaces the old text badge (VERDICT_LABEL)
 * in the card corner. VERDICT_LABEL deliberately has no text for "ok"/
 * "stretch" (a printed "Ok fit"/"Stretch fit" told the candidate to discount
 * a role the judge had just verified as worth applying to), which left those
 * two grades with nothing at all in the corner. A filled-dot count conveys
 * the same graded signal for all four grades without a word to read as
 * discouraging.
 */
function FitDots({ verdict }: { verdict: RoleVerdict }) {
  const filled = VERDICT_DOTS[verdict];
  return (
    <div className="fit-dots" role="img" aria-label={FIT_DOTS_LABEL[verdict]}>
      {[0, 1, 2, 3].map((i) => (
        <span key={i} className={`fit-dot${i < filled ? " filled" : ""}`} />
      ))}
    </div>
  );
}

export function RoleCard({
  role,
  showRank = false,
  variant = "",
  showAnalysis = false,
  meta,
  actions,
  indentActions = false,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const salaryPeriod = useSalaryPeriod();
  const sponsorFilterOn = useSponsorFilterOn();
  // location_label is the readable form of a location the source gave as a raw
  // postcode ("B706AW" -> "Sandwell"). Null on most rows, where `location` is
  // already a place name and needs no help.
  const companyLine = [role.company, role.location_label || role.location]
    .filter(Boolean)
    .join(" — ");
  const a = role.ai_analysis ? parseAnalysis(role.ai_analysis) : null;
  const hasNewSections =
    !!a &&
    (a.signal.length > 0 ||
      a.qualificationVerdict.length > 0 ||
      a.qualification.length > 0 ||
      a.applyHighlights.length > 0 ||
      a.requirements.length > 0 ||
      a.roleDuties.length > 0 ||
      a.aiReasoning.length > 0 ||
      a.ghost.length > 0 ||
      a.caution.length > 0);
  // What's left for the "Show more" toggle: everything the card-redesign layout
  // doesn't show up front. `requirements`/`roleDuties` are the always-visible
  // columns and `headline`/`qualification` are never shown at all (not even
  // here) -- see the render below and RoleCard's module docstring notes on
  // `signal`/`qualificationVerdict`/`qualification` above.
  const hasExpandable =
    !!a &&
    (a.legacy.length > 0 ||
      a.applyHighlights.length > 0 ||
      a.aiReasoning.length > 0 ||
      a.ghost.length > 0 ||
      a.caution.length > 0);
  const hasSponsorNote =
    sponsorFilterOn && !!role.sponsor_statement && !!role.sponsor_statement_quote;
  const hasNotes = !!a && (hasSponsorNote || a.notes.length > 0);
  const facts = factChips(
    role, salaryPeriod, sponsorFilterOn,
    !!a && a.ghost.length > 0, !!a && a.caution.length > 0,
  );
  const verdict = role.verdict as RoleVerdict | null | undefined;
  // The +/- line pair. Preferred source is `§signal` (FINAL_EVAL_PROMPT_VERSION
  // 31) -- short "+ .../- ..." lines the judge wrote FOR this line, already
  // short enough to read as a headline. A pre-31 row has no `§signal` block, so
  // falls back to deriving a (longer) pair from `qualificationVerdict`
  // (can_do_fit) and the first `qualification` concern -- see
  // splitQualification's docstring.
  const signalPos = a?.signal.find((l) => l.startsWith("+ "));
  const signalNeg = a?.signal.find((l) => l.startsWith("- "));
  const posText =
    signalPos?.slice(2).trim() ||
    a?.qualificationVerdict[0]?.replace(/^✓\s*/, "").trim() ||
    null;
  const negText =
    signalNeg?.slice(2).trim() ||
    (a ? splitQualification(a.qualification).concerns[0] : null) ||
    null;
  // How much bigger one line reads than the other: a very_strong/strong grade
  // means the positive clearly outweighs the negative (fit_level is derived
  // mechanically from the same checklist that drives this, see full_auto's
  // rubric), "ok" is a genuine toss-up, and "stretch" means the concern is the
  // dominant fact about the role.
  const signalDominance: "pos" | "neg" | "equal" =
    verdict === "very_strong" || verdict === "strong"
      ? "pos"
      : verdict === "stretch"
        ? "neg"
        : "equal";

  return (
    <div className={`card${variant ? ` ${variant}` : ""}`}>
      <div className="card-top">
        {showRank && <div className="card-rank">{role.fit_rank ?? "·"}</div>}
        <div className="card-main">
          <div className="card-title">{role.title}</div>
          {companyLine && <div className="card-company">{companyLine}</div>}
          {facts.length > 0 && (
            <div className="card-tags">
              {facts.map((f, i) => (
                <span className={f.warn ? "tag tag-warn" : "tag"} key={i}>
                  {f.warn ? `⚠ ${f.text}` : f.text}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="card-corner">
          {role.provisional ? (
            // An embedding-stage card has had NO model look at it — saying
            // "Verifying…" would imply a review is underway on this specific
            // role when it may never be examined at all.
            <span className="verdict v-verifying">
              {role.provisional_stage === "embed" ? "Not yet reviewed" : "Verifying…"}
            </span>
          ) : role.provisional_stage === "embed" ? (
            // A pinned leftover from an interrupted run (see
            // engine._retain_interrupted_provisional): still true that no model
            // has looked at it, same label as the live provisional case above.
            <span className="verdict v-verifying">Not yet reviewed</span>
          ) : role.provisional_stage === "rank" ? (
            <span className="verdict v-verifying">Quick-scored only</span>
          ) : (
            verdict && <FitDots verdict={verdict} />
          )}
          {meta && <div className="applied-meta">{meta}</div>}
        </div>
      </div>

      {showAnalysis && a && (
        <>
          {/* The employer's own sentence behind the sponsorship chip, plus any
              cluster-label/closest-match notes — always visible, never behind
              "Show more". Shown because a chip asserting something this
              consequential should be checkable in one glance, and because "No
              sponsorship" is a claim that costs the candidate a role if we got
              it wrong, so the words that produced it belong on the card, not
              in a log. Same filter gating as the chip itself. */}
          {hasNotes && (
            <div className={`card-notes${indentActions ? "" : " flush"}`}>
              {hasSponsorNote && (
                <div className="an-note">
                  The listing itself says:{" "}
                  <em>&ldquo;{role.sponsor_statement_quote}&rdquo;</em>
                </div>
              )}
              {a.notes.map((n, i) => (
                <div className="an-note" key={i}>
                  {n}
                </div>
              ))}
            </div>
          )}

          {/* The card's headline read: what's going for it, then what isn't.
              "+" is the qualification verdict; "-" is the single most
              sink-worthy concern. Sized by how one-sided the grade is — a
              very_strong/strong pick makes the positive the bigger line, a
              stretch pick makes the negative the bigger line, and "ok" (a
              genuine toss-up) keeps them equal. See signalDominance above. */}
          {(posText || negText) && (
            <div className={`card-signals${indentActions ? "" : " flush"}`}>
              {posText && (
                <div
                  className={`signal-line signal-pos${
                    signalDominance === "pos" ? " emph" : signalDominance === "neg" ? " mute" : ""
                  }`}
                >
                  <span className="signal-icon" aria-hidden="true">+</span> {posText}
                </div>
              )}
              {negText && (
                <div
                  className={`signal-line signal-neg${
                    signalDominance === "neg" ? " emph" : signalDominance === "pos" ? " mute" : ""
                  }`}
                >
                  <span className="signal-icon" aria-hidden="true">−</span> {negText}
                </div>
              )}
            </div>
          )}

          {/* "fit checklist" mirrors §requirements (the judge's step-D checklist,
              ✓/✗ per ask) and "the role" is §the-role (role_duties,
              FINAL_EVAL_PROMPT_VERSION 30) — the two always-visible columns the
              card-redesign is built around. Either can be empty on its own
              (an older row has no role_duties; a row with no parseable
              checklist has no requirements) without suppressing the other. */}
          {(a.requirements.length > 0 || a.roleDuties.length > 0) && (
            <div
              className={`card-columns${indentActions ? "" : " flush"}${
                a.requirements.length > 0 && a.roleDuties.length > 0 ? "" : " single"
              }`}
            >
              {a.requirements.length > 0 && (
                <div className="card-col">
                  {/* Deliberately headed "found in the posting", not "everything
                      the posting asks for". This is the judge's extraction, and
                      it is not guaranteed complete -- the heading should not
                      make a claim the list can't keep. */}
                  <div className="an-h">Fit checklist</div>
                  {a.requirements.map((l, i) => {
                    // engine.requirements_block always emits "✓ "/"✗ " as the
                    // first two characters -- split that off into its own
                    // coloured span so a met/unmet ask reads at a glance (green
                    // check, red cross) while the rest of the line stays plain
                    // ink, same as the mockup. No count or ratio is rendered
                    // here or in engine._compose_analysis -- see the
                    // §requirements block there for why.
                    const met = l.startsWith("✓");
                    return (
                      <div key={i}>
                        <span className={`req-icon${met ? " req-icon-ok" : " req-icon-bad"}`} aria-hidden="true">
                          {l.charAt(0)}
                        </span>{" "}
                        {l.slice(1).trim()}
                      </div>
                    );
                  })}
                </div>
              )}
              {a.roleDuties.length > 0 && (
                <div className="card-col">
                  <div className="an-h">The role</div>
                  {a.roleDuties.map((l, i) => (
                    <div key={i}>{l.startsWith("- ") ? l.slice(2) : l}</div>
                  ))}
                </div>
              )}
            </div>
          )}

          {hasExpandable && (
            <div className={`card-analysis${indentActions ? "" : " flush"}`}>
              {(!hasNewSections || expanded) && (
                <>
                  {a.legacy.length > 0 && (
                    <div className="an-sec">
                      {a.legacy.map((l, i) => (
                        <div key={i} className={l.startsWith("⚠") ? "concern" : undefined}>
                          {l}
                        </div>
                      ))}
                    </div>
                  )}
                  {a.applyHighlights.length > 0 && (
                    <div className="an-sec">
                      <div className="an-h">What this role filters on</div>
                      {a.applyHighlights.map((l, i) => (
                        // Since FINAL_EVAL_PROMPT_VERSION 29 this block is one
                        // "- <requirement> — <evidence>" line per ask; the
                        // optional framing sentence, and every pre-v29 row's
                        // flat "This role likely filters on: …" lead, arrive
                        // unprefixed and render as plain paragraphs -- see
                        // globals.css's .an-map.
                        <div key={i} className={l.startsWith("- ") ? "an-map" : undefined}>
                          {l}
                        </div>
                      ))}
                    </div>
                  )}
                  {a.aiReasoning.length > 0 && (
                    <div className="an-sec">
                      <div className="an-h">AI reasoning</div>
                      {a.aiReasoning.map((l, i) => (
                        <div key={i}>{l}</div>
                      ))}
                    </div>
                  )}
                  {a.ghost.length > 0 && (
                    <div className="an-sec">
                      <div className="an-h">Why this was flagged</div>
                      {a.ghost.map((l, i) => (
                        <div key={i} className="concern">
                          {l}
                        </div>
                      ))}
                    </div>
                  )}
                  {a.caution.length > 0 && (
                    <div className="an-sec">
                      <div className="an-h">Before you apply</div>
                      {a.caution.map((l, i) => (
                        <div key={i} className="concern">
                          {l}
                        </div>
                      ))}
                    </div>
                  )}
                </>
              )}
              {hasNewSections && hasExpandable && (
                <button
                  type="button"
                  className="ghost tiny"
                  style={{ alignSelf: "flex-start" }}
                  onClick={() => setExpanded((v) => !v)}
                >
                  {expanded ? "Show less" : "Show more"}
                </button>
              )}
            </div>
          )}
        </>
      )}

      {(actions || role.url) && (
        <div className={`card-actions${indentActions ? "" : " flush"}`}>
          {role.url && (
            <a
              className={`btn btn-secondary${showRank ? "" : " sm"}`}
              href={role.url}
              target="_blank"
              rel="noreferrer"
            >
              ↗ View role
            </a>
          )}
          {actions}
        </div>
      )}
    </div>
  );
}
