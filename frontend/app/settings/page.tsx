"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { Nav } from "@/components/Nav";
import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";
import type { SourceInfo } from "@/lib/types";

export default function SettingsPage() {
  const qc = useQueryClient();
  const { activeId } = useProfiles();
  const [busy, setBusy] = useState(false);
  const [harvestMsg, setHarvestMsg] = useState<string | null>(null);
  const [domainDraft, setDomainDraft] = useState("");
  const [timingFile, setTimingFile] = useState<File | null>(null);
  const [timingBusy, setTimingBusy] = useState(false);
  const [timingErr, setTimingErr] = useState<string | null>(null);

  const { data: sources } = useQuery({
    queryKey: ["sources"],
    queryFn: () => api.sources(),
  });

  const { data: scrapeSetting } = useQuery({
    queryKey: ["scrapeSetting"],
    queryFn: () => api.scrapeSetting(),
  });

  const { data: blocklist } = useQuery({
    queryKey: ["blocklist"],
    queryFn: () => api.blocklist(),
  });

  const { data: sourceStats } = useQuery({
    queryKey: ["sourceStats"],
    queryFn: () => api.sourceStats(),
  });

  const { data: runFunnel } = useQuery({
    queryKey: ["runFunnel"],
    queryFn: () => api.runFunnel(),
  });

  const { data: snapshot } = useQuery({
    queryKey: ["snapshot"],
    queryFn: () => api.snapshot(),
  });

  const { data: timing } = useQuery({
    queryKey: ["cvParseTiming"],
    queryFn: () => api.cvParseTiming(),
  });

  const { data: runTimings } = useQuery({
    queryKey: ["runTimings"],
    queryFn: () => api.runTimings(),
  });

  const total = (sources ?? []).reduce((n, s) => n + s.last_count, 0);

  async function runTiming() {
    if (!timingFile || timingBusy) return;
    setTimingBusy(true);
    setTimingErr(null);
    try {
      const result = await api.runCvParseTiming(timingFile);
      qc.setQueryData(["cvParseTiming"], result);
    } catch (e) {
      setTimingErr((e as Error).message);
    } finally {
      setTimingBusy(false);
    }
  }

  async function toggleScrape() {
    if (!scrapeSetting || busy) return;
    setBusy(true);
    try {
      const updated = await api.setScrapeSetting(!scrapeSetting.enabled);
      qc.setQueryData(["scrapeSetting"], updated);
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function toggle(target: SourceInfo) {
    if (!sources || busy) return;
    setBusy(true);
    const disabled = sources
      .filter((s) => (s.key === target.key ? s.enabled : !s.enabled))
      .map((s) => s.key);
    try {
      const updated = await api.setSources(disabled);
      qc.setQueryData(["sources"], updated);
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function addDomain() {
    const domain = domainDraft.trim().toLowerCase();
    if (!domain || !blocklist || busy) return;
    setBusy(true);
    try {
      const updated = await api.setBlocklist([...blocklist.domains, domain]);
      qc.setQueryData(["blocklist"], updated);
      setDomainDraft("");
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function removeDomain(domain: string) {
    if (!blocklist || busy) return;
    setBusy(true);
    try {
      const updated = await api.setBlocklist(blocklist.domains.filter((d) => d !== domain));
      qc.setQueryData(["blocklist"], updated);
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function reharvest() {
    if (!activeId) return;
    setBusy(true);
    setHarvestMsg(null);
    try {
      await api.harvestAts(activeId, true);
      setHarvestMsg(
        "Harvest scheduled — new ATS companies for your sectors will appear in the next few searches."
      );
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const pct = (n: number) => (total > 0 ? Math.round((n / total) * 100) : 0);
  const funnelPct = (n: number, base: number) => (base > 0 ? Math.round((n / base) * 100) : 0);

  return (
    <div className="app">
      <Nav />
      <div className="page-body">
        <div className="page-title">Settings</div>

        <div className="panel">
          <div className="panel-h">Search run timings</div>
          <div className="panel-b">
            <div className="annotation">
              Where the last finished search spent its time, phase by phase, plus
              each role track&rsquo;s own funnel through the run. Read-only — these
              numbers are recorded by every search, so nothing here costs API
              credits or re-runs anything.
            </div>
            {runTimings && runTimings.run_id ? (
              <div style={{ marginTop: 14 }}>
                <div className="annotation">
                  Run #{runTimings.run_id} · total{" "}
                  <strong>{runTimings.total_seconds.toFixed(1)}s</strong>
                  {runTimings.finished_at &&
                    ` · finished ${new Date(runTimings.finished_at).toLocaleString()}`}
                </div>
                <div style={{ marginTop: 10 }}>
                  {runTimings.phases.map((ph) => {
                    const barPct =
                      runTimings.total_seconds > 0
                        ? Math.round((ph.seconds / runTimings.total_seconds) * 100)
                        : 0;
                    return (
                      <div key={ph.name} style={{ marginTop: 10 }}>
                        <div className="row" style={{ alignItems: "center" }}>
                          <div style={{ width: 230, flex: "none" }}>{ph.label}</div>
                          <div
                            className="field"
                            style={{ display: "flex", alignItems: "center", gap: 8 }}
                          >
                            <div
                              style={{
                                flex: 1,
                                height: 8,
                                background: "rgba(127,127,127,.15)",
                                borderRadius: 4,
                                overflow: "hidden",
                              }}
                            >
                              <div
                                style={{
                                  width: `${barPct}%`,
                                  height: "100%",
                                  background: "var(--accent)",
                                }}
                              />
                            </div>
                            <span
                              style={{
                                minWidth: 96,
                                textAlign: "right",
                                fontVariantNumeric: "tabular-nums",
                              }}
                            >
                              {ph.seconds.toFixed(2)}s ({barPct}%)
                            </span>
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
                {runTimings.clusters.length > 0 && (
                  <div style={{ marginTop: 18 }}>
                    <div className="annotation" style={{ marginBottom: 6 }}>
                      Per role track — a track that reaches the judge with a healthy
                      pool and still returns nothing strong is the shape the
                      run-wide funnel above can&rsquo;t show.
                    </div>
                    <div style={{ overflowX: "auto" }}>
                      <table style={{ borderCollapse: "collapse", fontSize: 13, minWidth: 640 }}>
                        <thead>
                          <tr style={{ textAlign: "right" }}>
                            <th style={{ textAlign: "left", padding: "4px 10px 4px 0" }}>Track</th>
                            <th style={{ padding: "4px 10px" }}>Queue</th>
                            <th style={{ padding: "4px 10px" }}>Examined</th>
                            <th style={{ padding: "4px 10px" }}>Gate</th>
                            <th style={{ padding: "4px 10px" }}>Judged</th>
                            <th style={{ padding: "4px 10px" }}>Strong</th>
                            <th style={{ padding: "4px 10px" }}>Backup</th>
                            <th style={{ padding: "4px 10px" }}>Shown</th>
                            <th style={{ textAlign: "left", padding: "4px 0 4px 10px" }}>
                              Stopped because
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {runTimings.clusters.map((c) => (
                            <tr
                              key={c.idx}
                              style={{
                                textAlign: "right",
                                fontVariantNumeric: "tabular-nums",
                                borderTop: "1px solid rgba(127,127,127,.15)",
                              }}
                            >
                              <td style={{ textAlign: "left", padding: "4px 10px 4px 0" }}>
                                {c.label || `Cluster ${c.idx}`}
                              </td>
                              <td style={{ padding: "4px 10px" }}>{c.queue_len}</td>
                              <td style={{ padding: "4px 10px" }}>{c.examined}</td>
                              <td style={{ padding: "4px 10px" }}>{c.gate_survivors}</td>
                              <td style={{ padding: "4px 10px" }}>{c.judged}</td>
                              <td style={{ padding: "4px 10px" }}>{c.judge_strong}</td>
                              <td style={{ padding: "4px 10px" }}>{c.judge_backup}</td>
                              <td style={{ padding: "4px 10px" }}>{c.picks}</td>
                              <td
                                className="annotation"
                                style={{ textAlign: "left", padding: "4px 0 4px 10px" }}
                              >
                                {c.stop_reason.replace(/_/g, " ")}
                                {c.fallbacks.length > 0 &&
                                  ` · ${c.fallbacks.join(", ").replace(/_/g, " ")}`}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                )}
              </div>
            ) : (
              <div className="annotation" style={{ marginTop: 10 }}>
                No finished search run yet — run a search and this fills in.
              </div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">CV parse timing</div>
          <div className="panel-b">
            <div className="annotation">
              Upload a CV to measure how long each stage of parsing takes: local
              text extraction, then the AI calls that run <em>in parallel</em> —
              structured extraction, role families + intent draft, and the summary
              + &ldquo;looking for&rdquo; header (that last one is skipped for a
              short CV, whose text is its own summary). It runs the real pipeline
              against a throwaway profile — your saved profiles are never touched —
              and <strong>spends API credits</strong> (the same mid-model calls as
              one real CV upload) each time you press Measure.
            </div>
            <div
              style={{ marginTop: 12, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}
            >
              <input
                type="file"
                accept=".pdf,.docx,.txt"
                disabled={timingBusy}
                onChange={(e) => setTimingFile(e.target.files?.[0] ?? null)}
              />
              <button
                className="btn btn-secondary"
                onClick={runTiming}
                disabled={timingBusy || !timingFile}
              >
                {timingBusy ? "Measuring…" : "⏱ Measure parse time"}
              </button>
            </div>
            {timingBusy && (
              <div className="annotation" style={{ marginTop: 8 }}>
                Running the full parse — the AI calls go out in parallel.
              </div>
            )}
            {timingErr && (
              <div className="annotation" style={{ marginTop: 8, color: "#c0392b" }}>
                {timingErr}
              </div>
            )}
            {timing && timing.measured_at && (
              <div style={{ marginTop: 14 }}>
                <div className="annotation">
                  {timing.filename || "CV"} · {timing.text_words} words · total{" "}
                  <strong>{timing.total_seconds.toFixed(1)}s</strong> (
                  {timing.llm_seconds.toFixed(1)}s in AI calls) ·{" "}
                  {timing.generated_summary
                    ? "summary generated"
                    : "short CV — summary skipped"}{" "}
                  · measured {new Date(timing.measured_at).toLocaleString()}
                </div>
                <div style={{ marginTop: 10 }}>
                  {timing.stages.map((st) => {
                    const barPct =
                      timing.total_seconds > 0
                        ? Math.round((st.seconds / timing.total_seconds) * 100)
                        : 0;
                    return (
                      <div key={st.name} style={{ marginTop: 10 }}>
                        <div className="row" style={{ alignItems: "center" }}>
                          <div style={{ width: 230, flex: "none" }}>{st.name}</div>
                          <div
                            className="field"
                            style={{ display: "flex", alignItems: "center", gap: 8 }}
                          >
                            <div
                              style={{
                                flex: 1,
                                height: 8,
                                background: "rgba(127,127,127,.15)",
                                borderRadius: 4,
                                overflow: "hidden",
                              }}
                            >
                              <div
                                style={{
                                  width: `${barPct}%`,
                                  height: "100%",
                                  background: "var(--accent)",
                                }}
                              />
                            </div>
                            <span
                              style={{
                                minWidth: 96,
                                textAlign: "right",
                                fontVariantNumeric: "tabular-nums",
                              }}
                            >
                              {st.seconds.toFixed(2)}s ({barPct}%)
                            </span>
                          </div>
                        </div>
                        {st.llm_calls.map((c, i) => (
                          <div
                            key={`${st.name}-${i}`}
                            className="annotation"
                            style={{ marginLeft: 12, marginTop: 2 }}
                          >
                            ↳ {c.model} · {c.duration_s.toFixed(2)}s
                            {c.total_tokens != null && ` · ${c.total_tokens} tok`}
                            {c.prompt_tokens != null &&
                              c.completion_tokens != null &&
                              ` (${c.prompt_tokens} in / ${c.completion_tokens} out)`}
                            {c.attempts > 1 && ` · ${c.attempts} attempts`}
                            {!c.ok && " · FAILED"}
                          </div>
                        ))}
                      </div>
                    );
                  })}
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">Full page scraping</div>
          <div className="panel-b">
            <div className="annotation">
              When on, the search reads each shortlisted job&apos;s real page before final
              matching — more accurate results (especially on experience/seniority
              requirements), at the cost of extra time per search. When off, matching
              uses only the short job-board snippet, which is faster but can miss
              requirements not shown in the snippet.
            </div>
            <div style={{ marginTop: 12 }}>
              <div className="row" style={{ alignItems: "center" }}>
                <label
                  className="checkbox-row"
                  style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer" }}
                >
                  <input
                    type="checkbox"
                    checked={scrapeSetting?.enabled ?? true}
                    disabled={busy || !scrapeSetting}
                    onChange={toggleScrape}
                  />
                  Read full job pages before final matching
                </label>
              </div>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">Blocked domains</div>
          <div className="panel-b">
            <div className="annotation">
              Job listings from these domains (and their subdomains) are dropped before
              they're ever stored — use this for SEO-spam job-board clones that slip in
              through aggregator sources.
            </div>
            <div className="field" style={{ marginTop: 12 }}>
              {(blocklist?.domains ?? []).map((d) => (
                <span className="chip" key={d}>
                  {d}
                  <span className="x" onClick={() => removeDomain(d)} role="button" aria-label={`remove ${d}`}>
                    ✕
                  </span>
                </span>
              ))}
              <input
                className="input"
                style={{ width: 200 }}
                placeholder="e.g. spamboard.com"
                value={domainDraft}
                onChange={(e) => setDomainDraft(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && addDomain()}
              />
              <button className="ghost" onClick={addDomain} disabled={busy || !domainDraft.trim()}>
                ＋ add
              </button>
            </div>
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">Discovery sources</div>
          <div className="panel-b">
            <div className="annotation">
              Turn a source on or off and see how many listings each contributed on
              the last search. If one source is flooding your results with
              irrelevant roles, disable it here.
            </div>

            <div style={{ marginTop: 12 }}>
              {(sources ?? []).map((s) => (
                <div key={s.key} className="row" style={{ alignItems: "center" }}>
                  <label
                    className="checkbox-row"
                    style={{ display: "flex", gap: 8, alignItems: "center", cursor: "pointer", flex: "none", width: 180 }}
                  >
                    <input
                      type="checkbox"
                      checked={s.enabled}
                      disabled={busy}
                      onChange={() => toggle(s)}
                    />
                    {s.label}
                    <span className="tag" style={{ opacity: 0.6 }}>
                      {s.kind === "ats" ? "ATS" : "API"}
                    </span>
                  </label>
                  <div className="field" style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <div
                      style={{
                        flex: 1,
                        height: 6,
                        background: "rgba(127,127,127,.15)",
                        borderRadius: 3,
                        overflow: "hidden",
                      }}
                    >
                      <div
                        style={{
                          width: `${pct(s.last_count)}%`,
                          height: "100%",
                          background: s.enabled ? "var(--accent)" : "#999",
                        }}
                      />
                    </div>
                    <span style={{ minWidth: 90, textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
                      {s.last_count} ({pct(s.last_count)}%)
                    </span>
                  </div>
                </div>
              ))}
            </div>

            {total > 0 && (
              <div className="annotation" style={{ marginTop: 8 }}>
                Last run discovered {total} raw listings across all sources.
              </div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">Source performance</div>
          <div className="panel-b">
            <div className="annotation">
              All-time funnel per source: how many jobs it's discovered, how many
              survived the sector/seniority gates and made the shortlist, how many
              made the final AI-picked selection, and how many you actually saved or
              applied to. Useful for deciding which APIs are worth keeping.
            </div>
            <div className="funnel-row funnel-header" style={{ marginTop: 12 }}>
              <div className="funnel-label" />
              <div className="funnel-track" />
              <div className="funnel-counts">
                <span>Disc</span>
                <span>Gated</span>
                <span>Shown</span>
                <span>Sel</span>
              </div>
            </div>
            {(sourceStats ?? []).map((s) => (
              <div key={s.key} className="funnel-row">
                <div className="funnel-label">{s.label}</div>
                <div className="funnel-track">
                  <div className="funnel-seg discovered" style={{ width: "100%" }} />
                  <div className="funnel-seg gated" style={{ width: `${funnelPct(s.gated, s.discovered)}%` }} />
                  <div className="funnel-seg shown" style={{ width: `${funnelPct(s.shown, s.discovered)}%` }} />
                  <div className="funnel-seg selected" style={{ width: `${funnelPct(s.selected, s.discovered)}%` }} />
                </div>
                <div className="funnel-counts">
                  <span>{s.discovered}</span>
                  <span>{s.gated}</span>
                  <span>{s.shown}</span>
                  <span>{s.selected}</span>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">Search funnel (last run)</div>
          <div className="panel-b">
            <div className="annotation">
              How far this run&apos;s candidates got, stage by stage: raw listings
              discovered, how many survived the free heuristic/embedding pre-filter, how
              many survived the cheap AI gates, how many the expensive final AI judge
              accepted, and how many you were actually shown. The per-source view above
              shows this split out by source, all-time — this is all stages together for
              one run.
            </div>
            {runFunnel && runFunnel.run_id ? (
              <>
                <div className="funnel-row funnel-header" style={{ marginTop: 12 }}>
                  <div className="funnel-label" />
                  <div className="funnel-track" />
                  <div className="funnel-counts funnel-counts-5">
                    <span>Enter</span>
                    <span>Embed</span>
                    <span>Gates</span>
                    <span>Judge</span>
                    <span>Shown</span>
                  </div>
                </div>
                <div className="funnel-row">
                  <div className="funnel-label">
                    {runFunnel.finished_at
                      ? new Date(runFunnel.finished_at).toLocaleString()
                      : "Latest run"}
                  </div>
                  <div className="funnel-track">
                    <div className="funnel-seg rf-entering" style={{ width: "100%" }} />
                    <div
                      className="funnel-seg rf-embedding"
                      style={{ width: `${funnelPct(runFunnel.passed_heuristic_embedding, runFunnel.entering)}%` }}
                    />
                    <div
                      className="funnel-seg rf-gates"
                      style={{ width: `${funnelPct(runFunnel.passed_gates, runFunnel.entering)}%` }}
                    />
                    <div
                      className="funnel-seg rf-judge"
                      style={{ width: `${funnelPct(runFunnel.final_judge, runFunnel.entering)}%` }}
                    />
                    <div
                      className="funnel-seg rf-shown"
                      style={{ width: `${funnelPct(runFunnel.shown, runFunnel.entering)}%` }}
                    />
                  </div>
                  <div className="funnel-counts funnel-counts-5">
                    <span>{runFunnel.entering}</span>
                    <span>{runFunnel.passed_heuristic_embedding}</span>
                    <span>{runFunnel.passed_gates}</span>
                    <span>{runFunnel.final_judge}</span>
                    <span>{runFunnel.shown}</span>
                  </div>
                </div>
                <div className="annotation" style={{ marginTop: 4 }}>
                  {(() => {
                    const evaluated = runFunnel.final_judge + runFunnel.final_judge_rejected;
                    const extra = evaluated - runFunnel.judge_pool_size;
                    return (
                      <>
                        Judge stage rejected {runFunnel.final_judge_rejected} of {evaluated} evaluated
                        {extra > 0 && (
                          <>
                            {" "}(judge pool: {runFunnel.judge_pool_size}; {extra} more came from
                            thin-cluster backfill retries)
                          </>
                        )}
                        .
                        {runFunnel.judge_dupes_suppressed > 0 && (
                          <>
                            {" "}{runFunnel.judge_dupes_suppressed} near-duplicate posting
                            {runFunnel.judge_dupes_suppressed === 1 ? "" : "s"} suppressed before
                            the judge (same employer, title &amp; text).
                          </>
                        )}
                      </>
                    );
                  })()}
                </div>
              </>
            ) : (
              <div className="annotation" style={{ marginTop: 12 }}>
                No completed search run yet.
              </div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">ATS company coverage</div>
          <div className="panel-b">
            <div className="annotation">
              The ATS tier searches known company job boards. Re-harvesting finds new
              boards that hire for your target roles and sectors. This runs in the
              background and only spends search credits when your targets have
              changed.
            </div>
            <div className="action-row" style={{ marginTop: 12 }}>
              <button className="btn btn-secondary" onClick={reharvest} disabled={busy || !activeId}>
                🔄 Re-harvest ATS boards for my profile
              </button>
            </div>
            {harvestMsg && (
              <div className="annotation" style={{ marginTop: 8 }}>
                {harvestMsg}
              </div>
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-h">Snapshot (last run, stage by stage)</div>
          <div className="panel-b">
            <div className="annotation">
              What was actually at each stage of the last search — the count plus a
              few random example roles (not the top-ranked ones, so they show what a
              stage really lets through). Copy a stage into an AI to analyse why it
              kept or dropped what it did.
            </div>
            {snapshot && snapshot.run_id ? (
              <>
                <div className="annotation" style={{ marginTop: 4 }}>
                  Run #{snapshot.run_id}
                  {snapshot.finished_at
                    ? ` · ${new Date(snapshot.finished_at).toLocaleString()}`
                    : ""}
                </div>
                {snapshot.stages.map((st) => (
                  <div key={st.stage} className="snapshot-stage">
                    <div className="snapshot-stage-h">
                      <span>{st.label}</span>
                      <span className="snapshot-count">{st.count}</span>
                    </div>
                    {st.samples.length > 0 ? (
                      <ul className="snapshot-jobs">
                        {st.samples.map((j, i) => (
                          <li key={`${st.stage}-${i}`} className="snapshot-job">
                            <span className="snapshot-job-title">
                              {j.title || "(untitled)"}
                              {j.company ? ` — ${j.company}` : ""}
                            </span>
                            {j.note && (
                              <span className="snapshot-job-note">{j.note}</span>
                            )}
                            {j.url && (
                              <a
                                href={j.url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="snapshot-job-url"
                              >
                                {j.url}
                              </a>
                            )}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <div className="annotation">Nothing reached this stage.</div>
                    )}
                  </div>
                ))}
              </>
            ) : (
              <div className="annotation" style={{ marginTop: 12 }}>
                No completed search run yet.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
