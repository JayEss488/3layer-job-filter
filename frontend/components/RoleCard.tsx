"use client";

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
const WHY_MATCHED = "§why-matched";
const YOUR_FIT = "§your-fit";

interface Analysis {
  /** The judge's one-line description of the role, lifted out of the box. */
  headline: string | null;
  /** Cluster note / "closest available match" warning — always precede the headline. */
  notes: string[];
  /** Un-sectioned body lines from a verdict judged before the markers existed. */
  legacy: string[];
  whyMatched: string[];
  yourFit: string[];
}

/**
 * Splits the analysis into its two distinct questions. The judge answers "why
 * this matched what you're after" and "could you actually do it" separately
 * (see full_auto's _FINAL_EVAL_REASONING), but they used to render as one
 * undifferentiated run of ✓ lines, which read as the same point made twice.
 *
 * Every verdict persisted before FINAL_EVAL_PROMPT_VERSION 8 has no section
 * markers, and those rows stay on screen until a re-judge — so anything after
 * the headline that isn't in a section falls through to `legacy` and renders
 * flat, the way it did before. Dropping it instead would quietly delete the
 * match reasons from every existing result.
 */
function parseAnalysis(text: string): Analysis {
  const out: Analysis = { headline: null, notes: [], legacy: [], whyMatched: [], yourFit: [] };
  let bucket: "lead" | "whyMatched" | "yourFit" = "lead";
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    if (line === WHY_MATCHED) {
      bucket = "whyMatched";
    } else if (line === YOUR_FIT) {
      bucket = "yourFit";
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
  const companyLine = [role.company, role.location].filter(Boolean).join(" — ");
  const a = role.ai_analysis ? parseAnalysis(role.ai_analysis) : null;
  const hasBody =
    !!a && (a.notes.length || a.legacy.length || a.whyMatched.length || a.yourFit.length) > 0;
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
              {a.legacy.length > 0 && (
                <div className="an-sec">
                  {a.legacy.map((l, i) => (
                    <div key={i} className={l.startsWith("⚠") ? "concern" : undefined}>
                      {l}
                    </div>
                  ))}
                </div>
              )}
              {a.whyMatched.length > 0 && (
                <div className="an-sec">
                  <div className="an-h">Why this matched</div>
                  {a.whyMatched.map((l, i) => (
                    <div key={i}>{l}</div>
                  ))}
                </div>
              )}
              {a.yourFit.length > 0 && (
                <div className="an-sec">
                  <div className="an-h">Why you&apos;d be good at it</div>
                  {a.yourFit.map((l, i) => (
                    <div key={i} className={l.startsWith("⚠") ? "concern" : undefined}>
                      {l}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </>
      )}

      {actions && <div className={`card-actions${indentActions ? "" : " flush"}`}>{actions}</div>}
    </div>
  );
}
