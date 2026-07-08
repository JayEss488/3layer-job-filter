"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { AttributeRow } from "@/components/AttributeRow";
import { LocationPicker } from "@/components/LocationPicker";
import { Nav } from "@/components/Nav";
import { ProfileTabs } from "@/components/ProfileTabs";
import { SalarySlider } from "@/components/SalarySlider";
import { SeniorityPicker } from "@/components/SeniorityPicker";
import { api } from "@/lib/api";
import { useAttributes, useStats } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

export default function DashboardPage() {
  const router = useRouter();
  const qc = useQueryClient();
  const { activeId } = useProfiles();
  const { data: attrs } = useAttributes(activeId);
  const { data: stats } = useStats(activeId);
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

  async function runSearch() {
    setBusy(true);
    try {
      await api.startSearch(activeId!);
      qc.invalidateQueries({ queryKey: ["searchStatus", activeId] });
      router.push("/search");
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function onUpload(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setBusy(true);
    try {
      await api.parseCv(activeId!, file);
      qc.invalidateQueries({ queryKey: ["attributes", activeId] });
      qc.invalidateQueries({ queryKey: ["confidence", activeId] });
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function clearMemory() {
    if (!confirm("Reset all learned weights for this profile? Your attributes are kept."))
      return;
    await api.clearMemory(activeId!);
    qc.invalidateQueries({ queryKey: ["attributes", activeId] });
  }

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        <div className="page-title">Profile &amp; Dashboard</div>

        <div className="stats">
          <div className="stat">
            <div className={`n${(stats?.searched ?? 0) === 0 ? " zero" : ""}`}>{stats?.searched ?? 0}</div>
            <div className="l">Roles searched</div>
          </div>
          <div className="stat">
            <div className={`n${(stats?.saved ?? 0) === 0 ? " zero" : ""}`}>{stats?.saved ?? 0}</div>
            <div className="l">Saved</div>
          </div>
          <div className="stat">
            <div className={`n${(stats?.applied ?? 0) === 0 ? " zero" : ""}`}>{stats?.applied ?? 0}</div>
            <div className="l">Applied</div>
          </div>
        </div>

        <ProfileTabs />

        <div className="panel">
          <div className="panel-h">What you&apos;re looking for</div>
          <div className="panel-b">
            <AttributeRow
              label="Target roles"
              profileId={activeId}
              type="target_role"
              attributes={g?.target_role ?? []}
              enableSuggest
              placeholder="e.g. Engineering Manager"
            />
            <AttributeRow
              label="Past roles"
              profileId={activeId}
              type="past_role"
              attributes={g?.past_role ?? []}
            />
            <AttributeRow
              label="Skills"
              profileId={activeId}
              type="skill"
              attributes={g?.skill ?? []}
              enableSuggest
            />
            <AttributeRow
              label="Experience"
              profileId={activeId}
              type="experience"
              attributes={g?.experience ?? []}
            />
            <AttributeRow
              label="Qualifications"
              profileId={activeId}
              type="qualification"
              attributes={g?.qualification ?? []}
              placeholder="e.g. First Class Honours BSc Physics, Durham"
            />
            <AttributeRow
              label="Sector / mission interests"
              profileId={activeId}
              type="sector_target"
              attributes={g?.sector_target ?? []}
              placeholder="e.g. Clean energy / net-zero policy"
            />

            <div className="subhead">PREFERENCES</div>

            <div className="row pref">
              <div className="label">Seniority</div>
              <div className="field">
                <SeniorityPicker profileId={activeId} attributes={g?.seniority ?? []} />
              </div>
            </div>

            <div className="row pref">
              <div className="label">Salary</div>
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
                  scopeAttributes={g?.location_scope ?? []}
                />
              </div>
            </div>

            <AttributeRow
              label="Anything else"
              profileId={activeId}
              type="custom"
              attributes={g?.custom ?? []}
              placeholder="e.g. Only Series B+ startups"
            />
          </div>
        </div>

        <div className="bottom-row">
          <div className="action-row">
            <button className="btn btn-primary" onClick={runSearch} disabled={busy}>
              ▶ Run New Search
            </button>
            <button className="btn btn-secondary" onClick={() => fileRef.current?.click()} disabled={busy}>
              ↑ Upload new CV
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".pdf,.docx,.txt"
              hidden
              onChange={onUpload}
            />
          </div>
          <button className="btn btn-ghost" onClick={clearMemory}>
            Clear memory
          </button>
        </div>

        <div className="annotation">
          Ticking and crossing roles in Search adjusts your profile weights automatically.
        </div>
      </div>
    </div>
  );
}
