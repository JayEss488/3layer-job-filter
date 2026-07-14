"use client";

import { useState } from "react";

import { api } from "@/lib/api";
import { useAttributeMutations } from "@/lib/hooks";
import type { Attribute, AttributeType } from "@/lib/types";
import { Chip, SuggestChip } from "./Chip";

const PROFICIENCY_CHOICES = ["Expert", "Proficient", "Familiar", "One-time"];

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
  const { add, remove, update } = useAttributeMutations(profileId);
  const [adding, setAdding] = useState(false);
  const [draft, setDraft] = useState("");
  const [suggestions, setSuggestions] = useState<string[]>([]);
  const [loadingSuggest, setLoadingSuggest] = useState(false);

  const existing = new Set(attributes.map((a) => a.value.toLowerCase()));
  const showProficiency = type === "skill";
  const showInformalToggle = type === "past_role";

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
    <div className="row">
      <div className="label">{label}</div>
      <div className="field col">
        <div className="field">
          {attributes.map((a) => (
            <span key={a.id} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
              <Chip label={a.value} onRemove={() => remove.mutate(a.id)} />
              {showProficiency && (
                <select
                  className="input"
                  style={{ fontSize: 11, padding: "3px 5px" }}
                  value={a.proficiency ?? ""}
                  onChange={(e) => update.mutate({ id: a.id, proficiency: e.target.value })}
                  title="How deep is this experience?"
                >
                  <option value="">— proficiency —</option>
                  {PROFICIENCY_CHOICES.map((p) => (
                    <option key={p} value={p}>{p}</option>
                  ))}
                </select>
              )}
              {showInformalToggle && (
                a.proficiency === "Informal" ? (
                  <button
                    className="tag-informal"
                    onClick={() => update.mutate({ id: a.id, proficiency: "" })}
                    title="Student club, society, or volunteer role — not paid employment. Click to unmark."
                  >
                    Informal ✕
                  </button>
                ) : (
                  <button
                    className="ghost tiny"
                    onClick={() => update.mutate({ id: a.id, proficiency: "Informal" })}
                    title="Mark as informal (student club, society, or volunteer — not paid employment)"
                  >
                    + informal
                  </button>
                )
              )}
            </span>
          ))}

          {adding ? (
            <input
              autoFocus
              className="input"
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
            <button className="ghost" onClick={() => setAdding(true)}>
              ＋ add
            </button>
          )}

          {enableSuggest && (
            <button
              className="ghost suggest"
              onClick={fetchSuggestions}
              disabled={loadingSuggest}
              style={loadingSuggest ? { opacity: 0.5 } : undefined}
            >
              ✦ suggest
            </button>
          )}
        </div>

        {suggestions.length > 0 && (
          <div style={{ marginTop: 6 }}>
            <div className="suggest-label">AI suggestions — tap to add</div>
            <div className="field" style={{ marginTop: 3 }}>
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
