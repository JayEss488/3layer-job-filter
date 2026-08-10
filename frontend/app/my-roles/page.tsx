"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Nav } from "@/components/Nav";
import { RoleCard } from "@/components/RoleCard";
import { SalaryPeriodToggle } from "@/components/SalaryPeriodToggle";
import { api } from "@/lib/api";
import { useRoles } from "@/lib/hooks";
import { NO_RESPONSE_PROMPT_DAYS, PRIMARY_OUTCOMES, daysSince } from "@/lib/outcomes";
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
  // "ignored" is folded in here: the only way a role reaches that status is the
  // pipeline auto-hiding an already-shown role it just confirmed dead/expired
  // (see engine._auto_hide_dead_roles) -- there's no dedicated Ignored tab, so
  // without this a role the app itself caught as dead would simply vanish with
  // no visible trace anywhere.
  const deleted = useRoles(activeId ?? null, "crossed,deleted,ignored");
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

  const byDateDesc = (a: string, b: string) => new Date(b).getTime() - new Date(a).getTime();

  const lists: Record<Tab, Role[]> = {
    saved: saved.data ?? [],
    inbox: inbox.data ?? [],
    // Newest-removed first (crossed/deleted both just bump updated_at).
    deleted: [...(deleted.data ?? [])].sort((a, b) => byDateDesc(a.updated_at, b.updated_at)),
    // Newest-applied first.
    applied: [...(applied.data ?? [])].sort((a, b) =>
      byDateDesc(a.applied_at ?? a.updated_at, b.applied_at ?? b.updated_at)
    ),
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
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            {/* Only offered when something on this tab actually has a parsed
                salary — see SalaryPeriodToggle. */}
            <SalaryPeriodToggle show={current.some((r) => r.salary_period)} />
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
            const overdue = s === "pending" && daysSince(role.applied_at) >= NO_RESPONSE_PROMPT_DAYS;
            return (
              <RoleCard
                key={role.id}
                role={role}
                variant={s === "rejected" || s === "no_response" ? "dim" : ""}
                showAnalysis
                // The nudge sits where the answer buttons already are. This
                // field went completely unused past "pending" for its entire
                // life, which was never a control problem -- nobody had a reason
                // to come back and say what happened, so nobody did.
                meta={
                  overdue ? (
                    <span className="concern">
                      Applied {daysSince(role.applied_at)} days ago — heard anything back?
                    </span>
                  ) : role.applied_at ? (
                    `Applied ${fmtDate(role.applied_at)}`
                  ) : undefined
                }
                actions={
                  <>
                    {PRIMARY_OUTCOMES.map((opt) => (
                      <button
                        key={opt}
                        className={`btn sm ${
                          s === opt
                            ? opt === "interview" || opt === "offer"
                              ? "btn-primary"
                              : "btn-ghost"
                            : "btn-secondary"
                        }`}
                        onClick={() => setStatus.mutate({ id: role.id, s: opt })}
                      >
                        {opt[0].toUpperCase() + opt.slice(1)}
                      </button>
                    ))}
                    {/* Separated from the row above: five equal-weight buttons
                        read worse than four, and this one is a different kind of
                        answer -- an absence rather than an event. Deliberately
                        NOT final; the buttons above stay live so a late reply
                        can correct it. */}
                    <button
                      className={`btn sm ${s === "no_response" ? "btn-ghost" : "btn-secondary"}`}
                      style={{ marginLeft: "auto", opacity: s === "no_response" ? 1 : 0.75 }}
                      onClick={() => setStatus.mutate({ id: role.id, s: "no_response" })}
                    >
                      No response yet
                    </button>
                  </>
                }
              />
            );
          })}
      </div>
    </div>
  );
}
