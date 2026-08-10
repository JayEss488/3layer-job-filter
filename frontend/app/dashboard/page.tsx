"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { IntentEditor } from "@/components/IntentEditor";
import { Nav } from "@/components/Nav";
import { PreferencesPanel } from "@/components/PreferencesPanel";
import { ProfileTabs } from "@/components/ProfileTabs";
import { RequirementRows } from "@/components/RequirementRows";
import { RoleFamilyCard } from "@/components/RoleFamilyCard";
import { api } from "@/lib/api";
import { useAttributes, useFamilies, useFamilyMutations, useStats } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

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
// early until a profile id exists, and the hooks here need a real one.
function ProfileBody({ profileId }: { profileId: number }) {
  const router = useRouter();
  const qc = useQueryClient();
  const { data: attrs } = useAttributes(profileId);
  const { data: families } = useFamilies(profileId);
  const { data: stats } = useStats(profileId);
  const fileRef = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const fam = useFamilyMutations(profileId);
  const g = attrs?.by_type;

  const targetRoles = g?.target_role ?? [];

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

        {/* profile.intent_text used to be editable ONLY during onboarding, even
            though it outranks every other want-signal at the final judge (see
            snapshot.build_snapshot) -- so a candidate who left it blank there,
            or whose priorities changed, had no way to set it. Same component,
            same three-block order as /onboarding. */}
        <IntentEditor profileId={profileId} />

        {/* ── Preferences ── */}
        {/* Extracted to a shared component so /onboarding renders the identical
            set of filters — see PreferencesPanel for why the two drifting apart
            was a real problem rather than a tidiness one. */}
        <div className="subhead">Preferences</div>
        <PreferencesPanel profileId={profileId} />

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
