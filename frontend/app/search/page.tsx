"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { Nav } from "@/components/Nav";
import { RoleCard } from "@/components/RoleCard";
import { SalaryPeriodToggle } from "@/components/SalaryPeriodToggle";
import { SearchFeedbackBox } from "@/components/SearchFeedbackBox";
import { SearchProgress } from "@/components/SearchProgress";
import { TrainingBanner } from "@/components/TrainingBanner";
import { api } from "@/lib/api";
import { useRoles, useSearchStatus } from "@/lib/hooks";
import { awaitingOutcome } from "@/lib/outcomes";
import { useProfiles } from "@/lib/ProfileContext";
import type { Role } from "@/lib/types";

export default function SearchPage() {
  const { activeId } = useProfiles();
  const qc = useQueryClient();
  const { data: status } = useSearchStatus(activeId);
  const running = status?.status === "running";
  // Drives the stage-target estimates in SearchProgress — the previous
  // finished run's own phase timings, so "about 1 min / 2 mins / 4 mins"
  // reflects this profile's actual pipeline rather than a fixed guess.
  // Only fetched while a run is in progress (that's the only time it's read).
  const { data: lastRunTimings } = useQuery({
    queryKey: ["runTimings", activeId],
    queryFn: () => api.runTimings(activeId!),
    enabled: !!activeId && running,
  });
  // Collapsed by default: these are de-prioritised, not deleted, and the count
  // in the heading is the point — the user should know they exist without
  // having to scroll past them.
  const [showGhosts, setShowGhosts] = useState(false);
  const [outcomePromptHidden, setOutcomePromptHidden] = useState(false);
  // includeProvisional + a poll while running: mid-run "being verified" rows
  // land as soon as the engine's gate+rank phase persists them (~halfway).
  const { data: roles } = useRoles(activeId ?? null, "new,saved,crossed", {
    includeProvisional: true,
    refetchInterval: running ? 2500 : false,
  });
  // Applied roles are fetched here purely to ask the outcome question where the
  // user actually is. The measured problem was never that the /my-roles control
  // was hard to use -- it is that nobody navigates to that tab, so the field sat
  // unused for its whole life. /search is where every session starts.
  const { data: appliedRoles } = useRoles(activeId ?? null, "applied");
  const awaiting = (appliedRoles ?? []).filter(awaitingOutcome);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["roles", activeId] });
    qc.invalidateQueries({ queryKey: ["stats", activeId] });
  };
  const afterJudgement = () => {
    invalidate();
  };
  const tick = useMutation({ mutationFn: api.tick, onSuccess: invalidate });
  const cross = useMutation({ mutationFn: api.cross, onSuccess: afterJudgement });
  const applyRole = useMutation({ mutationFn: api.apply, onSuccess: afterJudgement });
  const clearAll = useMutation({ mutationFn: api.clearAllRoles, onSuccess: invalidate });
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
  // Mid-run cards for the current run only, split by which paint produced them.
  // `verifying` (rank stage: cheap gate passed, 0-100 estimate) renders above
  // `earlyMatches` (embedding stage: nothing has looked at it yet) — a role
  // promoted from one to the other is the SAME row updated in place by the
  // backend, so it moves between these lists rather than appearing in both.
  const provisionalNow = running
    ? (roles ?? []).filter(
        (r) => r.provisional && r.search_run_id === status?.id && r.status !== "crossed",
      )
    : [];
  const verifying = provisionalNow.filter((r) => r.provisional_stage !== "embed");
  const earlyMatches = provisionalNow.filter((r) => r.provisional_stage === "embed");

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
  // Roles that never got a full review: either the quick scorer rated them but
  // the full review never reached them (engine._retain_unreviewed_provisional,
  // stage "rank"), or the run was cancelled/crashed/restarted before this row
  // got past the embedding pre-filter at all (engine._retain_interrupted_provisional,
  // stage "embed" or "rank" pinned as a leftover instead of being deleted).
  // Either way they carry no fit_rank, so they must be pulled out of `current`
  // before it's rendered as this run's ranked picks.
  //
  // Deliberately NOT scoped to latestRunId: this row's originating run is
  // irrelevant to what the section means ("nothing has fully reviewed this
  // yet"), and these rows carry no fit_rank so they can never collide with a
  // ranked badge the way a stale SAVED role could. Scoping this to the latest
  // run used to leave every earlier run's leftovers permanently invisible
  // (still counted in "Showing N results" via `active`, but rendered nowhere)
  // -- a real production case had 55 such rows inflating "64 results" down to
  // 9 actually on screen.
  const isUnreviewed = (r: Role) => r.provisional_stage === "rank" || r.provisional_stage === "embed";
  const unreviewed = active.filter((r) => isUnreviewed(r) && r.status === "new");
  // A role the user Kept (or marked applied) mid-run that the judge then picked
  // is upgraded IN PLACE at finalization -- same row, status untouched, but now
  // carrying this run's real fit_rank (engine.py's finalization loop). It is a
  // ranked pick of this run like any other, so it belongs in `current` with its
  // badge. Filing it under "already saved" instead dropped rank 1 out of the
  // ranked list and made the visible numbering start at 2.
  //
  // The duplicate-rank-badge bug this section split was originally added to fix
  // is a DIFFERENT case: a saved role from an EARLIER run, whose fit_rank is
  // only unique within that run and would collide with this run's numbering.
  // Scoping to latestRunId + a non-null fit_rank keeps that case out, and also
  // keeps out a saved leftover the judge never reached (fit_rank null).
  // High ghost risk: pulled out of the ranked lists into a collapsed section of
  // their own. Only "high" — a "medium" role stays inline carrying its chip,
  // because one ordinary signal is a caveat, not a reason to hide a real job.
  //
  // Scoped to still-`new` rows: once the user has saved or applied to something,
  // hiding it would be overriding a decision they already made, and the chip on
  // the card already tells them what we think.
  const isGhostRisk = (r: Role) => r.ghost_level === "high" && r.status === "new";
  const ghostRisk = active.filter((r) => isGhostRisk(r) && !isUnreviewed(r));
  const isCurrentRankedPick = (r: Role) =>
    r.fit_rank != null && !isUnreviewed(r) && r.search_run_id === latestRunId;
  const inCurrent = (r: Role) =>
    !isUnreviewed(r) &&
    !isGhostRisk(r) &&
    (r.status === "new"
      ? r.search_run_id == null || r.search_run_id === latestRunId
      : isCurrentRankedPick(r));
  const current = active.filter(inCurrent);
  const previous = active.filter(
    (r) =>
      r.status === "new" &&
      !isUnreviewed(r) &&
      !isGhostRisk(r) &&
      r.search_run_id != null &&
      r.search_run_id !== latestRunId
  );
  // Saved roles that aren't one of this run's ranked picks: a completed decision
  // from an earlier run, or one this run kept but never ranked. Shown unranked in
  // their own section below.
  const savedRoles = active.filter((r) => r.status === "saved" && !inCurrent(r));

  // Counts for the meta row. `current` is this run's ranked picks and is the
  // only number that answers "what did this search find" -- everything else on
  // the page is a labelled section of leftovers (earlier runs, roles already
  // saved, rows the full review never reached, ghost-flagged, passed).
  //
  // One combined "Showing N results" conflated the two, and the gap is large
  // enough to read as a bug: a live run showed "Showing 21 results" over 12
  // ranked picks + 4 from an earlier run + 2 already-saved + 3 quick-scored.
  // Nothing was miscounted -- all 21 render -- but the headline number claimed
  // the search had found 21 roles when it had found 12. Both numbers are still
  // summed from the exact buckets rendered below, never derived by subtraction,
  // so neither can drift from what is actually on screen.
  const currentCount = current.length;
  const belowCount =
    previous.length + savedRoles.length + unreviewed.length + ghostRisk.length + crossed.length;
  const totalCount = currentCount + belowCount;

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        {/* Asked here rather than on /my-roles because that is where the user
            is. Dismissible per session: a prompt that cannot be silenced stops
            being a prompt and becomes furniture. */}
        {!running && awaiting.length > 0 && !outcomePromptHidden && (
          <div className="annotation" style={{ display: "flex", gap: 10, alignItems: "center" }}>
            <span>
              You applied to {awaiting.length} role{awaiting.length === 1 ? "" : "s"} over three
              weeks ago. Did any of them come back to you?
            </span>
            {/* "Update them", not "Tell us": this records the outcome against
                your own applications on /my-roles, and it is the only ground
                truth the ghost-listing rules have to calibrate against. The old
                wording read as sending feedback to someone, which it never was
                and certainly isn't now. */}
            <a className="btn btn-secondary sm" href="/my-roles">
              Update them
            </a>
            <button
              className="btn btn-ghost sm"
              style={{ marginLeft: "auto" }}
              onClick={() => setOutcomePromptHidden(true)}
            >
              Not now
            </button>
          </div>
        )}
        {!running && (
          <SearchFeedbackBox profileId={activeId} />
        )}
        <TrainingBanner />

        {running && (
          <div className="warning-banner">
            {/* The stage timeline carries the "how long does this take" answer
                (see SearchProgress); the engine's own live phase message and the
                cancel control sit under it. */}
            <SearchProgress
              startedAt={status?.started_at}
              stage1Done={earlyMatches.length > 0 || verifying.length > 0}
              stage2Done={verifying.length > 0}
              lastRunPhases={lastRunTimings?.phases}
              lastRunTotalSeconds={lastRunTimings?.total_seconds}
            />
            <div className="warning-banner-row search-progress-foot">
              <span>{status?.message || "Starting up…"}</span>
              <button
                className="btn btn-ghost sm"
                onClick={() => activeId && cancelSearch.mutate(activeId)}
                disabled={cancelSearch.isPending}
              >
                {cancelSearch.isPending ? "Cancelling…" : "Cancel Search"}
              </button>
            </div>
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
            <span>
              {/* See the currentCount/belowCount note above: the headline number is
                  this search's ranked picks only, and everything else is counted
                  separately rather than folded in. */}
              Showing{" "}
              <span className="count">
                {currentCount} result{currentCount === 1 ? "" : "s"}
              </span>{" "}
              from this search
              {belowCount > 0 && (
                <>
                  {" · "}
                  <span className="count">{belowCount} more</span> below
                </>
              )}
            </span>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              {/* Only offered when something on screen actually has a parsed
                  salary — see SalaryPeriodToggle. */}
              <SalaryPeriodToggle show={(roles ?? []).some((r) => r.salary_period)} />
            {totalCount > 0 && (
              <button
                className="btn btn-ghost sm"
                onClick={() => {
                  if (
                    activeId &&
                    window.confirm(
                      "Clear all roles from this list? Saved and applied roles are kept — " +
                        "everything else can be restored from the Deleted tab in My Roles."
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
              Quick-scored by AI (about 3 minutes in). These are provisional — anything you
              don&apos;t Keep will disappear once the full review finishes, at around 6
              minutes, if it doesn&apos;t make the final cut.
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

        {/* Paint 1 of 3, kept underneath once the later paints land. These come
            straight off the semantic pre-filter — no model has read them — so
            they're framed as "what the search is looking at", not as results.
            Same Keep/Pass/Applied actions as any other card: a Keep here follows
            the identical leftover rules (see engine._resolve_leftover_provisional),
            so keeping something the run never verifies returns it to your Inbox
            rather than silently staying saved. */}
        {running && earlyMatches.length > 0 && (
          <>
            <div className="crossed-section-label">
              Early matches — found by keyword/semantic similarity, not yet reviewed
            </div>
            <div className="annotation" style={{ padding: "0 0 8px" }}>
              No AI has read these yet — they land first (about 1 minute 30 in) precisely
              because nothing has reviewed them. They&apos;re here so you can see what the
              search picked up straight away; most will be replaced above as the review
              progresses.
            </div>
            {earlyMatches.map((role) => {
              const kept = role.status === "saved";
              return (
                <RoleCard
                  key={role.id}
                  role={role}
                  variant="dim"
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

        {/* `current` now also holds this run's ranked picks the user already
            acted on mid-run (a Keep during the quick-scoring paint, or a Mark as
            applied) — same row, upgraded in place by the judge, so it keeps its
            rank badge here rather than being demoted into the unranked "already
            saved" section. */}
        {!running && current.map((role: Role) => {
          const saved = role.status === "saved";
          const applied = role.status === "applied";
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
                    disabled={applied}
                  >
                    {saved ? "✓ Saved" : "✓ Save"}
                  </button>
                  <button
                    className="btn btn-secondary"
                    onClick={() => applyRole.mutate(role.id)}
                    disabled={applied}
                  >
                    {applied ? "✓ Applied" : "Mark as applied"}
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

        {/* Paint 3 of 3's trailing section: roles the quick scorer rated well but
            the full AI review ran out of budget before reaching. Anything the
            judge actually looked at and rejected is excluded on the backend, so
            this is only ever "not reached", never "reviewed and failed". No rank
            badge — their fit_rank is null and they aren't part of this run's
            ranking. */}
        {!running && unreviewed.length > 0 && (
          <>
            <div className="crossed-section-label">
              quick-scored only — the full review didn&apos;t reach these
            </div>
            {unreviewed.map((role) => {
              const saved = role.status === "saved";
              return (
                <RoleCard
                  key={role.id}
                  role={role}
                  showAnalysis
                  variant="dim"
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

        {/* Possible ghost listings — hidden behind a count by default, but never
            removed. A warning, not a filter: the rules infer from a posting date
            and the listing's own words, and being wrong must cost the user
            nothing more than a click. Expanded, these carry the FULL action row,
            because the user overrules us, not the other way round. */}
        {!running && ghostRisk.length > 0 && (
          <>
            <button
              type="button"
              className="crossed-section-label"
              style={{ background: "none", border: 0, cursor: "pointer", textAlign: "left" }}
              onClick={() => setShowGhosts((v) => !v)}
            >
              ⚠ {ghostRisk.length} listing{ghostRisk.length === 1 ? "" : "s"} flagged as a
              possible ghost job — {showGhosts ? "hide" : "show"}
            </button>
            {showGhosts &&
              ghostRisk.map((role) => (
                <RoleCard
                  key={role.id}
                  role={role}
                  showAnalysis
                  variant="dim"
                  actions={
                    <>
                      <button
                        className="btn btn-secondary"
                        onClick={() => tick.mutate(role.id)}
                      >
                        ✓ Save
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

        {/* Said once, globally, rather than as a per-card "looks fine" badge.
            Without it an absent warning is indistinguishable from the check not
            running at all, and the user has paid attention cost for a feature
            they cannot tell is working. Suppressed mid-run and when there are no
            results, where "none flagged" would be meaningless rather than
            reassuring. */}
        {!running && ghostRisk.length === 0 && current.length > 0 && (
          <div className="annotation">
            No listings in these results were flagged as possible ghost jobs.
          </div>
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
