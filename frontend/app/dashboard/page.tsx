"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { HardSoftToggle } from "@/components/HardSoftToggle";
import { LocationPicker, WORK_SET } from "@/components/LocationPicker";
import { Nav } from "@/components/Nav";
import { ProfileTabs } from "@/components/ProfileTabs";
import { RequirementRows } from "@/components/RequirementRows";
import { RoleFamilyCard } from "@/components/RoleFamilyCard";
import { SalarySlider } from "@/components/SalarySlider";
import { SeniorityPicker } from "@/components/SeniorityPicker";
import { WorkStylePicker } from "@/components/WorkStylePicker";
import { api } from "@/lib/api";
import {
  useAttributeMutations,
  useAttributes,
  useFamilies,
  useFamilyMutations,
  useStats,
} from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";
import { enforcementOf } from "@/lib/types";
import type { Attribute, Enforcement } from "@/lib/types";

/**
 * Profile = what the candidate WANTS: the role families the engine searches as
 * separate streams, their own requirements, and their preferences. Who they ARE
 * (past roles, qualifications, skills, CV context) lives on /memory.
 */
export default function DashboardPage() {
  const { activeId } = useProfiles();

  if (!activeId) {
    return (
      <div className="app">
        <Nav />
        <div className="center-pad">Loading profile…</div>
      </div>
    );
  }
  // Keyed so switching profiles remounts rather than carrying this profile's
  // in-flight edit state (a half-typed family name, an open add-role input)
  // across to the next one.
  return <ProfileBody key={activeId} profileId={activeId} />;
}

// Mirrors backend config.MAX_USER_ROLE_FAMILIES -- the engine only ever
// clusters into 3 streams (MAX_ROLE_CLUSTERS), so this caps manual additions
// one above that: at most one family ever needs folding into another.
const MAX_ROLE_FAMILIES = 4;

// Split out so every hook below runs unconditionally: the page above returns
// early until a profile id exists, and useAttributeMutations needs a real one.
function ProfileBody({ profileId }: { profileId: number }) {
  const router = useRouter();
  const qc = useQueryClient();
  const { data: attrs } = useAttributes(profileId);
  const { data: families } = useFamilies(profileId);
  const { data: stats } = useStats(profileId);
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const { update } = useAttributeMutations(profileId);
  const fam = useFamilyMutations(profileId);
  const g = attrs?.by_type;

  const targetRoles = g?.target_role ?? [];
  const locationAttrs = g?.location ?? [];
  const cityAttr = locationAttrs.find((a) => !WORK_SET.has(a.value.toLowerCase()));
  const workTypeAttrs = locationAttrs.filter((a) => WORK_SET.has(a.value.toLowerCase()));
  const seniorityAttrs = g?.seniority ?? [];
  const salaryAttr = g?.salary?.[0];

  /**
   * Hard/Soft for a preference is stored per attribute row, but the UI shows one
   * toggle per preference — so a group with several rows (every selected
   * seniority level, every ticked work type) writes the same value to all of
   * them. Reading takes the first row's value: they're only ever set together.
   */
  function groupEnforcement(rows: Attribute[], fallback: Enforcement): Enforcement {
    return rows.length ? enforcementOf(rows[0]) : fallback;
  }
  function setGroupEnforcement(rows: Attribute[], v: Enforcement) {
    rows.forEach((a) => update.mutate({ id: a.id, enforcement: v }));
  }

  const workStyleEnforcement = groupEnforcement(workTypeAttrs, "soft");

  async function runSearch() {
    setBusy(true);
    try {
      await api.startSearch(profileId);
      qc.invalidateQueries({ queryKey: ["searchStatus", profileId] });
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
    setUploading(true);
    try {
      await api.parseCv(profileId, file);
      qc.invalidateQueries({ queryKey: ["attributes", profileId] });
      // A parse adds ungrouped target roles; refetching families is what seeds
      // them into cards (see routers/families.py's GET).
      qc.invalidateQueries({ queryKey: ["families", profileId] });
      qc.invalidateQueries({ queryKey: ["confidence", profileId] });
      // Profile-table fields (cv_summary, and intent_text when it was empty --
      // see profile_intel._apply) can change too; see onboarding/page.tsx's
      // invalidate() for the bug this avoids.
      qc.invalidateQueries({ queryKey: ["profiles"] });
    } catch (err) {
      alert((err as Error).message);
    } finally {
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function clearMemory() {
    if (!confirm("Reset all learned weights for this profile? Your attributes are kept."))
      return;
    await api.clearMemory(profileId);
    qc.invalidateQueries({ queryKey: ["attributes", profileId] });
  }

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        <div className="page-title">Profile &amp; Dashboard</div>

        <div className="stats">
          <div className="stat">
            <div className={`n${(stats?.searched ?? 0) === 0 ? " zero" : ""}`}>
              {stats?.searched ?? 0}
            </div>
            <div className="l">Roles searched</div>
          </div>
          <div className="stat">
            <div className={`n${(stats?.saved ?? 0) === 0 ? " zero" : ""}`}>
              {stats?.saved ?? 0}
            </div>
            <div className="l">Saved</div>
          </div>
          <div className="stat">
            <div className={`n${(stats?.applied ?? 0) === 0 ? " zero" : ""}`}>
              {stats?.applied ?? 0}
            </div>
            <div className="l">Applied</div>
          </div>
        </div>

        <ProfileTabs />

        {/* ── Role types ── */}
        <div className="section-head">
          <div className="subhead">Role types</div>
          <button
            className="ghost"
            disabled={(families?.length ?? 0) >= MAX_ROLE_FAMILIES}
            title={
              (families?.length ?? 0) >= MAX_ROLE_FAMILIES
                ? `Limit reached (${MAX_ROLE_FAMILIES} max)`
                : undefined
            }
            onClick={() => fam.add.mutate({ name: "New role family" })}
          >
            ＋ add role family
          </button>
        </div>
        <div className="fam-list">
          {(families ?? []).map((f) => (
            <RoleFamilyCard
              key={f.id}
              profileId={profileId}
              family={f}
              roles={targetRoles.filter((r) => r.family_id === f.id)}
            />
          ))}
          {families?.length === 0 && (
            <div className="info-banner">
              {uploading ? (
                <>
                  <span className="spinner">◴</span> Reading your CV — the AI reads it closely, so
                  this can take 15-20 seconds.
                </>
              ) : (
                <>
                  No role families yet —{" "}
                  <a
                    href="#"
                    onClick={(e) => {
                      e.preventDefault();
                      fileRef.current?.click();
                    }}
                  >
                    upload a CV
                  </a>{" "}
                  and they&apos;ll be built for you, or add one above.
                </>
              )}
            </div>
          )}
        </div>
        <div className="annotation">
          Each family is searched as its own stream, so unrelated interests are judged on
          their own merits rather than blended together.
        </div>

        {/* ── Extra requirements ── */}
        <div className="subhead">Extra requirements</div>
        <RequirementRows
          profileId={profileId}
          mustHave={g?.must_have ?? []}
          avoid={g?.avoid ?? []}
        />

        {/* ── Preferences ── */}
        <div className="subhead">Preferences</div>
        <div className="pref-list">
          <div className="pref-card">
            <div className="pref-label">Seniority</div>
            <div className="pref-field">
              <SeniorityPicker profileId={profileId} attributes={seniorityAttrs} />
            </div>
            <HardSoftToggle
              value={groupEnforcement(seniorityAttrs, "soft")}
              onChange={(v) => setGroupEnforcement(seniorityAttrs, v)}
              disabled={seniorityAttrs.length === 0}
              disabledReason="Pick a seniority level first — there's nothing to enforce yet."
            />
          </div>

          <div className="pref-card">
            <div className="pref-label">Salary</div>
            <div className="pref-field">
              <SalarySlider profileId={profileId} attribute={salaryAttr} />
            </div>
            <HardSoftToggle
              value={salaryAttr ? enforcementOf(salaryAttr) : "soft"}
              onChange={(v) => salaryAttr && update.mutate({ id: salaryAttr.id, enforcement: v })}
              disabled={!salaryAttr}
              disabledReason="Set a salary range first — there's nothing to enforce yet."
            />
          </div>

          <div className="pref-card">
            <div className="pref-label">Location</div>
            <div className="pref-field">
              <LocationPicker
                profileId={profileId}
                attributes={locationAttrs}
                countryAttributes={g?.country ?? []}
                scopeAttributes={g?.location_scope ?? []}
              />
            </div>
            <HardSoftToggle
              value={cityAttr ? enforcementOf(cityAttr) : "hard"}
              onChange={(v) => cityAttr && update.mutate({ id: cityAttr.id, enforcement: v })}
              disabled={!cityAttr}
              disabledReason="Enter a city or region first — there's nothing to enforce yet."
            />
          </div>

          <div className="pref-card">
            <div className="pref-label">Work style</div>
            <div className="pref-field">
              <WorkStylePicker
                profileId={profileId}
                attributes={locationAttrs}
                enforcement={workStyleEnforcement}
              />
            </div>
            <HardSoftToggle
              value={workStyleEnforcement}
              onChange={(v) => setGroupEnforcement(workTypeAttrs, v)}
              disabled={workTypeAttrs.length === 0}
              disabledReason="Pick a work style first — there's nothing to enforce yet."
            />
          </div>
        </div>

        <div className="bottom-row">
          <div className="action-row">
            <button className="btn btn-primary" onClick={runSearch} disabled={busy || uploading}>
              {busy ? "Starting…" : "▶ Run New Search"}
            </button>
            <button
              className="btn btn-secondary"
              onClick={() => fileRef.current?.click()}
              disabled={busy || uploading}
            >
              {uploading ? "Reading CV…" : "↑ Upload new CV"}
            </button>
            {uploading && (
              <span className="annotation" style={{ marginLeft: 8 }}>
                <span className="spinner">◴</span> Takes 15-20 seconds.
              </span>
            )}
            <input ref={fileRef} type="file" accept=".pdf,.docx,.txt" hidden onChange={onUpload} />
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
