export type AttributeType =
  | "past_role"
  | "skill"
  | "experience"
  | "qualification"
  | "seniority"
  | "target_role"
  | "sector_target"
  | "salary"
  | "location"
  | "country"
  | "location_scope"
  | "custom";

export interface Profile {
  id: number;
  name: string;
  is_active: boolean;
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

export interface Blocklist {
  domains: string[];
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
}

export interface AttributesResponse {
  by_type: Record<AttributeType, Attribute[]>;
  items: Attribute[];
}

export interface Confidence {
  score: number;
  missing: string[];
  tip: string;
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

export interface Role {
  id: number;
  profile_id: number;
  external_id?: string | null;
  title: string;
  company?: string | null;
  location?: string | null;
  url?: string | null;
  tags?: string[] | null;
  salary_text?: string | null;
  source?: string | null;
  fit_rank?: number | null;
  ai_analysis?: string | null;
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
  result_count: number;
  started_at: string;
  finished_at?: string | null;
}

export interface SearchStart {
  run_id: number;
  status: string;
  searches_remaining: number;
}
