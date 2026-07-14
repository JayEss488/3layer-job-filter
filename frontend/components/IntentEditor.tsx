"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";

/**
 * Free-text "what I'm looking for" box. Persists to profile.intent_text (fed to the
 * final job-match judge as authoritative context) and drives "Regenerate target
 * roles", which rewrites the editable target-role chips from the CV + this text into
 * clean, board-standard titles the search is actually built from.
 */
export function IntentEditor({ profileId }: { profileId: number }) {
  const qc = useQueryClient();
  const { profiles } = useProfiles();
  const profile = profiles.find((p) => p.id === profileId);

  const [text, setText] = useState(profile?.intent_text ?? "");
  const [savedText, setSavedText] = useState(profile?.intent_text ?? "");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  // Re-sync when the active profile changes or its stored value loads/updates.
  useEffect(() => {
    setText(profile?.intent_text ?? "");
    setSavedText(profile?.intent_text ?? "");
  }, [profile?.id, profile?.intent_text]);

  const dirty = text.trim() !== (savedText ?? "").trim();

  async function save() {
    if (!dirty) return;
    setBusy(true);
    try {
      await api.updateProfile(profileId, { intent_text: text });
      await qc.invalidateQueries({ queryKey: ["profiles"] });
      setSavedText(text);
      setStatus("Saved.");
    } catch (e) {
      setStatus((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function regenerate() {
    setBusy(true);
    setStatus("Rebuilding target roles from what you wrote…");
    try {
      if (dirty) await api.updateProfile(profileId, { intent_text: text });
      const created = await api.regenerateTargetRoles(profileId);
      await qc.invalidateQueries({ queryKey: ["profiles"] });
      await qc.invalidateQueries({ queryKey: ["attributes", profileId] });
      await qc.invalidateQueries({ queryKey: ["confidence", profileId] });
      setSavedText(text);
      setStatus(
        created.length
          ? `Suggested ${created.length} target role${created.length === 1 ? "" : "s"} — review and edit below.`
          : "No new roles to add (your confirmed roles already cover it).",
      );
    } catch (e) {
      setStatus((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="row" style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
      <div className="label" style={{ width: "auto" }}>What you&apos;re actually looking for</div>
      <textarea
        className="textarea-input"
        placeholder="In your own words: what kind of roles, sectors, and level are you after? e.g. 'Entry-level policy or research roles at climate / environment charities and think tanks — I care about the mission more than the exact job title, and I'd also consider data / insight work.'"
        value={text}
        onChange={(e) => setText(e.target.value)}
        onBlur={save}
      />
      <div className="upload-btn-row" style={{ alignItems: "center", gap: 10 }}>
        <button className="btn btn-secondary" onClick={regenerate} disabled={busy || !text.trim()}>
          {busy ? "Working…" : "↻ Regenerate target roles from this"}
        </button>
        <span style={{ fontSize: 12, opacity: 0.7 }}>
          {dirty && !busy ? "Unsaved — click away or regenerate to save." : status}
        </span>
      </div>
    </div>
  );
}
