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
  | "must_have"
  | "max_listing_age"
  // Commute radius in miles as a string ("30"); "0" means no distance limit.
  // Only enforced at location_scope="local" — see LocationPicker.
  | "commute_miles"
  // Single-value boolean, value "true"; no row at all means off (the default).
  // Inherently hard, so it carries no Hard/Soft — see VisaSponsorToggle.
  | "visa_sponsor_only"
  // Single-value: minimum annual salary (GBP) to count as sponsorable, as a
  // string ("41700"); "0" means no floor. Only enforced while visa_sponsor_only
  // is on. No row at all means the standard-applicant default — see
  // VisaSponsorToggle and backend/app/config.DEFAULT_VISA_SPONSOR_MIN_SALARY.
  | "visa_sponsor_min_salary"
  // Single-value boolean, value "true"; no row at all means off (the default).
  // Carries no Hard/Soft because turning it on IS the softening — see
  // AllowOverqualifiedToggle.
  | "allow_overqualified";

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

/** One named reason listings were turned away, for the run-funnel panel's
 *  summary. Mirrors backend RunRejectReasonOut. */
export interface RunRejectReason {
  /** Stable slug — safe to key off; the label wording may change. */
  reason: string;
  label: string;
  count: number;
  /** Which tier caught it: "free" (no model), "gate" (cheap screen), "judge". */
  stage: string;
}

export interface RunFunnel {
  /** Ranked causes, biggest first, non-zero only. NOT a partition: the counts
   *  come from different stages over different populations and soft-axis ones
   *  are counted per axis, so a listing can appear under several. Never total
   *  this list. */
  rejection_summary: RunRejectReason[];
  run_id: number | null;
  finished_at?: string | null;
  entering: number;
  passed_heuristic_embedding: number;
  passed_gates: number;
  final_judge: number;
  final_judge_rejected: number;
  /** How many of those rejects carry the judge's own reason, and how many jobs it
   *  left out of all four of its output lists. judge_unaccounted should be 0 —
   *  anything else went unjudged AND unwritten, and re-competes next run. */
  final_judge_reject_reasoned: number;
  judge_unaccounted: number;
  judge_pool_size: number;
  judge_dupes_suppressed: number;
  decided_family_keys: number;
  decided_family_suppressed: number;
  decided_family_shadow: boolean;
  /** Pre-judge liveness sample (rationed across the rank pool). */
  verify_checked: number;
  verify_dead: number;
  verify_unverifiable: number;
  /** Final-pick liveness — every shown pick, checked unconditionally. */
  final_verify_checked: number;
  final_verify_dead: number;
  final_verify_unverifiable: number;
  final_verify_browser: number;
  final_verify_backfilled: number;
  /** Adzuna picks routed to the tracking-redirect check — a deliberate routing
   *  state, not a host that refused to answer. */
  final_verify_redirect_routed: number;
  /** Scam / CV-farming flags. Read scam_verify_no_sentence first: if it climbs
   *  back toward scam_suspect_raised, the corroboration check has stopped
   *  running rather than stopped finding anything — which is how it sat broken
   *  unnoticed once already. Only scam_dropped removes a listing. */
  scam_suspect_raised: number;
  scam_dropped: number;
  scam_verify_no_sentence: number;
  scam_verify_inconclusive: number;
  scam_shown_with_caution: number;
  /** Flags cleared with no check because the employer is a named, established
   *  agency — the traits the judge flags on are that trade's normal practice. */
  scam_known_agency_cleared: number;
  /** Licensed visa-sponsor filter; all zero unless the preference is on. */
  sponsor_filter_raw_before: number;
  sponsor_filter_raw_after: number;
  sponsor_filter_raw_blank_company: number;
  /** Confirmed below the candidate's sponsorship salary floor — never counts an
   *  unpriced listing. */
  sponsor_filter_below_salary_floor: number;
  sponsor_filter_scored_before: number;
  sponsor_filter_scored_after: number;
  sponsor_filter_scored_blank_company: number;
  /** Free, LLM-free drops made at pool admission, broken out by reason so the
   *  cost of filtering before any model sees a candidate stays attributable. */
  heuristic_prescreen_dropped: number;
  pool_quality_dropped: number;
  pool_quality_dropped_foreign_location: number;
  pool_quality_dropped_junk_listing: number;
  /** Demoted, never dropped, for being past a SOFT "Maximum listing age".
   *  Always 0 when that preference is Hard — those are dropped instead. */
  stale_soft_demoted: number;
  stale_soft_demoted_double: number;
  /** ATS vendor batch: off on an ordinary run, back on for a thin one (non-UK,
   *  sponsor-only, a repeat run the same day, or a first run). ats_pool_excluded
   *  counts already-stored ATS rows held out of the candidate pool, which is
   *  where the examine budget is actually freed. */
  ats_enabled: boolean;
  ats_pool_excluded: number;
  /** The examine budget this run used, and the reference run's selection ratio
   *  in per mille (0 = no usable reference, so the budget sat at its ceiling). */
  examine_budget_used: number;
  examine_budget_ratio_ref: number;
  shown: number;
  /** rank_scored — total examined by the cheap+mid gates. */
  examined: number;
  /** final_judge / examined — a coarse "how niche is this profile" gauge.
   *  null when nothing was examined yet. */
  filtering_ratio: number | null;
  token_usage: RunTokenStage[];
}

/** One LLM stage's token spend for a run. Mirrors backend RunTokenStageOut. */
export interface RunTokenStage {
  stage: string;
  calls: number;
  prompt_tokens: number;
  /** The part of prompt_tokens served from OpenAI's prompt cache. */
  cached_tokens: number;
  completion_tokens: number;
  /** cached/prompt — null when nothing was sent. */
  cache_hit_ratio: number | null;
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
  /** Judge-eligible candidates discarded for exceeding this cluster's share of
   *  RANK_TARGET_POOL. Tells a TRIMMED cluster from a STARVED one — both land on
   *  the same judge_eligible number. */
  judge_target_trimmed: number;
  stop_reason: string;
  judged: number;
  judge_reused_from_cache: number;
  judge_strong: number;
  judge_backup: number;
  judge_disqualified: number;
  picks: number;
  fallbacks: string[];
}

/** Today's live pipeline cap constants — mirrors backend RunCapsOut. Not
 *  per-run; shown under the per-cluster table so a "stopped because:
 *  absolute pool cap" row is checkable against the real number. */
export interface RunCaps {
  rank_examine_budget: number;
  rank_target_pool: number;
  judge_pool: number;
  judge_pool_floor: number;
  rank_reject_score_floor: number;
  target_pool_per_round: number;
  min_results_floor: number;
  final_picks: number;
}

/** Per-phase wall time for the last finished search run — mirrors backend
 *  RunTimingsOut. The search-side counterpart to CvParseTiming below. */
export interface RunTimings {
  run_id: number | null;
  finished_at?: string | null;
  total_seconds: number;
  phases: RunPhase[];
  clusters: RunCluster[];
  caps: RunCaps;
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
  max_listing_age: "hard",
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

/**
 * Mirrors routers/search.py::_VALID_APP_STATUS.
 *
 * `offer` exists so the terminal states aren't uniformly negative — a form whose
 * only outcomes are bad is a form nobody fills in, and this field went unused
 * past "pending" for its entire life before these two were added.
 *
 * `no_response` is deliberately NOT final: the other controls stay available so
 * a late reply can correct it. It is also a biased signal for ghost detection —
 * most applications get no reply for entirely ordinary reasons — so it is only
 * ever read as a rate across many rows, never as proof about one listing.
 */
export type ApplicationStatus =
  | "pending"
  | "interview"
  | "offer"
  | "rejected"
  | "no_response";

/** Ghost-listing risk. `null`/absent means NOTHING FIRED, not "unknown" — every
 *  backend rule fires on positive evidence only (see services/ghost.py). There
 *  is deliberately no "low": a value covering ~90% of rows would get rendered
 *  and train the reader to ignore the chip. */
export type GhostLevel = "high" | "medium";

/** Pay period — mirrors backend/app/services/salary.py's vocabulary. */
export type SalaryPeriod = "year" | "month" | "week" | "day" | "hour";

/** The final judge's fit grade — mirrors full_auto's _FINAL_EVAL_SCHEMA. */
export type RoleVerdict = "very_strong" | "strong" | "ok" | "stretch";

/**
 * Badge text per grade. "ok" and "stretch" deliberately have NO label: the badge
 * is the first thing read on a card, and "Ok fit"/"Stretch fit" told the
 * candidate to discount a role the judge had just verified as worth applying to
 * — while the card's own body (can_do_fit, the strengths/concerns pair) says the
 * same thing with the specifics attached. The grade still does its real job,
 * which is ordering the picks (engine._VERDICT_GRADES); it just isn't printed.
 *
 * Partial on purpose, so an unlabelled grade renders as no badge rather than as
 * a raw enum. RoleCard already guards on `VERDICT_LABEL[verdict]` being present.
 */
export const VERDICT_LABEL: Partial<Record<RoleVerdict, string>> = {
  very_strong: "Very strong fit",
  strong: "Strong fit",
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
  /** Readable stand-in for `location` when the source gave a raw postcode
   *  ("B706AW" -> "Sandwell"). Null when `location` needs no fixing. */
  location_label?: string | null;
  /** Straight-line miles from the candidate's stated place, from ONS postcode
   *  centroids. Null whenever either end couldn't be resolved — which is the
   *  common case, and must render as no chip rather than as 0. */
  distance_miles?: number | null;
  /** Whether the employer is on the Home Office licensed-sponsor register.
   *  THREE-STATE: true/false once checked, null when the listing named no
   *  employer to check — never render null as "not a sponsor". */
  sponsor_licensed?: boolean | null;
  /** What the LISTING says about sponsoring THIS role, in its own words —
   *  a different question from sponsor_licensed, which is about the employer's
   *  licence. Null means the listing was silent (the common case); it is never
   *  inferred from silence in either direction. */
  sponsor_statement?: "offered" | "not_offered" | null;
  /** The employer's own sentence behind sponsor_statement, so the card can show
   *  the words rather than ask the reader to trust a badge. */
  sponsor_statement_quote?: string | null;
  /** When this listing was last directly confirmed to still exist. Null on
   *  provisional cards (nothing has been checked yet) and on rows from runs
   *  that predate the check. */
  last_verified_at?: string | null;
  salary_text?: string | null;
  /** salary_text parsed into comparable numbers, in `salary_period`'s own units
   *  (NOT annualised). All null together when nothing parseable was stated, in
   *  which case salary_text is what gets shown. */
  salary_min?: number | null;
  salary_max?: number | null;
  salary_period?: SalaryPeriod | null;
  salary_currency?: string | null;
  /** True when salary_min/max is a modelled estimate (Adzuna's own
   *  salary_is_predicted) rather than a figure the employer/board stated --
   *  see lib/salary.ts's formatSalary, which labels it rather than showing it
   *  as fact. */
  salary_is_predicted?: boolean | null;
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
  /** When the employer posted/closes the listing, as stated by the source.
   *  Null when unknown -- never render that as "old", it just isn't claimed. */
  posted_at?: string | null;
  expires_at?: string | null;
  /** True when posted_at was aliased from a source's updated/modified field
   *  rather than a genuine "first posted" date (e.g. Greenhouse) -- render as
   *  "updated"/"~", never "posted", when this is set. */
  posted_at_approx?: boolean | null;
  /** Ghost-listing risk and the named rules behind it. Absent/null is the
   *  common case and means nothing fired. `ghost_signals` is decoded from JSON
   *  by the backend schema, so this is a real array. */
  ghost_level?: GhostLevel | null;
  ghost_signals?: string[] | null;
  status: RoleStatus;
  application_status?: ApplicationStatus | null;
  applied_at?: string | null;
  /** When the employer first responded, or the user declared no response.
   *  Stamped once, never overwritten. */
  response_at?: string | null;
  created_at: string;
  updated_at: string;
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

// ── Beta feedback ───────────────────────────────────────────────────────────

/** This user's wrap-up survey answers. `answered` is the whole gate. */
export interface ExitSurvey {
  answered: boolean;
  change: string;
  useful_features: string[];
  speed_tradeoff: string;
  created_at?: string | null;
}

export interface FeedbackPrompt {
  due: boolean;
  /** The run this answer would be about; null for the setup prompt, which fires
   *  at CV-parse time when no run exists yet. */
  run_id: number | null;
}

/** Which in-product prompts to show. Computed server-side — see
 *  backend routers/feedback.py for why the trigger rules don't live in the client. */
export interface FeedbackDue {
  results_quality: FeedbackPrompt;
  setup_ok: FeedbackPrompt;
}
