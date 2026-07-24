"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";

import { Nav } from "@/components/Nav";
import { RoleCard } from "@/components/RoleCard";
import { SearchFeedbackBox } from "@/components/SearchFeedbackBox";
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
  const running = status?.status === "running";
  // includeProvisional + a poll while running: mid-run "being verified" rows
  // land as soon as the engine's gate+rank phase persists them (~halfway).
  const { data: roles } = useRoles(activeId ?? null, "new,saved,crossed", {
    includeProvisional: true,
    refetchInterval: running ? 2500 : false,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["roles", activeId] });
    qc.invalidateQueries({ queryKey: ["stats", activeId] });
  };
  const tick = useMutation({ mutationFn: api.tick, onSuccess: invalidate });
  const cross = useMutation({ mutationFn: api.cross, onSuccess: invalidate });
  const applyRole = useMutation({ mutationFn: api.apply, onSuccess: invalidate });
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

  // !r.provisional: once the run leaves "running", any provisional row still
  // in the payload is a leftover the engine's cleanup hasn't committed yet
  // (e.g. the gap right after a cancel) — never render it as a real result.
  const active = (roles ?? []).filter((r) => r.status !== "crossed" && !r.provisional);
  const crossed = (roles ?? []).filter((r) => r.status === "crossed" && !r.provisional);
  // Mid-run "being verified" cards for the current run only.
  const verifying = running
    ? (roles ?? []).filter(
        (r) => r.provisional && r.search_run_id === status?.id && r.status !== "crossed",
      )
    : [];

  // Second-search semantics: an unreviewed ('new') role left over from an
  // earlier run shouldn't interleave with this run's fresh picks by fit_rank
  // (each run numbers its own 1..N) -- it moves into its own section below.
  // 'saved' roles are a completed decision, not pending review, so they stay
  // in the main section regardless of which run surfaced them. A null
  // search_run_id (rows from before this field existed) is treated as
  // current rather than hidden, since we have no run to compare it against.
  const latestRunId = active.reduce<number | null>(
    (max, r) => (r.search_run_id != null && (max === null || r.search_run_id > max) ? r.search_run_id : max),
    null
  );
  const current = active.filter(
    (r) => r.status === "new" && (r.search_run_id == null || r.search_run_id === latestRunId)
  );
  const previous = active.filter(
    (r) => r.status === "new" && r.search_run_id != null && r.search_run_id !== latestRunId
  );
  // Saved roles are a completed decision from any run -- shown in their own
  // section below, never mixed into the ranked `current`/`previous` lists
  // (their fit_rank is only unique within whichever run produced it, so
  // mixing them in caused duplicate rank badges alongside this run's picks).
  const savedRoles = active.filter((r) => r.status === "saved");

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        {!running && <SearchFeedbackBox profileId={activeId} />}
        <TrainingBanner />

        {running && (
          <div className="warning-banner warning-banner-row">
            <span>
              <span className="spinner">◴</span> {status?.message || "Building your matches…"} This
              takes a couple of minutes — top candidates appear below as soon as they're ranked,
              then get upgraded once the full AI review finishes.
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

        {running && verifying.length > 0 && (
          <>
            <div className="crossed-section-label">
              Top candidates so far — verifying with the full AI review…
            </div>
            <div className="annotation" style={{ padding: "0 0 8px" }}>
              These are provisional. Anything you don&apos;t Keep will disappear once the
              full review finishes if it doesn&apos;t make the final cut.
            </div>
            {verifying.map((role) => {
              const kept = role.status === "saved";
              return (
                <RoleCard
                  key={role.id}
                  role={role}
                  showRank
                  indentActions
                  actions={
                    <>
                      <button
                        className={`btn ${kept ? "btn-primary" : "btn-secondary"}`}
                        onClick={() => (kept ? cross.mutate(role.id) : tick.mutate(role.id))}
                        title={kept ? "Click to un-keep (pass)" : "Keep this role even if the AI review later rates it lower"}
                      >
                        {kept ? "✓ Kept" : "✓ Keep"}
                      </button>
                      <button className="btn btn-secondary" onClick={() => applyRole.mutate(role.id)}>
                        Mark as applied
                      </button>
                      <button className="btn btn-ghost" onClick={() => cross.mutate(role.id)}>
                        ✗ Pass
                      </button>
                    </>
                  }
                />
              );
            })}
          </>
        )}

        {!running && current.map((role: Role) => {
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
                    onClick={() => (saved ? cross.mutate(role.id) : tick.mutate(role.id))}
                    title={saved ? "Click to unsave" : undefined}
                  >
                    {saved ? "✓ Saved" : "✓ Save"}
                  </button>
                  <button className="btn btn-secondary" onClick={() => applyRole.mutate(role.id)}>
                    Mark as applied
                  </button>
                  <button className="btn btn-ghost" onClick={() => cross.mutate(role.id)}>
                    ✗ Pass
                  </button>
                </>
              }
            />
          );
        })}

        {!running && previous.length > 0 && (
          <>
            <div className="crossed-section-label">from earlier searches</div>
            {previous.map((role) => {
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
                        onClick={() => (saved ? cross.mutate(role.id) : tick.mutate(role.id))}
                        title={saved ? "Click to unsave" : undefined}
                      >
                        {saved ? "✓ Saved" : "✓ Save"}
                      </button>
                      <button className="btn btn-secondary" onClick={() => applyRole.mutate(role.id)}>
                        Mark as applied
                      </button>
                      <button className="btn btn-ghost" onClick={() => cross.mutate(role.id)}>
                        ✗ Pass
                      </button>
                    </>
                  }
                />
              );
            })}
          </>
        )}

        {!running && savedRoles.length > 0 && (
          <>
            <div className="crossed-section-label">already saved</div>
            {savedRoles.map((role) => (
              <RoleCard
                key={role.id}
                role={role}
                showAnalysis
                actions={
                  <>
                    <button
                      className="btn btn-primary"
                      onClick={() => cross.mutate(role.id)}
                      title="Click to unsave"
                    >
                      ✓ Saved
                    </button>
                    <button className="btn btn-secondary" onClick={() => applyRole.mutate(role.id)}>
                      Mark as applied
                    </button>
                    <button className="btn btn-ghost" onClick={() => cross.mutate(role.id)}>
                      ✗ Pass
                    </button>
                  </>
                }
              />
            ))}
          </>
        )}

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
