"use client";

import { useState } from "react";

import { VERDICT_LABEL } from "@/lib/types";
import type { Role, RoleVerdict } from "@/lib/types";

interface Props {
  role: Role;
  /** Optional rank shown on the left (search page). */
  showRank?: boolean;
  /** Extra className for the card (crossed / ignored / dim). */
  variant?: "" | "crossed" | "ignored" | "dim";
  /** Render the analysis block (search page only). */
  showAnalysis?: boolean;
  /** Right-aligned meta in the header (e.g. "Applied 24 Jun"). */
  meta?: React.ReactNode;
  /** The action row content. */
  actions?: React.ReactNode;
  /** Indent actions under the rank column (search layout). */
  indentActions?: boolean;
}

/** Section markers emitted by engine._compose_analysis. */
const ROLE_TYPE = "§role-type";
const QUALIFICATION = "§qualification";
const AI_REASONING = "§ai-reasoning";

interface Analysis {
  /** The judge's one-line plain-language description of the role, lifted out of the box. */
  headline: string | null;
  /** Cluster note / "closest available match" warning — always precede the headline. */
  notes: string[];
  /** Un-sectioned body lines from a verdict judged before these markers existed. */
  legacy: string[];
  /** What kind of day-to-day work this is — always visible, no expand. */
  roleType: string[];
  /** Qualified-or-not verdict + concern count/bullets — behind "Show more". */
  qualification: string[];
  /** One synthesized narrative paragraph — behind "Show more". */
  aiReasoning: string[];
}

/**
 * Splits the analysis into its sections. `roleType` always renders;
 * `qualification`/`aiReasoning` hide behind the "Show more" toggle (see
 * hasNewSections in the component below).
 *
 * Rows judged under an older FINAL_EVAL_PROMPT_VERSION carry none of these
 * markers (or an earlier version's now-unrecognised ones) and stay on screen
 * until next re-judged — anything after the headline that isn't one of the
 * three current markers falls through to `legacy` and renders flat, so an old
 * result doesn't quietly lose its reasoning. An unrecognised `§`-prefixed
 * marker from a since-retired format is dropped rather than shown as literal
 * marker text.
 */
function parseAnalysis(text: string): Analysis {
  const out: Analysis = {
    headline: null, notes: [], legacy: [], roleType: [], qualification: [], aiReasoning: [],
  };
  let bucket: "lead" | "roleType" | "qualification" | "aiReasoning" = "lead";
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    if (line === ROLE_TYPE) {
      bucket = "roleType";
    } else if (line === QUALIFICATION) {
      bucket = "qualification";
    } else if (line === AI_REASONING) {
      bucket = "aiReasoning";
    } else if (bucket === "lead" && line.startsWith("§")) {
      // A marker from a retired format -- skip rather than show it as text.
      continue;
    } else if (bucket === "lead") {
      // engine._compose_analysis always emits notes before the summary, so the
      // first line that isn't one is the headline and everything after it is body.
      if (out.headline === null && !line.startsWith("Matched via:") && !line.startsWith("⚠"))
        out.headline = line;
      else if (out.headline === null) out.notes.push(line);
      else out.legacy.push(line);
    } else {
      out[bucket].push(line);
    }
  }
  return out;
}

function factChips(role: Role): string[] {
  // Only what the AI actually read off the listing — a null means the listing
  // was silent, and no chip is better than a guessed one.
  return [
    role.salary_text,
    role.work_style,
    role.seniority_level,
    role.deadline_text ? `Apply by ${role.deadline_text}` : null,
  ].filter((v): v is string => !!v && !!v.trim());
}

export function RoleCard({
  role,
  showRank = false,
  variant = "",
  showAnalysis = false,
  meta,
  actions,
  indentActions = false,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const companyLine = [role.company, role.location].filter(Boolean).join(" — ");
  const a = role.ai_analysis ? parseAnalysis(role.ai_analysis) : null;
  const hasNewSections =
    !!a && (a.roleType.length > 0 || a.qualification.length > 0 || a.aiReasoning.length > 0);
  const hasDetail = !!a && (a.legacy.length > 0 || a.qualification.length > 0 || a.aiReasoning.length > 0);
  const hasBody = !!a && (a.notes.length > 0 || a.roleType.length > 0 || hasDetail);
  const facts = factChips(role);
  const verdict = role.verdict as RoleVerdict | null | undefined;

  return (
    <div className={`card${variant ? ` ${variant}` : ""}`}>
      <div className="card-top">
        {showRank && <div className="card-rank">{role.fit_rank ?? "·"}</div>}
        <div className="card-main">
          <div className="card-title">
            {role.title}
            {role.url && (
              <a href={role.url} target="_blank" rel="noreferrer">
                ↗ view role
              </a>
            )}
          </div>
          {companyLine && <div className="card-company">{companyLine}</div>}
          {facts.length > 0 && (
            <div className="card-tags">
              {facts.map((f, i) => (
                <span className="tag" key={i}>
                  {f}
                </span>
              ))}
            </div>
          )}
        </div>
        <div className="card-corner">
          {verdict && VERDICT_LABEL[verdict] && (
            <span className={`verdict v-${verdict}`}>{VERDICT_LABEL[verdict]}</span>
          )}
          {meta && <div className="applied-meta">{meta}</div>}
        </div>
      </div>

      {showAnalysis && a && (
        <>
          {a.headline && (
            <div className={`card-headline${indentActions ? "" : " flush"}`}>{a.headline}</div>
          )}
          {hasBody && (
            <div className={`card-analysis${indentActions ? "" : " flush"}`}>
              {a.notes.map((n, i) => (
                <div className="an-note" key={i}>
                  {n}
                </div>
              ))}
              {a.roleType.length > 0 && (
                <div className="an-sec">
                  <div className="an-h">Role type</div>
                  {a.roleType.map((l, i) => (
                    <div key={i}>{l}</div>
                  ))}
                </div>
              )}
              {(!hasNewSections || expanded) && (
                <>
                  {a.legacy.length > 0 && (
                    <div className="an-sec">
                      {a.legacy.map((l, i) => (
                        <div key={i} className={l.startsWith("⚠") ? "concern" : undefined}>
                          {l}
                        </div>
                      ))}
                    </div>
                  )}
                  {a.qualification.length > 0 && (
                    <div className="an-sec">
                      <div className="an-h">Qualification</div>
                      {a.qualification.map((l, i) => (
                        <div
                          key={i}
                          className={
                            l.startsWith("⚠") ? "concern" : l.startsWith("- ") ? "an-sub" : undefined
                          }
                        >
                          {l}
                        </div>
                      ))}
                    </div>
                  )}
                  {a.aiReasoning.length > 0 && (
                    <div className="an-sec">
                      <div className="an-h">AI reasoning</div>
                      {a.aiReasoning.map((l, i) => (
                        <div key={i}>{l}</div>
                      ))}
                    </div>
                  )}
                </>
              )}
              {hasNewSections && hasDetail && (
                <button
                  type="button"
                  className="ghost tiny"
                  style={{ alignSelf: "flex-start" }}
                  onClick={() => setExpanded((v) => !v)}
                >
                  {expanded ? "Show less" : "Show more"}
                </button>
              )}
            </div>
          )}
        </>
      )}

      {actions && <div className={`card-actions${indentActions ? "" : " flush"}`}>{actions}</div>}
    </div>
  );
}
