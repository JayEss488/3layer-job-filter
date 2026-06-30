"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute, AttributeType } from "@/lib/types";
import { Chip, SuggestChip } from "./Chip";

interface Props {
  label: string;
  profileId: number;
  type: AttributeType;
  attributes: Attribute[];
  /** Show the dashed "AI suggestions" affordance (target_role, skill). */
  enableSuggest?: boolean;
  placeholder?: string;
}

export function AttributeRow({
  label,
  profileId,
  type,
  attributes,
  enableSuggest = false,
  placeholder,
}: Props) {
  const { add, remove } = useAttributeMutations(profileId);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [loadingSuggest, setLoadingSuggest] = useState(false);

  const existing = new Set(attributes.map((a) => a.value.toLowerCase()));

  function commit(value: string) {
    const v = value.trim();
    if (!v || existing.has(v.toLowerCase())) {
      setDraft("");
      return;
    }
    add.mutate({ type, value: v });
    setDraft("");
    setSuggestions((s) => s.filter((x) => x.toLowerCase() !== v.toLowerCase()));
  }

  async function fetchSuggestions() {
    setLoadingSuggest(true);
    try {
      const ctx = attributes.map((a) => a.value).join(", ");
      const { suggestions } = await api.suggest(profileId, type, ctx);
      setSuggestions(suggestions.filter((s) => !existing.has(s.toLowerCase())));
    } catch {
      /* ignore */
    } finally {
      setLoadingSuggest(false);
    }
  }

  return (
    <div className="attr-row">
      <div className="attr-label">{label}</div>
      <div className="attr-values" style={{ flexDirection: "column", alignItems: "stretch" }}>
        <div className="attr-values">
          {attributes.map((a) => (
            <Chip key={a.id} label={a.value} onRemove={() => remove.mutate(a.id)} />
          ))}

          {adding ? (
            <input
              autoFocus
              className="inline-input"
              value={draft}
              placeholder={placeholder || `Add ${label.toLowerCase()}…`}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => {
                commit(draft);
                setAdding(false);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") commit(draft);
                if (e.key === "Escape") {
                  setDraft("");
                  setAdding(false);
                }
              }}
            />
          ) : (
            <button className="chip-add" onClick={() => setAdding(true)}>
              + add
            </button>
          )}

          {enableSuggest && (
            <button className="chip-add" onClick={fetchSuggestions} disabled={loadingSuggest}>
              {loadingSuggest ? "…" : "✨ suggest"}
            </button>
          )}
        </div>

        {suggestions.length > 0 && (
          <div style={{ marginTop: 6 }}>
            <div className="suggest-label">AI suggestions — tap to add</div>
            <div className="attr-values" style={{ marginTop: 3 }}>
              {suggestions.map((s) => (
                <SuggestChip key={s} label={s} onAdd={() => commit(s)} />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
