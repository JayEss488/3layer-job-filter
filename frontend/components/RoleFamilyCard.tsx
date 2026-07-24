"use client";

import { useEffect, useState } from "react";

import { useAttributeMutations, useFamilyMutations } from "@/lib/hooks";
import type { Attribute, RoleFamily } from "@/lib/types";

const REGENERATE_TITLE =
  "Regenerate suggested roles for this family from its name — pinned roles are kept.";

/**
 * One role family = one stream the search engine runs independently (scored,
 * gated, and judged on its own — see CLAUDE.md's search-pipeline section). The
 * card is therefore not just a grouping widget: what you put in it decides how
 * the engine splits its budget.
 *
 * Active/Inactive is a strict on/off switch, not a priority scale: an inactive
 * family gets no cluster at all — no discovery, no searching, no gate/rank/
 * judge calls, nothing (see CLAUDE.md's search-pipeline section). The per-role
 * pin is a separate, milder signal that only matters within an active family:
 * it feeds an emphasis multiplier on the embedding pre-filter (backend
 * config.PINNED_ROLE_MULT) and sets `confirmed`, which protects a role from
 * being dropped when target roles regenerate — one control, one meaning
 * ("this one matters"), rather than two near-identical pins to tell apart.
 */
export function RoleFamilyCard({
  profileId,
  family,
  roles,
}: {
  profileId: number;
  family: RoleFamily;
  roles: Attribute[];
}) {
  const { add, update, remove } = useAttributeMutations(profileId);
  const fam = useFamilyMutations(profileId);
  const [name, setName] = useState(family.name);
  const [draft, setDraft] = useState("");

  // Keep the input honest when the family is renamed elsewhere (or a refetch
  // lands): local state would otherwise pin the stale name on screen.
  useEffect(() => setName(family.name), [family.name]);

  function commitName() {
    const v = name.trim();
    if (!v) {
      setName(family.name); // empty is rejected by the API; snap back rather than 422
      return;
    }
    if (v !== family.name) fam.update.mutate({ id: family.id, name: v });
  }

  function addRole() {
    const v = draft.trim();
    if (!v) return;
    if (roles.some((r) => r.value.toLowerCase() === v.toLowerCase())) {
      setDraft("");
      return;
    }
    add.mutate({ type: "target_role", value: v, family_id: family.id });
    setDraft("");
  }

  return (
    <div className="fam-card">
      <div className="fam-head">
        <input
          className="fam-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            if (e.key === "Escape") setName(family.name);
          }}
          aria-label="Role family name"
        />
        <div className="fam-head-right">
          <div className="choice-row">
            {(["active", "inactive"] as const).map((tier) => (
              <button
                key={tier}
                className={`toggle sm ${family.tier === tier ? "on" : "off"}`}
                onClick={() => fam.update.mutate({ id: family.id, tier })}
                title={
                  tier === "active"
                    ? "Searched every run — has its own discovery, gate, and AI review."
                    : "Not searched at all — the algorithm ignores this stream completely until you switch it back on."
                }
              >
                {tier === "active" ? "Active" : "Inactive"}
              </button>
            ))}
          </div>
          <button
            className="ghost tiny"
            title={REGENERATE_TITLE}
            aria-label={`Regenerate roles for ${family.name}`}
            disabled={fam.regenerate.isPending}
            onClick={() => fam.regenerate.mutate(family.id)}
          >
            {fam.regenerate.isPending ? "…" : "↻ Regenerate"}
          </button>
          <button
            className="fam-x"
            title="Remove this family and the roles in it"
            aria-label={`Remove ${family.name} family`}
            onClick={() => {
              if (
                confirm(
                  `Remove "${family.name}" and its ${roles.length} role${
                    roles.length === 1 ? "" : "s"
                  }? Searches will stop covering this stream.`
                )
              )
                fam.remove.mutate(family.id);
            }}
          >
            ✕
          </button>
        </div>
      </div>

      <div className="fam-sub">Such as</div>
      <div className="fam-roles">
        {roles.map((r) => (
          <span key={r.id} className={`chip${r.pinned ? " tinted" : ""}`}>
            <button
              className={`fam-pin${r.pinned ? " on" : ""}`}
              onClick={() =>
                update.mutate({ id: r.id, pinned: !r.pinned, confirmed: !r.pinned })
              }
              title={
                r.pinned
                  ? "A priority in this family — weighted up, and kept when roles regenerate. Click to unpin."
                  : "Pin as a priority in this family — weights it up and keeps it when roles regenerate."
              }
              aria-label={`${r.pinned ? "Unpin" : "Pin"} ${r.value}`}
            />
            {r.value}
            <span
              className="x"
              role="button"
              aria-label={`remove ${r.value}`}
              onClick={() => remove.mutate(r.id)}
            >
              ✕
            </span>
          </span>
        ))}
        <span className="fam-add">
          <input
            value={draft}
            placeholder="add role"
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") addRole();
              if (e.key === "Escape") setDraft("");
            }}
            aria-label={`Add a role to ${family.name}`}
          />
          <button onClick={addRole} aria-label="Add role">
            ＋
          </button>
        </span>
      </div>
    </div>
  );
}
