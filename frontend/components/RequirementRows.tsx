"use client";

import { useEffect, useState } from "react";

import { useAttributeMutations } from "@/lib/hooks";
import { enforcementOf } from "@/lib/types";
import type { Attribute, Enforcement } from "@/lib/types";
import { HardSoftToggle } from "./HardSoftToggle";

/**
 * The candidate's own requirement chips, as editable rows: Need/Avoid picks the
 * direction (must_have vs avoid — two attribute types, so switching direction
 * re-creates the row), Hard/Soft picks how literally the engine applies it.
 *
 * Both axes are load-bearing. Need+Hard is the only combination that will drop
 * a role for failing to offer something; Avoid+Hard is the only one that drops
 * for involving it. Everything else is a preference the AI weighs.
 */
function RequirementRow({
  profileId,
  attr,
}: {
  profileId: number;
  attr: Attribute;
}) {
  const { update, remove, add } = useAttributeMutations(profileId);
  const [text, setText] = useState(attr.value);
  useEffect(() => setText(attr.value), [attr.value]);

  const mode = attr.type as "must_have" | "avoid";
  const enforcement = enforcementOf(attr);

  function commitText() {
    const v = text.trim();
    if (!v) {
      setText(attr.value); // the API rejects empty; don't send a guaranteed 422
      return;
    }
    if (v !== attr.value) update.mutate({ id: attr.id, value: v });
  }

  function setMode(next: "must_have" | "avoid") {
    if (next === mode) return;
    // must_have and avoid are separate attribute types, so flipping direction is
    // a delete + re-add rather than a field edit. Enforcement is carried over
    // explicitly (rather than left to the new type's default) so switching
    // Need->Avoid doesn't silently re-harden a chip the user marked Soft.
    add.mutate({ type: next, value: attr.value, enforcement });
    remove.mutate(attr.id);
  }

  return (
    <div className="req-row">
      <div className="choice-row">
        {(
          [
            ["must_have", "Need"],
            ["avoid", "Avoid"],
          ] as const
        ).map(([m, label]) => (
          <button
            key={m}
            className={`toggle sm ${mode === m ? "on" : "off"}`}
            onClick={() => setMode(m)}
            title={
              m === "must_have"
                ? "Something a role must offer you."
                : "Something you don't want in a role."
            }
          >
            {label}
          </button>
        ))}
      </div>
      <input
        className="input req-text"
        value={text}
        placeholder="Describe the requirement…"
        onChange={(e) => setText(e.target.value)}
        onBlur={commitText}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.currentTarget.blur();
          if (e.key === "Escape") setText(attr.value);
        }}
        aria-label="Requirement"
      />
      <HardSoftToggle
        value={enforcement}
        onChange={(v: Enforcement) => update.mutate({ id: attr.id, enforcement: v })}
      />
      <button
        className="fam-x"
        onClick={() => remove.mutate(attr.id)}
        aria-label={`remove ${attr.value}`}
        title="Remove this requirement"
      >
        ✕
      </button>
    </div>
  );
}

export function RequirementRows({
  profileId,
  mustHave,
  avoid,
}: {
  profileId: number;
  mustHave: Attribute[];
  avoid: Attribute[];
}) {
  const { add } = useAttributeMutations(profileId);
  // Interleaved by id so a row keeps its place when its Hard/Soft changes, and
  // a newly added one lands at the bottom where the user is looking.
  const rows = [...mustHave, ...avoid].sort((a, b) => a.id - b.id);

  return (
    <>
      <div className="req-list">
        {rows.map((a) => (
          <RequirementRow key={a.id} profileId={profileId} attr={a} />
        ))}
        {rows.length === 0 && (
          <div className="muted-text">
            Nothing yet — add anything a role must offer you, or must not involve.
          </div>
        )}
      </div>
      <button
        className="ghost"
        onClick={() => add.mutate({ type: "must_have", value: "New requirement" })}
      >
        ＋ add requirement
      </button>
    </>
  );
}
