"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { InRunFeedbackPrompt } from "@/components/InRunFeedbackPrompt";
import { IntentEditor } from "@/components/IntentEditor";
import { PreferencesPanel } from "@/components/PreferencesPanel";
import { RequirementRows } from "@/components/RequirementRows";
import { RoleFamilyCard } from "@/components/RoleFamilyCard";
import { api } from "@/lib/api";
import { useAttributes, useFamilies } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

export default function OnboardingPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { activeId } = useProfiles();
  const { data: attrs } = useAttributes(activeId);
  const { data: families } = useFamilies(activeId);
  const fileRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  // "Did setup work how it should?" — armed the moment a CV/notes parse
  // finishes, and shown at the bottom of the page beside the run button, which
  // is where the user is looking next. Asked once ever (the server scopes
  // `setup_ok` to the user, not to a run), so re-parsing does not re-ask
  // someone who already answered.
  const [setupPrompt, setSetupPrompt] = useState(false);

  if (!activeId) {
    return <div className="app narrow"><div className="center-pad">Loading…</div></div>;
  }

  const g = attrs?.by_type;
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["attributes", activeId] });
    // A parse also seeds role-family cards (backend now does this synchronously
    // -- see onboarding.py's parse_cv/parse_text), so a families query left
    // mounted from an earlier /dashboard visit needs refreshing too.
    qc.invalidateQueries({ queryKey: ["families", activeId] });
    // A parse can autofill Profile-table fields too (cv_summary, and intent_text
    // when it was empty -- see profile_intel._apply), which IntentEditor reads
    // from the ["profiles"] cache. Without this, a first-time CV upload's
    // drafted intent text silently doesn't appear until something else
    // invalidates that cache (e.g. navigating away and back).
    qc.invalidateQueries({ queryKey: ["profiles"] });
  };

  // Only after a parse actually SUCCEEDS: asking "did setup work?" when the
  // parse just errored is asking a question the screen has already answered,
  // and would collect a "no" that says nothing beyond the error the user can
  // see. Best-effort and silent on failure — this must never look like the
  // parse itself went wrong.
  const maybeArmSetupPrompt = () => {
    if (!activeId || setupPrompt) return;
    api
      .feedbackDue(activeId)
      .then((due) => setSetupPrompt(due.setup_ok.due))
      .catch(() => {
        /* non-blocking by design */
      });
  };

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setStatus("Reading your CV — the AI reads it closely, so this can take 15-20 seconds…");
    try {
      const created = await api.parseCv(activeId!, file);
      invalidate();
      setStatus(`Added ${created.length} items from your CV.`);
      maybeArmSetupPrompt();
    } catch (err) {
      setStatus((err as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function parseText() {
    if (!text.trim()) return;
    setBusy(true);
    setStatus("Reading your notes — the AI reads them closely, so this can take 15-20 seconds…");
    try {
      const created = await api.parseText(activeId!, text);
      invalidate();
      setText("");
      setStatus(`Added ${created.length} items from your text.`);
      maybeArmSetupPrompt();
    } catch (err) {
      setStatus((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function runFirstSearch() {
    setBusy(true);
    try {
      await api.startSearch(activeId!);
      qc.invalidateQueries({ queryKey: ["searchStatus", activeId] });
      router.push("/search");
    } catch (e) {
      alert((e as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="app narrow">
      <div className="screen-header">
        <div className="logo">Four in a Thousand</div>
        <div className="step-indicator">Build your profile</div>
      </div>

      <div className="page-body">
        <div>
          <div className="page-title" style={{ paddingBottom: 0 }}>
            Let&apos;s build your profile
          </div>
          <div className="page-sub">
            Upload your CV, paste your experience, or fill in manually below.
          </div>
        </div>

        {/* Step 1 — CV / text */}
        <div className="step-block">
          <div className="step-heading">Step 1 — CV upload or text entry</div>
          <div className="upload-zone">
            <div className="upload-top">
              <div className="upload-icon">📄</div>
              <div className="upload-text">
                <div className="label">Drop your CV here</div>
                <div className="sub">PDF, DOCX or TXT</div>
              </div>
              <div className="upload-btn-row">
                <button
                  className="btn btn-primary"
                  onClick={() => fileRef.current?.click()}
                  disabled={busy}
                >
                  {busy ? (
                    <>
                      <span className="spinner">◴</span> Working…
                    </>
                  ) : (
                    "Choose file"
                  )}
                </button>
                <input
                  ref={fileRef}
                  type="file"
                  accept=".pdf,.docx,.txt"
                  hidden
                  onChange={onUpload}
                />
              </div>
            </div>
            {(busy || status) && (
              <div className="upload-status">
                {busy && <span className="spinner">◴</span>} {busy ? status || "Working…" : status}
              </div>
            )}
            <div className="honesty-note">
              <strong>Works best if filled in honestly</strong> — AI tends to overestimate
              CV claims.
            </div>
            <div className="or-divider">or paste / type your experience directly</div>
            <textarea
              className="textarea-input"
              placeholder="e.g. 8 years as a software engineer, led a team of 8, built a payments platform at a FinTech startup. Strong in Python and React. Looking to move into a tech lead role…"
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="upload-btn-row">
              <button className="btn btn-secondary" onClick={parseText} disabled={busy || !text.trim()}>
                {busy ? "Parsing…" : "Parse text"}
              </button>
            </div>
          </div>
        </div>

        {/* Step 2 — review + complete. Deliberately the same three blocks, in the
            same order, as /dashboard's profile editor (role families → extra
            requirements → preferences): onboarding used to show a different set
            of controls in a different layout, so the profile a user built here
            didn't look like the profile they came back to edit. Past roles and
            qualifications are no longer shown at all -- they're background, not
            things the search targets, and having them first invited people to
            fill in a CV they had already uploaded. Both attribute types still
            exist and still feed the engine; they're just edited on /memory. */}
        <div className="step-block">
          <div className="step-heading">Step 2 — Review and complete your profile</div>
          <div className="profile-box">
            <div className="profile-header">Your profile</div>
            <div className="panel-b">
              <div className="profile-section-label">Role types</div>
              <div className="fam-list">
                {(families ?? []).map((f) => (
                  <RoleFamilyCard
                    key={f.id}
                    profileId={activeId}
                    family={f}
                    roles={(g?.target_role ?? []).filter((r) => r.family_id === f.id)}
                  />
                ))}
                {families?.length === 0 && (
                  <div className="info-banner">
                    {busy
                      ? "Reading your CV — your role families will appear here."
                      : "No role families yet — upload a CV or paste your experience above and they'll be built for you."}
                  </div>
                )}
              </div>
              <div className="annotation">
                Each family is searched as its own stream, so unrelated interests are judged on
                their own merits rather than blended together.
              </div>

              <div className="profile-section-label">Extra requirements</div>
              <RequirementRows
                profileId={activeId}
                mustHave={g?.must_have ?? []}
                avoid={g?.avoid ?? []}
              />

              {/* The identical control set /dashboard shows, from the same
                  component. Onboarding used to carry only three of these
                  (seniority, salary, location), so work style, maximum listing
                  age, junior-role tolerance and visa sponsorship were invisible
                  to a first-time user -- who then ran their first search with
                  all four left at their defaults without ever being told they
                  existed. See PreferencesPanel. */}
              <div className="profile-section-label">Preferences</div>
              <PreferencesPanel profileId={activeId} />

              <IntentEditor profileId={activeId} />
            </div>
          </div>
        </div>

        {/* Step 3 — launch */}
        <div className="step-block">
          <div className="step-heading">Step 3 — Run your first search</div>
          <div className="launch-box">
            <button
              className="btn btn-primary"
              style={{ padding: "10px 24px", fontSize: 13 }}
              onClick={runFirstSearch}
              disabled={busy}
            >
              ▶ Run first search
            </button>
          </div>
          {/* Asked here, next to the run button, because this is where the user
              is looking once their CV has been read — and because setup is
              precisely the thing they have just finished doing, so it is fresh.
              Sits below the button, not above it: nothing should stand between
              a first-time user and starting their first search. */}
          {setupPrompt && (
            <InRunFeedbackPrompt
              questionId="setup_ok"
              question="Did setting this up work how it should?"
              detailPlaceholder="What went wrong? e.g. 'my CV didn't upload', 'it got my job titles wrong', 'I couldn't tell what to do next'…"
              profileId={activeId}
              onDone={() => setSetupPrompt(false)}
            />
          )}
        </div>
      </div>
    </div>
  );
}
