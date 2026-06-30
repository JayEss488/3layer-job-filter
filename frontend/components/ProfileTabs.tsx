"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";

export function ProfileTabs() {
  const { profiles, activeId, setActiveId } = useProfiles();
  const qc = useQueryClient();
  const [renamingId, setRenamingId] = useState<number | null>(null);
  const [draft, setDraft] = useState("");

  const refresh = () => qc.invalidateQueries({ queryKey: ["profiles"] });

  async function createProfile() {
    const p = await api.createProfile();
    await refresh();
    setActiveId(p.id);
  }

  async function rename(id: number) {
    if (draft.trim()) await api.updateProfile(id, { name: draft.trim() });
    setRenamingId(null);
    refresh();
  }

  async function remove(id: number) {
    if (!confirm("Delete this profile and all its roles?")) return;
    try {
      await api.deleteProfile(id);
      await refresh();
      const next = profiles.find((p) => p.id !== id);
      if (next) setActiveId(next.id);
    } catch (e) {
      alert((e as Error).message);
    }
  }

  return (
    <div className="profile-tab-bar">
      {profiles.map((p) => (
        <div
          key={p.id}
          className={`profile-tab${p.id === activeId ? " active" : ""}`}
          onClick={() => setActiveId(p.id)}
          onDoubleClick={() => {
            setRenamingId(p.id);
            setDraft(p.name);
          }}
        >
          {renamingId === p.id ? (
            <input
              autoFocus
              className="profile-tab-rename"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onBlur={() => rename(p.id)}
              onKeyDown={(e) => e.key === "Enter" && rename(p.id)}
              onClick={(e) => e.stopPropagation()}
            />
          ) : (
            <>
              {p.name}
              {profiles.length > 1 && (
                <span
                  className="tab-x"
                  onClick={(e) => {
                    e.stopPropagation();
                    remove(p.id);
                  }}
                >
                  ✕
                </span>
              )}
            </>
          )}
        </div>
      ))}
      <div className="profile-tab add-tab" onClick={createProfile}>
        + new
      </div>
    </div>
  );
}
