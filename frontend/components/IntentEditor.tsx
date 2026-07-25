"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";

/**
 * Free-text "anything else" box. Persists to profile.intent_text (fed to the
 * final job-match judge as authoritative context — snapshot.py ranks it above
 * every other statement of what the candidate wants) and drives "Regenerate
 * target roles", which rewrites the editable target-role chips from the CV +
 * this text into clean, board-standard titles the search is actually built from.
 *
 * Deliberately framed as OPTIONAL extra information rather than a field to
 * complete. It used to arrive pre-filled with an AI-drafted paraphrase of the
 * CV, which read as the candidate's own words to every downstream stage while
 * being a guess — and on a short CV the guess had almost nothing to go on and
 * measurably widened the search (see profile_intel.generate_families). The
 * backend no longer drafts one in that case, so an empty box here is the normal
 * state, not an unfinished one.
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
      // The backend now (re-)groups the fresh roles into existing families as
      // part of this call -- refresh family cards too so they don't keep
      // showing stale membership until an unrelated page load happens to
      // refetch them.
      await qc.invalidateQueries({ queryKey: ["families", profileId] });
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
      <div className="label" style={{ width: "auto" }}>
        Anything else we should know <span style={{ opacity: 0.6 }}>(optional)</span>
      </div>
      <div className="annotation" style={{ padding: 0 }}>
        Only if there&apos;s something your CV and the settings above don&apos;t already say. Leave
        it blank and nothing is assumed on your behalf — anything you do write here is treated
        as your own words and outweighs everything the AI inferred.
      </div>
      <textarea
        className="textarea-input"
        placeholder="e.g. 'I care about the mission more than the exact job title — climate / environment charities and think tanks especially. I'd also consider data / insight work, but not anything client-facing.'"
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
