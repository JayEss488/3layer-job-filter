"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { AttributeRow } from "@/components/AttributeRow";
import { ConfidenceBar } from "@/components/ConfidenceBar";
import { LocationPicker } from "@/components/LocationPicker";
import { SalarySlider } from "@/components/SalarySlider";
import { SeniorityPicker } from "@/components/SeniorityPicker";
import { api } from "@/lib/api";
import { useAttributes, useConfidence } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

export default function OnboardingPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { activeId } = useProfiles();
  const { data: attrs } = useAttributes(activeId);
  const { data: confidence } = useConfidence(activeId);
  const fileRef = useRef<HTMLInputElement>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  if (!activeId) {
    return <div className="app narrow"><div className="center-pad">Loading…</div></div>;
  }

  const g = attrs?.by_type;
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["attributes", activeId] });
    qc.invalidateQueries({ queryKey: ["confidence", activeId] });
  };

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    setStatus("Reading your CV…");
    try {
      const created = await api.parseCv(activeId!, file);
      invalidate();
      setStatus(`Added ${created.length} items from your CV.`);
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
    setStatus("Reading your notes…");
    try {
      const created = await api.parseText(activeId!, text);
      invalidate();
      setText("");
      setStatus(`Added ${created.length} items from your text.`);
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
        <div className="logo">Omni Board</div>
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
                  Choose file
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
              {status && <span className="muted-text" style={{ alignSelf: "center" }}>{status}</span>}
            </div>
          </div>
        </div>

        {/* Step 2 — review + complete */}
        <div className="step-block">
          <div className="step-heading">Step 2 — Review and complete your profile</div>
          <div className="profile-box">
            <div className="profile-header">Your profile</div>
            <div className="panel-b">
              <div className="profile-section-label">
                Your background — what you have done
              </div>
              <AttributeRow label="Past roles" profileId={activeId} type="past_role" attributes={g?.past_role ?? []} />
              <AttributeRow label="Skills" profileId={activeId} type="skill" attributes={g?.skill ?? []} enableSuggest />
              <AttributeRow label="Experience" profileId={activeId} type="experience" attributes={g?.experience ?? []} />
              <div className="row pref">
                <div className="label">Seniority</div>
                <div className="field">
                  <SeniorityPicker profileId={activeId} attributes={g?.seniority ?? []} />
                </div>
              </div>

              <div className="profile-section-label">
                What you&apos;re looking for — your next role
              </div>
              <AttributeRow
                label="Target roles"
                profileId={activeId}
                type="target_role"
                attributes={g?.target_role ?? []}
                enableSuggest
                placeholder="e.g. Engineering Manager, Principal Engineer"
              />
              <div className="row pref">
                <div className="label">Salary range</div>
                <div className="field">
                  <SalarySlider profileId={activeId} attribute={g?.salary?.[0]} />
                </div>
              </div>
              <div className="row pref">
                <div className="label">Location</div>
                <div className="field">
                  <LocationPicker
                    profileId={activeId}
                    attributes={g?.location ?? []}
                    countryAttributes={g?.country ?? []}
                  />
                </div>
              </div>
              <AttributeRow
                label="Anything else"
                profileId={activeId}
                type="custom"
                attributes={g?.custom ?? []}
                placeholder="e.g. Only Series B+ startups, must offer visa sponsorship"
              />
            </div>
          </div>
        </div>

        {/* Step 3 — launch */}
        <div className="step-block">
          <div className="step-heading">Step 3 — Run your first search</div>
          <div className="launch-box">
            <ConfidenceBar confidence={confidence} />
            <button
              className="btn btn-primary"
              style={{ padding: "10px 24px", fontSize: 13 }}
              onClick={runFirstSearch}
              disabled={busy}
            >
              ▶ Run first search
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
