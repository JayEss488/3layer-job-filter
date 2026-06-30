"use client";

import type { Role } from "@/lib/types";

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
  return (
    <div className={`card${variant ? ` ${variant}` : ""}`}>
      <div className="card-top">
        {showRank && (
          <div className="card-rank">{role.fit_rank ?? "·"}</div>
        )}
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
          {role.tags && role.tags.length > 0 && (
            <div className="card-tags">
              {role.tags.map((t, i) => (
                <span className="tag" key={i}>
                  {t}
                </span>
              ))}
              {role.salary_text && <span className="tag">{role.salary_text}</span>}
            </div>
          )}
        </div>
        {meta && <div className="applied-meta">{meta}</div>}
      </div>

      {showAnalysis && role.ai_analysis && (
        <div className="card-analysis">{role.ai_analysis}</div>
      )}

      {actions && (
        <div className={`card-actions${indentActions ? "" : " flush"}`}>{actions}</div>
      )}
    </div>
  );
}
