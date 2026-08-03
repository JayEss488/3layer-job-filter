"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Nav } from "@/components/Nav";
import { RoleCard } from "@/components/RoleCard";
import { api } from "@/lib/api";
import { useRoles } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";
import type { ApplicationStatus, Role } from "@/lib/types";

type Tab = "saved" | "inbox" | "deleted" | "applied";

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
  const [tab, setTab] = useState<Tab>("inbox");

  const saved = useRoles(activeId ?? null, "saved");
  const inbox = useRoles(activeId ?? null, "new");
  const deleted = useRoles(activeId ?? null, "crossed,deleted");
  const applied = useRoles(activeId ?? null, "applied");

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["roles", activeId] });
    qc.invalidateQueries({ queryKey: ["stats", activeId] });
  };
  const apply = useMutation({ mutationFn: api.apply, onSuccess: invalidate });
  const save = useMutation({ mutationFn: api.save, onSuccess: invalidate });
  const del = useMutation({ mutationFn: api.deleteRole, onSuccess: invalidate });
  const clearAll = useMutation({ mutationFn: api.clearAllRoles, onSuccess: invalidate });
  const setStatus = useMutation({
    mutationFn: (v: { id: number; s: ApplicationStatus }) =>
      api.setApplicationStatus(v.id, v.s),
    onSuccess: invalidate,
  });

  if (!activeId) {
    return <div className="app"><Nav /><div className="center-pad">Loading…</div></div>;
  }

  const lists: Record<Tab, Role[]> = {
    saved: saved.data ?? [],
    inbox: inbox.data ?? [],
    deleted: deleted.data ?? [],
    applied: applied.data ?? [],
  };
  const current = lists[tab];

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        <div className="ptabs" style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
          <div style={{ display: "flex" }}>
            {(["saved", "inbox", "deleted", "applied"] as Tab[]).map((t) => (
              <div
                key={t}
                className={`ptab ${tab === t ? "on" : "off"}`}
                onClick={() => setTab(t)}
              >
                {t[0].toUpperCase() + t.slice(1)}
                <span className="count">({lists[t].length})</span>
              </div>
            ))}
          </div>
          {tab === "inbox" && current.length > 0 && (
            <button
              className="btn btn-ghost sm"
              onClick={() => {
                if (
                  activeId &&
                  window.confirm(
                    "Clear all roles from the Inbox? Saved and applied roles are kept — " +
                      "everything else can be restored from the Deleted tab."
                  )
                ) {
                  clearAll.mutate(activeId);
                }
              }}
              disabled={clearAll.isPending}
            >
              {clearAll.isPending ? "Clearing…" : "Clear all"}
            </button>
          )}
        </div>

        {current.length === 0 && (
          <div className="center-pad">No {tab} roles yet.</div>
        )}

        {tab === "saved" &&
          current.map((role) => (
            <RoleCard
              key={role.id}
              role={role}
              showAnalysis
              actions={
                <>
                  <button className="btn btn-primary sm" onClick={() => apply.mutate(role.id)}>
                    Mark as applied
                  </button>
                  <button className="btn btn-ghost sm" onClick={() => del.mutate(role.id)}>
                    ✕ Delete
                  </button>
                </>
              }
            />
          ))}

        {tab === "inbox" &&
          current.map((role) => (
            <RoleCard
              key={role.id}
              role={role}
              showAnalysis
              actions={
                <>
                  <button className="btn btn-secondary sm" onClick={() => save.mutate(role.id)}>
                    ✓ Save
                  </button>
                  <button className="btn btn-primary sm" onClick={() => apply.mutate(role.id)}>
                    Mark as applied
                  </button>
                  <button className="btn btn-ghost sm" onClick={() => del.mutate(role.id)}>
                    ✕ Delete
                  </button>
                </>
              }
            />
          ))}

        {tab === "deleted" &&
          current.map((role) => (
            <RoleCard
              key={role.id}
              role={role}
              variant="ignored"
              showAnalysis
              actions={
                <button className="btn btn-secondary sm" onClick={() => save.mutate(role.id)}>
                  ✓ Restore to Saved
                </button>
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
                showAnalysis
                meta={role.applied_at ? `Applied ${fmtDate(role.applied_at)}` : undefined}
                actions={(["pending", "interview", "rejected"] as ApplicationStatus[]).map(
                  (opt) => (
                    <button
                      key={opt}
                      className={`btn sm ${s === opt ? (opt === "interview" ? "btn-primary" : "btn-ghost") : "btn-secondary"}`}
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
