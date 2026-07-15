"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import { AttributeRow } from "@/components/AttributeRow";
import { ConfidenceBar } from "@/components/ConfidenceBar";
import { Nav } from "@/components/Nav";
import { ProfileTabs } from "@/components/ProfileTabs";
import { SalarySlider } from "@/components/SalarySlider";
import { api } from "@/lib/api";
import { useAttributes, useConfidence, useContextHeader } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

/**
 * Memory = who the candidate IS: the background the AI judges them on. What
 * they WANT (role families, requirements) lives on /dashboard.
 *
 * The split isn't just tidiness — it's the same one the engine makes. This
 * page's rows feed the CV text the final judge reads and the evidence tiers the
 * cheap screen weighs; the Profile page's rows decide what gets searched for in
 * the first place. Showing the AI's own context header at the bottom closes the
 * loop: it's generated from exactly these rows, so it's how you check an edit
 * actually landed.
 *
 * Salary and "extra preferences" (the custom attribute type) are the one
 * deliberate exception to that split: both count toward the confidence meter
 * shown on THIS page, so both need an editor here too, not only on /dashboard
 * — otherwise the meter's "add salary" tip is a dead end. Salary is still also
 * editable (with its Hard/Soft toggle) on the Profile tab; this is the same
 * attribute row, not a second copy.
 */
export default function MemoryPage() {
  const qc = useQueryClient();
  const { activeId } = useProfiles();
  const { data: attrs } = useAttributes(activeId);
  const { data: ctx } = useContextHeader(activeId);
  const { data: conf } = useConfidence(activeId);
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);

  if (!activeId) {
    return (
      <div className="app">
        <Nav />
        <div className="center-pad">Loading profile…</div>
      </div>
    );
  }

  const g = attrs?.by_type;

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      await api.parseCv(activeId!, file);
      qc.invalidateQueries({ queryKey: ["attributes", activeId] });
      qc.invalidateQueries({ queryKey: ["families", activeId] });
      qc.invalidateQueries({ queryKey: ["confidence", activeId] });
      qc.invalidateQueries({ queryKey: ["contextHeader", activeId] });
      // Profile-table fields (cv_summary, and intent_text when it was empty --
      // see profile_intel._apply) can change too; see onboarding/page.tsx's
      // invalidate() for the bug this avoids.
      qc.invalidateQueries({ queryKey: ["profiles"] });
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        <div>
          <div className="page-title">Memory</div>
          <div className="page-sub">
            What the AI knows about you. Your targets and filters live on the Profile tab.
          </div>
        </div>

        <ConfidenceBar confidence={conf} />

        <div className="panel">
          <div className="panel-h">Background</div>
          <div className="panel-b">
            <AttributeRow
              label="Past roles"
              profileId={activeId}
              type="past_role"
              attributes={g?.past_role ?? []}
              placeholder="e.g. Data Analyst at Acme"
            />
            <AttributeRow
              label="Qualifications"
              profileId={activeId}
              type="qualification"
              attributes={g?.qualification ?? []}
              placeholder="e.g. First Class Honours BSc Physics, Durham"
            />
            {/* Skills were deliberately un-rendered when the Profile page became
                "what the candidate wants" (see CLAUDE.md) — they kept feeding the
                engine invisibly because there was nowhere honest to put them. This
                page is that place: a skill and its evidence tier are squarely "who
                you are". sector_target stays hidden, deliberately: that's a want,
                not background — see snapshot._BASE_EMPHASIS for how it still feeds
                the engine. custom is surfaced below instead, since it counts toward
                confidence. */}
            <AttributeRow
              label="Skills"
              profileId={activeId}
              type="skill"
              attributes={g?.skill ?? []}
              enableSuggest
              placeholder="e.g. SQL"
            />
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">Preferences</div>
          <div className="panel-b">
            <div className="annotation" style={{ padding: "10px 0 4px" }}>
              These count toward the confidence score above — fill them in here so the tip
              isn&apos;t a dead end. Fine-tune enforcement (hard/soft) on the Profile tab.
            </div>
            <div className="row pref">
              <div className="label">Salary</div>
              <div className="field">
                <SalarySlider profileId={activeId} attribute={g?.salary?.[0]} />
              </div>
            </div>
            <AttributeRow
              label="Extra preferences"
              profileId={activeId}
              type="custom"
              attributes={g?.custom ?? []}
              placeholder="e.g. visa sponsorship required"
            />
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">What the AI reads about you</div>
          <div className="panel-b">
            <div className="annotation" style={{ padding: "10px 0 4px" }}>
              Generated from the background above — this is the summary given to the AI that
              judges each role. It refreshes when your memory changes, or when you run a
              search.
            </div>
            {ctx?.header ? (
              <div className="ctx-block">{ctx.header}</div>
            ) : (
              <div className="info-banner">
                Nothing generated yet — it&apos;s written the first time a search runs, or
                right after you upload a CV.
              </div>
            )}
            {(ctx?.requirements?.length ?? 0) > 0 && (
              <>
                <div className="subhead">Requirements the AI screens on</div>
                <ul className="ctx-list">
                  {ctx!.requirements.map((r, i) => (
                    <li key={i}>{r}</li>
                  ))}
                </ul>
              </>
            )}
            {ctx?.cv_summary && (
              <>
                <div className="subhead">CV summary</div>
                <div className="ctx-block">{ctx.cv_summary}</div>
              </>
            )}
          </div>
        </div>

        <div className="action-row">
          <button
            className="btn btn-secondary"
            onClick={() => fileRef.current?.click()}
            disabled={busy}
          >
            {busy ? "Reading CV…" : "↑ Upload new CV"}
          </button>
          <input ref={fileRef} type="file" accept=".pdf,.docx,.txt" hidden onChange={onUpload} />
        </div>

        <ProfileTabs />
      </div>
    </div>
  );
}
