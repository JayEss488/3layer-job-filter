"use client";

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";

/**
 * One in-product feedback prompt: a Y/N question, and a "what went wrong?" box
 * that only appears on No.
 *
 * Shared by both prompts (`results_quality` on /search, `setup_ok` on
 * /onboarding) because they are the same interaction with different words —
 * see backend routers/feedback.py for the trigger rules, which live server-side
 * so a user who answered on one device isn't asked again on another.
 *
 * Yes submits immediately and collapses: the whole point is that saying "it's
 * fine" costs one click. No opens the box, because a No with no detail tells us
 * something is wrong and nothing about what.
 *
 * The detail box is OPTIONAL even after No. The No itself is already recorded
 * by then (two separate rows, `<id>` and `<id>_detail`), so someone who can't
 * be bothered to type still leaves a usable signal rather than an abandoned
 * prompt that records nothing.
 */
export function InRunFeedbackPrompt({
  questionId,
  question,
  detailPlaceholder,
  profileId,
  runId,
  onDone,
  compact = false,
}: {
  questionId: "results_quality" | "setup_ok";
  question: string;
  detailPlaceholder: string;
  profileId: number;
  runId?: number | null;
  /** Called once the prompt is finished with, so the parent can stop rendering it. */
  onDone: () => void;
  /** Tighter layout for sitting inline next to a button rather than in a box. */
  compact?: boolean;
}) {
  const [answer, setAnswer] = useState<"yes" | "no" | null>(null);
  const [detail, setDetail] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const doneTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Show the acknowledgement, THEN hand back to the parent (which unmounts us
  // and restores whatever this prompt was standing in for). Calling onDone
  // straight away removes the component in the same tick, so the prompt just
  // vanishes and the user gets no confirmation their answer was taken.
  function finish() {
    setDone(true);
    doneTimer.current = setTimeout(onDone, 2200);
  }

  useEffect(() => () => {
    if (doneTimer.current) clearTimeout(doneTimer.current);
  }, []);

  async function record(question_id: string, value: string) {
    await api.submitFeedback({
      question_id,
      answer: value,
      profile_id: profileId,
      run_id: runId ?? null,
    });
  }

  async function answerYesNo(value: "yes" | "no") {
    if (busy) return;
    setAnswer(value);
    setBusy(true);
    try {
      await record(questionId, value);
    } catch {
      /* Best-effort: a failed prompt answer must never interrupt what the user
         was actually doing. The UI proceeds either way. */
    } finally {
      setBusy(false);
    }
    // A Yes is the whole answer; hold the No open for the detail box.
    if (value === "yes") finish();
  }

  async function sendDetail() {
    if (busy) return;
    setBusy(true);
    try {
      if (detail.trim()) await record(`${questionId}_detail`, detail);
    } catch {
      /* see above */
    } finally {
      setBusy(false);
      finish();
    }
  }

  if (done) {
    return (
      <div className={compact ? "prompt-inline" : "annotation"}>
        <span>Thanks — noted.</span>
      </div>
    );
  }

  return (
    <div className={compact ? "prompt-inline" : "annotation"}>
      <div className="prompt-row">
        <span className="prompt-q">{question}</span>
        {answer === null && (
          <>
            <button
              className="btn btn-secondary sm"
              type="button"
              onClick={() => answerYesNo("yes")}
              disabled={busy}
            >
              Yes
            </button>
            <button
              className="btn btn-secondary sm"
              type="button"
              onClick={() => answerYesNo("no")}
              disabled={busy}
            >
              No
            </button>
            {/* Dismissible: a prompt that cannot be silenced stops being a
                prompt and becomes furniture. Records nothing, so it will be
                offered again next time it is due. */}
            <button
              className="btn btn-ghost sm"
              type="button"
              style={{ marginLeft: "auto" }}
              onClick={onDone}
            >
              Not now
            </button>
          </>
        )}
      </div>

      {answer === "no" && (
        <div className="prompt-detail">
          <textarea
            className="textarea-input"
            style={{ minHeight: 40 }}
            placeholder={detailPlaceholder}
            value={detail}
            onChange={(e) => setDetail(e.target.value)}
            autoFocus
          />
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button
              className="btn btn-secondary sm"
              type="button"
              onClick={sendDetail}
              disabled={busy}
            >
              {busy ? "Sending…" : "Send"}
            </button>
            <button className="btn btn-ghost sm" type="button" onClick={sendDetail}>
              Skip
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
