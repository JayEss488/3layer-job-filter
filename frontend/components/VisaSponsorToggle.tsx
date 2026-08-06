"use client";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";

/**
 * Single-value boolean: at most one visa_sponsor_only row, whose presence with
 * value "true" means on. No row at all means off, which is the default -- see
 * backend/app/config.ATTRIBUTE_TYPES and snapshot._parse_visa_sponsor_only.
 *
 * Deliberately has no Hard/Soft control, unlike every other preference card:
 * there is no useful soft reading of "I need a visa". The note below the buttons
 * is the one place in the UI that explains what the filter cannot see, and it
 * should stay: the match is on company NAME against a register with no domains,
 * so a role advertised by a recruitment agency is dropped even when the end
 * employer does sponsor. A user who turns this on and sees their result count
 * collapse deserves to know that before concluding the app is broken.
 */
export function VisaSponsorToggle({
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
    if (next) add.mutate({ type: "visa_sponsor_only", value: "true" });
  }

  return (
    <div className="location-row">
      <div className="choice-row">
        <button className={`toggle ${on ? "on" : "off"}`} onClick={() => set(true)}>
          Sponsors only
        </button>
        <button className={`toggle ${!on ? "on" : "off"}`} onClick={() => set(false)}>
          Any employer
        </button>
      </div>
      {on && (
        <div className="hint-detected">
          Hard filter, matched on employer name against the Home Office register.
          Roles advertised by <strong>recruitment agencies</strong>, or with no
          employer named, can&rsquo;t be checked and are excluded &mdash; so expect
          far fewer results.
        </div>
      )}
    </div>
  );
}
