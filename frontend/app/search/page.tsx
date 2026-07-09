"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

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
  const cancelSearch = useMutation({
    mutationFn: (id: number) => api.cancelSearch(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["searchStatus", activeId] }),
  });

  // useSearchStatus stops polling once the run finishes, but the already-mounted
  // roles query has no idea new rows landed — without this, the list stays stale
  // until the user navigates away and back (which forces a remount + refetch).
  const prevStatus = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (prevStatus.current === "running" && status?.status !== "running") {
      invalidate();
    }
    prevStatus.current = status?.status;
  }, [status?.status]);

  if (!activeId) {
    return <div className="app"><Nav /><div className="center-pad">Loading…</div></div>;
  }

  const running = status?.status === "running";
  const active = (roles ?? []).filter((r) => r.status !== "crossed");
  const crossed = (roles ?? []).filter((r) => r.status === "crossed");

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        <TrainingBanner />

        {running && (
          <div className="warning-banner warning-banner-row">
            <span>
              <span className="spinner">◴</span> {status?.message || "Building your matches…"} This
              can take a couple of minutes (fetching, ranking, and reading full role pages).
            </span>
            <button
              className="btn btn-ghost sm"
              onClick={() => activeId && cancelSearch.mutate(activeId)}
              disabled={cancelSearch.isPending}
            >
              {cancelSearch.isPending ? "Cancelling…" : "Cancel Search"}
            </button>
          </div>
        )}
        {status?.status === "error" && (
          <div className="warning-banner">{status.message}</div>
        )}
        {status?.status === "cancelled" && (
          <div className="info-banner">{status.message || "Search cancelled."}</div>
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

        {!running && active.map((role: Role) => {
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
                    className={`btn ${saved ? "btn-primary" : "btn-secondary"}`}
                    onClick={() => tick.mutate(role.id)}
                    disabled={saved}
                  >
                    {saved ? "✓ Saved" : "✓ Save"}
                  </button>
                  <button className="btn btn-ghost" onClick={() => cross.mutate(role.id)}>
                    ✗ Pass
                  </button>
                </>
              }
            />
          );
        })}

        {!running && crossed.length > 0 && (
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
                    <button className="btn btn-secondary" onClick={() => tick.mutate(role.id)}>
                      ✓ Save
                    </button>
                    <button className="btn btn-ghost" disabled>
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
