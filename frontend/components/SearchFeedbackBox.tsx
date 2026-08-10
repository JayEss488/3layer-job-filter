"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";

import { InRunFeedbackPrompt } from "@/components/InRunFeedbackPrompt";
import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";

/**
 * Free-text feedback on the results shown below ("too senior", "stop showing
 * sales roles"), distinct from IntentEditor's intent_text (what the candidate
 * WANTS, which drives target-role generation). Persists to
 * profile.search_feedback and is read by the final judge on the next run --
 * see snapshot.build_snapshot.
 *
 * Also carries a second, visually distinct box for general beta feedback
 * about the APP (bugs/confusing bits/ideas) -- posts to POST
 * /profiles/{id}/comment and is read back by the owner-only GET
 * /admin/analytics, not by the search pipeline.
 *
 * When the "Are results what they should be?" prompt is due (`resultsPrompt`,
 * fired by the first cross or apply on a run), it renders IN PLACE OF that
 * bottom box rather than alongside it -- two feedback asks stacked on top of
 * each other is how both get ignored. The bug box comes back the moment the
 * prompt is answered or dismissed.
 *
 * Both of those stay strictly separate from the top box: a beta tester's note
 * about a confusing button must never reach search_feedback, which is read by
 * the LLM judge as if it were a job-search preference.
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

  // Product feedback about the APP itself (bugs, confusing bits, feature
  // ideas) -- deliberately a separate field from search_feedback above, which
  // gets read by the final judge on the next run. Mixing the two would leak a
  // beta tester's comment about a confusing button into the LLM prompt as if
  // it were a job-search preference.
  const [comment, setComment] = useState("");
  const [commentStatus, setCommentStatus] = useState("");
  const [sending, setSending] = useState(false);

  async function sendComment() {
    const v = comment.trim();
    if (!v || sending) return;
    setSending(true);
    setCommentStatus("");
    try {
      await api.submitComment(profileId, v);
      setComment("");
      setCommentStatus("Thanks — the team will see this.");
    } catch (e) {
      setCommentStatus((e as Error).message);
    } finally {
      setSending(false);
    }
  }

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
    // Best-effort: turn actionable feedback into a reviewable avoid/must-have
    // filter. Kept out of the try/catch above -- the feedback text is already
    // safely saved either way, so a failure here shouldn't look like a save error.
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
          Feedback on these results
        </div>
        <textarea
          className="textarea-input"
          style={{ minHeight: 48 }}
          placeholder="e.g. 'these are too senior for me' or 'stop showing sales roles' — used on your next search"
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
      <div
        className="row"
        style={{
          flexDirection: "column",
          alignItems: "stretch",
          gap: 6,
          marginTop: 10,
          paddingTop: 10,
          borderTop: "1px solid var(--line)",
        }}
      >
        {resultsPrompt ? (
          <InRunFeedbackPrompt
            questionId="results_quality"
            question="Are these results what they should be?"
            detailPlaceholder="What went wrong? e.g. 'wrong kind of role', 'all too senior', 'only 2 results'…"
            profileId={profileId}
            runId={resultsPrompt.runId}
            onDone={() => onResultsPromptDone?.()}
          />
        ) : (
          <>
            <div className="label" style={{ width: "auto" }}>
              Got a bug or an idea for the app? Tell us — it goes straight to the team
            </div>
            <textarea
              className="textarea-input"
              style={{ minHeight: 40 }}
              placeholder="Anything about the app itself — bugs, confusing bits, features you'd want…"
              value={comment}
              onChange={(e) => setComment(e.target.value)}
            />
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <button
                className="btn btn-secondary sm"
                onClick={sendComment}
                disabled={!comment.trim() || sending}
              >
                {sending ? "Sending…" : "Send feedback"}
              </button>
              {commentStatus && (
                <span style={{ fontSize: 12, opacity: 0.7 }}>{commentStatus}</span>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
