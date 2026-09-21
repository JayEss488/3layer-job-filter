"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";

/**
 * The "tune my next search" box on /search.
 *
 * What the user types here is saved to profile.search_feedback and read by the
 * final judge on the NEXT run (see snapshot.build_snapshot), so it is a control,
 * not a comment box -- "these are too senior for me" or "stop showing sales
 * roles" changes what comes back. A best-effort interpretFeedback pass also
 * turns an actionable line into a reviewable avoid/must-have chip.
 *
 * Distinct from IntentEditor's intent_text (what the candidate WANTS, which
 * drives target-role generation): this is reactive feedback on what was shown,
 * not a statement of intent.
 */
export function SearchFeedbackBox({ profileId }: { profileId: number }) {
  const qc = useQueryClient();
  const { profiles } = useProfiles();
  const profile = profiles.find((p) => p.id === profileId);

  const [text, setText] = useState(profile?.search_feedback ?? "");
  const [savedText, setSavedText] = useState(profile?.search_feedback ?? "");
  const [status, setStatus] = useState("");

  useEffect(() => {
    setText(profile?.search_feedback ?? "");
    setSavedText(profile?.search_feedback ?? "");
  }, [profile?.id, profile?.search_feedback]);

  const dirty = text.trim() !== (savedText ?? "").trim();

  async function save() {
    if (!dirty) return;
    try {
      await api.updateProfile(profileId, { search_feedback: text });
      await qc.invalidateQueries({ queryKey: ["profiles"] });
      setSavedText(text);
      setStatus("Saved — will be taken into account next search.");
    } catch (e) {
      setStatus((e as Error).message);
      return;
    }
    // Turn an actionable line into a reviewable avoid/must-have filter.
    // Best-effort: the feedback text itself is already safely saved above, so a
    // failure here must not look like a save error.
    try {
      const created = await api.interpretFeedback(profileId);
      if (created.length > 0) {
        await qc.invalidateQueries({ queryKey: ["attributes", profileId] });
        const names = created
          .map((a) => `${a.type === "avoid" ? "avoid" : "need"}: "${a.value}"`)
          .join(", ");
        setStatus(
          `Saved — added ${created.length} filter${created.length > 1 ? "s" : ""} (${names}) — review on your dashboard.`
        );
      }
    } catch {
      /* silent -- the feedback text itself already saved fine */
    }
  }

  return (
    <div className="feedback-box">
      <div className="row" style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
        <div className="label" style={{ width: "auto" }}>
          Tune your next search
        </div>
        <textarea
          className="textarea-input"
          style={{ minHeight: 48 }}
          placeholder="e.g. 'these are too senior for me', 'stop showing sales roles' — read by the final judge on your next run"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={save}
        />
        {(dirty || status) && (
          <span style={{ fontSize: 12, opacity: 0.7 }}>
            {dirty ? "Unsaved — click away to save." : status}
          </span>
        )}
      </div>
    </div>
  );
}
