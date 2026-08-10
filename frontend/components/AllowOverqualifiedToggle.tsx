"use client";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

/**
 * Single-value boolean: at most one allow_overqualified row, whose presence with
 * value "true" means on. No row at all means off, which is the default -- see
 * backend/app/config.ATTRIBUTE_TYPES and snapshot._parse_allow_overqualified.
 *
 * Deliberately has no Hard/Soft control, like VisaSponsorToggle but for the
 * opposite reason: turning this on IS the softening. It converts the free
 * junior-title reject in engine._heuristic_prescreen from an unconditional drop
 * into something the cheap gate weighs, and tells all three AI tiers that a role
 * pitched below the candidate's level is not itself a mismatch. A "Hard" reading
 * of "I'll consider more junior roles" would be self-contradictory.
 *
 * Direction-aware: it only affects a candidate whose stated seniority is on the
 * senior side. A Graduate/Junior profile is unaffected, since being open to more
 * junior roles says nothing about Director postings.
 *
 * The note below the buttons should stay. It names the one thing this does NOT
 * do, and it is the exact thing a user would otherwise assume it does:
 * apprenticeships and student placements stay excluded, because they were never
 * excluded for being junior -- they are courses with eligibility bars against
 * people who already hold the qualification.
 */
export function AllowOverqualifiedToggle({
  profileId,
  attributes,
}: {
  profileId: number;
  attributes: Attribute[];
}) {
  const { add, remove } = useAttributeMutations(profileId);
  const on = attributes.some((a) => a.value.toLowerCase() === "true");

  function set(next: boolean) {
    if (next === on) return;
    attributes.forEach((a) => remove.mutate(a.id));
    if (next) add.mutate({ type: "allow_overqualified", value: "true" });
  }

  return (
    <div className="location-row">
      <div className="choice-row">
        <button className={`toggle ${on ? "on" : "off"}`} onClick={() => set(true)}>
          Show them
        </button>
        <button className={`toggle ${!on ? "on" : "off"}`} onClick={() => set(false)}>
          Hide them
        </button>
      </div>
      {on && (
        <div className="hint-detected">
          Roles pitched below your seniority become a <strong>soft</strong> filter &mdash;
          they can appear, ranked on how well they fit otherwise.{" "}
          <strong>Apprenticeships and placement years stay excluded</strong>: those
          are courses that usually bar applicants who already hold the qualification,
          so they&rsquo;re not a level question.
        </div>
      )}
    </div>
  );
}
