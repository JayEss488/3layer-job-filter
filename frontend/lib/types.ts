export type AttributeType =
  | "past_role"
  | "skill"
  | "qualification"
  | "seniority"
  | "target_role"
  | "sector_target"
  | "salary"
  | "location"
  | "country"
  | "location_scope"
  | "custom"
  | "avoid"
  | "must_have";

export interface Profile {
  id: number;
  name: string;
  is_active: boolean;
  intent_text?: string | null;
  search_feedback?: string | null;
  created_at: string;
  updated_at: string;
}

export interface SourceInfo {
  key: string;
  label: string;
  kind: "api" | "ats";
  enabled: boolean;
  last_count: number;
}

export interface ScrapeSetting {
  enabled: boolean;
}

export interface SourceStat {
  key: string;
  label: string;
  discovered: number;
  gated: number;
  shown: number;
  selected: number;
}

export interface RunFunnel {
  run_id: number | null;
  finished_at?: string | null;
  entering: number;
  passed_heuristic_embedding: number;
  passed_gates: number;
  final_judge: number;
  final_judge_rejected: number;
  judge_pool_size: number;
  judge_dupes_suppressed: number;
  shown: number;
}

export interface RunPhase {
  name: string;
  label: string;
  seconds: number;
}

/** One role cluster's own funnel through a run — the per-track breakdown the
 *  run-wide RunFunnel above sums away. Mirrors backend RunClusterOut. */
export interface RunCluster {
  idx: number;
  label: string;
  queue_len: number;
  examined: number;
  gate_survivors: number;
  hard_dropped: number;
  off_sector: number;
  hard_gate_dropped: number;
  rank_floor_rejected: number;
  judge_eligible: number;
  stop_reason: string;
  judged: number;
  judge_reused_from_cache: number;
  judge_strong: number;
  judge_backup: number;
  judge_disqualified: number;
  picks: number;
  fallbacks: string[];
}

/** Per-phase wall time for the last finished search run — mirrors backend
 *  RunTimingsOut. The search-side counterpart to CvParseTiming below. */
export interface RunTimings {
  run_id: number | null;
  finished_at?: string | null;
  total_seconds: number;
  phases: RunPhase[];
  clusters: RunCluster[];
}

export interface SnapshotJob {
  title: string;
  company: string;
  url: string;
  note?: string;
}

export interface SnapshotStage {
  stage: string;
  label: string;
  count: number;
  samples: SnapshotJob[];
}

export interface Snapshot {
  run_id: number | null;
  finished_at?: string | null;
  stages: SnapshotStage[];
}

export interface Blocklist {
  domains: string[];
}

export interface LlmCall {
  model: string;
  prompt_chars: number;
  duration_s: number;
  attempts: number;
  ok: boolean;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  total_tokens?: number | null;
}

export interface ParseStage {
  name: string;
  seconds: number;
  llm_calls: LlmCall[];
}

/** Per-stage wall time for a full CV parse — mirrors backend CvParseTimingOut. */
export interface CvParseTiming {
  filename: string;
  measured_at?: string | null;
  text_chars: number;
  text_words: number;
  generated_summary: boolean;
  total_seconds: number;
  llm_seconds: number;
  stages: ParseStage[];
}

/** How literally a constraint row is applied — mirrors backend config.py. */
export type Enforcement = "hard" | "soft";

/** Mirrors backend config.ENFORCEMENT_DEFAULT + WORK_TYPE_VALUES. A null
 *  `enforcement` on a row means "never set" and must resolve through here, the
 *  same way the backend resolves it via config.enforcement_for — the two are
 *  kept in manual sync, as there's no shared import across the Python/TS
 *  boundary (same convention as PROFICIENCY_CHOICES in AttributeRow). */
const WORK_TYPE_VALUES = new Set(["remote", "hybrid", "on-site", "onsite"]);
const ENFORCEMENT_DEFAULT: Partial<Record<AttributeType, Enforcement>> = {
  avoid: "hard",
  must_have: "hard",
  location: "hard",
  seniority: "soft",
  salary: "soft",
};

export function enforcementOf(attr: Attribute): Enforcement {
  if (attr.enforcement) return attr.enforcement;
  if (attr.type === "location" && WORK_TYPE_VALUES.has(attr.value.toLowerCase().trim()))
    return "soft";
  return ENFORCEMENT_DEFAULT[attr.type] ?? "soft";
}

export type FamilyTier = "active" | "inactive";

export interface RoleFamily {
  id: number;
  profile_id: number;
  name: string;
  tier: FamilyTier;
  position: number;
}

export interface Attribute {
  id: number;
  profile_id: number;
  type: AttributeType;
  value: string;
  weight: number;
  source: string;
  confirmed: boolean;
  proficiency?: string | null;
  evidence_origin?: string | null;
  family_id?: number | null;
  pinned?: boolean;
  /** Null means "never set" — read it through enforcementOf, not directly. */
  enforcement?: Enforcement | null;
}

export interface AttributesResponse {
  by_type: Record<AttributeType, Attribute[]>;
  items: Attribute[];
}

/**
 * What the AI is told about the candidate, shown on /memory. `requirements` is
 * a read-only mirror of the must_have/avoid chips edited elsewhere; `header`
 * and `cv_summary` are hand-editable (PATCH /profiles/{id}/context-header and
 * PATCH /profiles/{id} respectively — see api.ts).
 */
export interface ContextHeader {
  header: string;
  requirements: string[];
  cv_summary: string;
}

export interface Stats {
  searched: number;
  saved: number;
  applied: number;
}

export type RoleStatus =
  | "new"
  | "saved"
  | "crossed"
  | "ignored"
  | "applied"
  | "deleted";

export type ApplicationStatus = "pending" | "interview" | "rejected";

/** The final judge's fit grade — mirrors full_auto's _FINAL_EVAL_SCHEMA. */
export type RoleVerdict = "very_strong" | "strong" | "ok" | "stretch";

export const VERDICT_LABEL: Record<RoleVerdict, string> = {
  very_strong: "Very strong fit",
  strong: "Strong fit",
  ok: "Ok fit",
  stretch: "Stretch fit",
};

export interface Role {
  id: number;
  profile_id: number;
  search_run_id?: number | null;
  external_id?: string | null;
  title: string;
  company?: string | null;
  location?: string | null;
  url?: string | null;
  tags?: string[] | null;
  salary_text?: string | null;
  source?: string | null;
  fit_rank?: number | null;
  /** The cheap rank stage's 0-100 fit estimate; shown only while provisional. */
  rank_score?: number | null;
  /** Mid-run "being verified" placeholder — upgraded/removed when the run finishes. */
  provisional?: boolean;
  /**
   * Which progressive-paint stage this row belongs to (see backend models.Role):
   * "embed" — straight off the cosine pre-filter, no AI has looked at it;
   * "rank"  — cheap gate + 0-100 estimate, no full review yet;
   * null    — a real judged pick.
   * `provisional_stage === "rank"` with `provisional === false` is the one
   * combination that outlives a run: a role the quick scorer rated but the full
   * review never reached, kept on screen under its own heading.
   */
  provisional_stage?: "embed" | "rank" | null;
  ai_analysis?: string | null;
  /** The final judge's grade. Null on rows judged before it existed. */
  verdict?: RoleVerdict | null;
  /** Facts the judge read off the listing. Null where the listing was silent. */
  work_style?: string | null;
  seniority_level?: string | null;
  deadline_text?: string | null;
  status: RoleStatus;
  application_status?: ApplicationStatus | null;
  applied_at?: string | null;
  created_at: string;
}

export interface SearchStatus {
  id: number;
  profile_id: number;
  status: "running" | "done" | "error" | "cancelled";
  message?: string | null;
  warning?: string | null;
  result_count: number | null;
  started_at: string;
  finished_at?: string | null;
}

export interface SearchStart {
  run_id: number;
  status: string;
  searches_remaining: number;
}
