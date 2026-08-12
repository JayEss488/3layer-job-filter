"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { InRunFeedbackPrompt } from "@/components/InRunFeedbackPrompt";
import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";

/**
 * One combined feedback box on /search. Used to be two boxes stacked on top
 * of each other -- a search_feedback textarea ("too senior", "stop showing
 * sales roles", read by the final judge next run -- see
 * snapshot.build_snapshot) directly above a near-identical-looking "bug or
 * idea for the app" textarea (POST /profiles/{id}/comment, team-visible only
 * via GET /admin/analytics) -- and testers couldn't tell the two apart, so
 * one of them routinely went unread.
 *
 * Now there's one textarea and one save action that does both jobs
 * unconditionally: persists to profile.search_feedback (+ best-effort
 * interpretFeedback, turning an actionable line into a reviewable avoid/
 * must-have filter, same as before) AND posts the same text to the team via
 * submitComment. Whichever kind of thing the tester actually typed -- a
 * preference note or a bug report -- now reaches both audiences; the search
 * pipeline simply finds nothing actionable in a bug report, the same way it
 * already tolerated any other feedback with no clear rule in it.
 *
 * Distinct from IntentEditor's intent_text (what the candidate WANTS, which
 * drives target-role generation) -- this is reactive feedback on what was
 * shown, not a statement of intent.
 *
 * The "Are results what they should be?" in-run prompt (`resultsPrompt`,
 * fired by the first cross or apply on a run) still swaps in for the WHOLE
 * box while due, rather than stacking beside it -- two feedback asks at once
 * is how both get ignored. The textarea comes back once the prompt is
 * answered or dismissed.
 */
export function SearchFeedbackBox({
  profileId,
  resultsPrompt,
  onResultsPromptDone,
}: {
  profileId: number;
  /** The run the prompt is about, or null when it isn't due. */
  resultsPrompt?: { runId: number | null } | null;
  onResultsPromptDone?: () => void;
}) {
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
    const v = text.trim();
    try {
      await api.updateProfile(profileId, { search_feedback: text });
      await qc.invalidateQueries({ queryKey: ["profiles"] });
      setSavedText(text);
      setStatus("Saved — will be taken into account next search, and shared with the team.");
    } catch (e) {
      setStatus((e as Error).message);
      return;
    }
    // Team-visible copy, best-effort and silent: the text is already safely
    // saved above either way, so a failure here shouldn't look like a save error.
    if (v) api.submitComment(profileId, v).catch(() => {});
    // Turn an actionable line into a reviewable avoid/must-have filter, also
    // best-effort for the same reason.
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

  if (resultsPrompt) {
    return (
      <div className="feedback-box">
        <InRunFeedbackPrompt
          questionId="results_quality"
          question="Are these results what they should be?"
          detailPlaceholder="What went wrong? e.g. 'wrong kind of role', 'all too senior', 'only 2 results'…"
          profileId={profileId}
          runId={resultsPrompt.runId}
          onDone={() => onResultsPromptDone?.()}
        />
      </div>
    );
  }

  return (
    <div className="feedback-box">
      <div className="row" style={{ flexDirection: "column", alignItems: "stretch", gap: 6 }}>
        <div className="label" style={{ width: "auto" }}>
          Feedback — on these results, or on the app itself
        </div>
        <textarea
          className="textarea-input"
          style={{ minHeight: 48 }}
          placeholder="e.g. 'these are too senior for me', 'stop showing sales roles', or a bug/idea for the app — shapes your next search and goes to the team"
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
