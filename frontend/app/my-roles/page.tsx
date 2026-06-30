"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Nav } from "@/components/Nav";
import { RoleCard } from "@/components/RoleCard";
import { api } from "@/lib/api";
import { useRoles } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";
import type { ApplicationStatus, Role } from "@/lib/types";

type Tab = "saved" | "ignored" | "applied";

function fmtDate(iso?: string | null) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

export default function MyRolesPage() {
  const { activeId } = useProfiles();
  const qc = useQueryClient();
  const [tab, setTab] = useState<Tab>("saved");

  const saved = useRoles(activeId ?? null, "saved");
  const ignored = useRoles(activeId ?? null, "ignored");
  const applied = useRoles(activeId ?? null, "applied");

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["roles", activeId] });
    qc.invalidateQueries({ queryKey: ["stats", activeId] });
  };
  const apply = useMutation({ mutationFn: api.apply, onSuccess: invalidate });
  const ignore = useMutation({ mutationFn: api.moveToIgnored, onSuccess: invalidate });
  const save = useMutation({ mutationFn: api.save, onSuccess: invalidate });
  const del = useMutation({ mutationFn: api.deleteRole, onSuccess: invalidate });
  const setStatus = useMutation({
    mutationFn: (v: { id: number; s: ApplicationStatus }) =>
      api.setApplicationStatus(v.id, v.s),
    onSuccess: invalidate,
  });

  if (!activeId) {
    return <div className="screen"><Nav /><div className="center-pad">Loading…</div></div>;
  }

  const lists: Record<Tab, Role[]> = {
    saved: saved.data ?? [],
    ignored: ignored.data ?? [],
    applied: applied.data ?? [],
  };
  const current = lists[tab];

  return (
    <div className="screen">
      <Nav />
      <div className="page-body">
        <div className="sub-tab-bar">
          {(["saved", "ignored", "applied"] as Tab[]).map((t) => (
            <div
              key={t}
              className={`sub-tab${tab === t ? " active" : ""}`}
              onClick={() => setTab(t)}
            >
              {t[0].toUpperCase() + t.slice(1)}
              <span className="count">({lists[t].length})</span>
            </div>
          ))}
        </div>

        {current.length === 0 && (
          <div className="center-pad">No {tab} roles yet.</div>
        )}

        {tab === "saved" &&
          current.map((role) => (
            <RoleCard
              key={role.id}
              role={role}
              actions={
                <>
                  <button className="action-btn primary sm" onClick={() => apply.mutate(role.id)}>
                    Mark as applied
                  </button>
                  <button className="action-btn muted sm" onClick={() => ignore.mutate(role.id)}>
                    Move to ignored
                  </button>
                </>
              }
            />
          ))}

        {tab === "ignored" &&
          current.map((role) => (
            <RoleCard
              key={role.id}
              role={role}
              variant="ignored"
              actions={
                <>
                  <button className="action-btn sm" onClick={() => save.mutate(role.id)}>
                    ✓ Save
                  </button>
                  <button className="action-btn primary sm" onClick={() => apply.mutate(role.id)}>
                    Mark as applied
                  </button>
                  <button className="action-btn muted sm" onClick={() => del.mutate(role.id)}>
                    Delete
                  </button>
                </>
              }
            />
          ))}

        {tab === "applied" &&
          current.map((role) => {
            const s = role.application_status ?? "pending";
            return (
              <RoleCard
                key={role.id}
                role={role}
                variant={s === "rejected" ? "dim" : ""}
                meta={role.applied_at ? `Applied ${fmtDate(role.applied_at)}` : undefined}
                actions={(["pending", "interview", "rejected"] as ApplicationStatus[]).map(
                  (opt) => (
                    <button
                      key={opt}
                      className={`action-btn sm${s === opt ? (opt === "interview" ? " primary" : " muted") : ""}`}
                      onClick={() => setStatus.mutate({ id: role.id, s: opt })}
                    >
                      {opt[0].toUpperCase() + opt.slice(1)}
                    </button>
                  )
                )}
              />
            );
          })}
      </div>
    </div>
  );
}
