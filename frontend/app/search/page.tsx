"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { Nav } from "@/components/Nav";
import { RoleCard } from "@/components/RoleCard";
import { TrainingBanner } from "@/components/TrainingBanner";
import { api } from "@/lib/api";
import { useRoles, useSearchStatus } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";
import type { Role } from "@/lib/types";

function timeAgo(iso?: string | null) {
  if (!iso) return "";
  const secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins} min${mins > 1 ? "s" : ""} ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs} hr${hrs > 1 ? "s" : ""} ago`;
  return `${Math.floor(hrs / 24)} day(s) ago`;
}

export default function SearchPage() {
  const { activeId } = useProfiles();
  const qc = useQueryClient();
  const { data: status } = useSearchStatus(activeId);
  const { data: roles } = useRoles(activeId ?? null, "new,saved,crossed");

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["roles", activeId] });
    qc.invalidateQueries({ queryKey: ["stats", activeId] });
  };
  const tick = useMutation({ mutationFn: api.tick, onSuccess: invalidate });
  const cross = useMutation({ mutationFn: api.cross, onSuccess: invalidate });

  if (!activeId) {
    return <div className="screen"><Nav /><div className="center-pad">Loading…</div></div>;
  }

  const running = status?.status === "running";
  const active = (roles ?? []).filter((r) => r.status !== "crossed");
  const crossed = (roles ?? []).filter((r) => r.status === "crossed");

  return (
    <div className="screen">
      <Nav />
      <div className="page-body">
        <TrainingBanner />

        {running && (
          <div className="warning-banner">
            <span className="spinner">◴</span> Building your matches… this can take a couple
            of minutes (fetching, ranking, and reading full role pages).
          </div>
        )}
        {status?.status === "error" && (
          <div className="warning-banner">{status.message}</div>
        )}
        {status?.warning && !running && (
          <div className="warning-banner">⚠ {status.warning}</div>
        )}

        {!running && (
          <div className="meta-row">
            Showing <span className="count">{active.length + crossed.length} results</span>
            {status?.finished_at && ` — last searched ${timeAgo(status.finished_at)}`}
          </div>
        )}

        {!running && (roles?.length ?? 0) === 0 && (
          <div className="center-pad">
            {status?.message || "No roles yet. Hit ▶ Run New Search to get started."}
          </div>
        )}

        {active.map((role: Role) => {
          const saved = role.status === "saved";
          return (
            <RoleCard
              key={role.id}
              role={role}
              showRank
              showAnalysis
              indentActions
              actions={
                <>
                  <button
                    className={`action-btn${saved ? " ticked" : ""}`}
                    onClick={() => tick.mutate(role.id)}
                    disabled={saved}
                  >
                    {saved ? "✓ Saved" : "✓ Save"}
                  </button>
                  <button className="action-btn" onClick={() => cross.mutate(role.id)}>
                    ✗ Pass
                  </button>
                </>
              }
            />
          );
        })}

        {crossed.length > 0 && (
          <>
            <div className="crossed-section-label">passed this session</div>
            {crossed.map((role) => (
              <RoleCard
                key={role.id}
                role={role}
                showRank
                showAnalysis
                indentActions
                variant="crossed"
                actions={
                  <>
                    <button className="action-btn" onClick={() => tick.mutate(role.id)}>
                      ✓ Save
                    </button>
                    <button className="action-btn crossed" disabled>
                      ✗ Passed
                    </button>
                  </>
                }
              />
            ))}
          </>
        )}
      </div>
    </div>
  );
}
