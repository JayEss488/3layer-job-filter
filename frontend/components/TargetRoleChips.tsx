"use client";

import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute } from "@/lib/types";
import { Chip } from "./Chip";

/**
 * Target roles are generated (and regenerated) entirely by the backend's
 * profile_intel step now -- no manual add/suggest UI. `confirmed` doubles as
 * "pinned": a pinned role survives regeneration, an unpinned one may be
 * replaced. Delete still removes a role outright (it just isn't guaranteed to
 * stay gone -- an unpinned title can resurface on the next regeneration).
 */
export function TargetRoleChips({
  profileId,
  attributes,
}: {
  profileId: number;
  attributes: Attribute[];
}) {
  const { update, remove } = useAttributeMutations(profileId);

  return (
    <div className="row">
      <div className="label">Target roles</div>
      <div className="field">
        {attributes.length === 0 && (
          <span style={{ fontSize: 12.5, color: "var(--muted)" }}>
            None yet — generated automatically once you add a CV or describe what you&apos;re
            looking for above.
          </span>
        )}
        {attributes.map((a) => (
          <Chip
            key={a.id}
            label={a.value}
            pinned={a.confirmed}
            onTogglePin={() => update.mutate({ id: a.id, confirmed: !a.confirmed })}
            onRemove={() => remove.mutate(a.id)}
          />
        ))}
      </div>
    </div>
  );
}
