import type {
  Attribute,
  AttributesResponse,
  AttributeType,
  Blocklist,
  ContextHeader,
  CvParseTiming,
  Enforcement,
  ExitSurvey,
  FamilyTier,
  FeedbackDue,
  Profile,
  Role,
  RoleFamily,
  RunFunnel,
  RunTimings,
  ScrapeSetting,
  SearchStart,
  SearchStatus,
  Snapshot,
  SourceInfo,
  SourceStat,
  Stats,
} from "./types";

import { clearAuth, getToken, setBetaExpired } from "./auth";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

/** Bearer header for the current token, or {} when logged out. */
function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

/**
 * On an expired/invalid token, drop it and bounce to the homepage.
 *
 * "/" rather than "/login": since self-serve sign-up, the homepage IS the
 * sign-in page (it renders the landing content plus the Google button when
 * logged out). /login still exists for the original hand-assigned beta
 * credentials, but sending an expired Google session there would show a
 * username/password form the user has never had and cannot use.
 */
export function handleUnauthorized(): void {
  clearAuth();
  if (typeof window !== "undefined" && window.location.pathname !== "/" && window.location.pathname !== "/login") {
    window.location.href = "/";
  }
}

/** Where a user whose beta window has lapsed is held. Also hosts the wrap-up survey. */
export const EXIT_SURVEY_PATH = "/exit-survey";

/**
 * Latch: this page load has already started bouncing to the survey.
 *
 * The `pathname` check below cannot stand in for it. An expired user landing on
 * an app route can fail several queries at once (/search fires roles, stats,
 * profiles and search-status together, each retried once), and navigation has
 * not landed while those resolve — so pathname is still the old route for every
 * one of them and each would assign `location.href` again. In practice the
 * first failure usually unloads the page before the rest resolve, but that is a
 * timing accident, not a guarantee. Redirecting is a one-time decision per page
 * load, so make it one.
 */
let betaExpiryHandled = false;

/**
 * On a lapsed beta window, route to the wrap-up survey.
 *
 * Deliberately does NOT clearAuth(): the user has to stay signed in to answer
 * the survey, which is the whole reason the backend keeps /me and /exit/survey
 * reachable while every data route 403s. Signing them out here would lock the
 * survey behind a login they can no longer complete.
 */
export function handleBetaExpired(): void {
  setBetaExpired(true);
  if (betaExpiryHandled || typeof window === "undefined") return;
  if (window.location.pathname !== EXIT_SURVEY_PATH) {
    betaExpiryHandled = true;
    window.location.href = EXIT_SURVEY_PATH;
  }
}

/**
 * Pull the message out of a FastAPI error body, and the machine-readable code
 * if there is one.
 *
 * `detail` is usually a plain string. The beta-expiry 403 sends a dict instead
 * (`{code, message}`) so the client can tell it apart from an ordinary 403 —
 * routing an expired user to the survey rather than showing them a raw error
 * needs that distinction, and a code in the body avoids a custom response header
 * (which would also need adding to the backend's CORS expose_headers).
 * Rendering the dict would print "[object Object]" at the user.
 */
function parseErrorBody(body: unknown, fallback: string): { message: string; code: string } {
  const detail = (body as { detail?: unknown } | null)?.detail;
  if (typeof detail === "string" && detail) return { message: detail, code: "" };
  if (detail && typeof detail === "object") {
    const d = detail as { code?: string; message?: string };
    return { message: d.message || fallback, code: d.code || "" };
  }
  return { message: fallback, code: "" };
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init?.headers as Record<string, string> | undefined),
    },
  });
  if (!res.ok) {
    if (res.status === 401) handleUnauthorized();
    let detail = res.statusText;
    let code = "";
    try {
      ({ message: detail, code } = parseErrorBody(await res.json(), res.statusText));
    } catch {
      /* ignore */
    }
    if (res.status === 403 && code === "beta_expired") handleBetaExpired();
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

/**
 * The open-beta window fields every identity response carries.
 *
 * All null/false for an account with no window (the original beta testers, who
 * are exempt) — so nothing here needs a special case for them.
 */
export interface BetaWindow {
  beta_expires_at: string | null;
  beta_days_left: number | null;
  /** Advisory; the real lapse is the server's 403. */
  beta_expired: boolean;
  /** Day 4 onwards: hold on /exit-survey until answered. Independent of expiry. */
  needs_exit_survey: boolean;
}

/** Shape of every sign-in response — the three self-serve paths and /login. */
export interface AuthResult extends BetaWindow {
  token: string;
  user_id: number;
  username: string;
  /** Hold the user on /welcome until they answer the two sign-up questions. */
  needs_survey: boolean;
  email: string;
  display_name: string;
  /** True only on an account's very first sign-in. */
  is_new: boolean;
}

/** GET /auth/config — which sign-in methods this deployment actually supports.
 *  A provider with no credential configured is reported disabled rather than
 *  rendered as a button that 503s. */
export interface AuthConfig {
  google_enabled: boolean;
  google_client_id: string;
  apple_enabled: boolean;
  /** Apple *Services ID*, not the App ID — what AppleID.js wants as client_id. */
  apple_client_id: string;
  email_enabled: boolean;
}

/** Posts to a public (token-less) endpoint, surfacing the server's own message. */
async function publicPost<T>(path: string, body: unknown, fallback: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res
      .json()
      .then((b) => b.detail)
      .catch(() => null);
    throw new Error(detail || fallback);
  }
  return (await res.json()) as T;
}

export const api = {
  // ── auth ───────────────────────────────────────────────────────────────
  /** Which sign-in methods this deployment supports; drives which of the three
   *  sign-up controls the homepage renders at all. Public, no token.
   *
   *  Read at run time from the server rather than from NEXT_PUBLIC_ env vars,
   *  which are baked in at BUILD time — a frontend built before a provider's
   *  credential existed would otherwise render a permanently broken button
   *  against a perfectly configured server. */
  authConfig: async () => {
    const res = await fetch(`${BASE}/auth/config`);
    if (!res.ok) throw new Error("Could not reach the server");
    return (await res.json()) as AuthConfig;
  },

  /** Self-serve sign-up AND sign-in — one call, because the browser can't know
   *  which it is and making the user choose only produces wrong answers.
   *  `credential` is the signed ID token from Google Identity Services. */
  googleAuth: (credential: string) =>
    publicPost<AuthResult>("/auth/google", { credential }, "Google sign-in failed"),

  /** Same one-call shape as Google. `name` is separate because Apple returns it
   *  exactly once, in the authorization response on first sign-up, and never in
   *  the token — the server treats it as display-only for that reason. */
  appleAuth: (credential: string, name = "") =>
    publicPost<AuthResult>("/auth/apple", { credential, name }, "Apple sign-in failed"),

  /** Email + password. Unlike the provider buttons these are TWO calls: the
   *  user knows whether they have registered before, and merging them would let
   *  a mistyped password silently create a second, empty account. */
  emailRegister: (email: string, password: string) =>
    publicPost<AuthResult>("/auth/email/register", { email, password }, "Sign-up failed"),
  emailLogin: (email: string, password: string) =>
    publicPost<AuthResult>("/auth/email/login", { email, password }, "Sign-in failed"),

  /** Legacy hand-assigned beta credentials. Kept so the first cohort isn't
   *  locked out; not linked from the homepage's main flow. */
  login: (username: string, password: string) =>
    publicPost<AuthResult>("/login", { username, password }, "Login failed"),

  me: () =>
    req<
      BetaWindow & {
        user_id: number;
        username: string;
        needs_survey: boolean;
        email: string;
        display_name: string;
      }
    >("/me"),

  /** This user's sign-up survey answers, or null if unanswered. */
  getSignupSurvey: () =>
    req<{ priority: string; used_ai_tool: boolean; created_at: string } | null>("/signup/survey"),

  submitSignupSurvey: (priority: string, used_ai_tool: boolean) =>
    req<{ priority: string; used_ai_tool: boolean }>("/signup/survey", {
      method: "POST",
      body: JSON.stringify({ priority, used_ai_tool }),
    }),

  // ── beta wrap-up survey ────────────────────────────────────────────────
  /** This user's wrap-up answers; `answered: false` when they haven't yet. */
  getExitSurvey: () => req<ExitSurvey>("/exit/survey"),

  submitExitSurvey: (body: {
    change: string;
    useful_features: string[];
    speed_tradeoff: string;
  }) => req<ExitSurvey>("/exit/survey", { method: "POST", body: JSON.stringify(body) }),

  // ── in-product feedback prompts ────────────────────────────────────────
  /** Which prompts to show right now. Server-side so a user who answered on one
   *  device isn't asked again on another. */
  feedbackDue: (profileId: number) =>
    req<FeedbackDue>(`/feedback/due?profile_id=${profileId}`),

  submitFeedback: (body: {
    question_id: string;
    answer: string;
    profile_id?: number;
    run_id?: number | null;
  }) => req<void>("/feedback", { method: "POST", body: JSON.stringify(body) }),

  // profiles
  listProfiles: () => req<Profile[]>("/profiles"),
  createProfile: (name?: string) =>
    req<Profile>("/profiles", { method: "POST", body: JSON.stringify({ name }) }),
  updateProfile: (
    id: number,
    body: Partial<Pick<Profile, "name" | "is_active" | "intent_text" | "search_feedback">> & {
      cv_summary?: string;
    }
  ) =>
    req<Profile>(`/profiles/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteProfile: (id: number) =>
    req<void>(`/profiles/${id}`, { method: "DELETE" }),
  stats: (id: number) => req<Stats>(`/profiles/${id}/stats`),
  interpretFeedback: (id: number) =>
    req<Attribute[]>(`/profiles/${id}/interpret-feedback`, { method: "POST" }),

  // attributes
  attributes: (id: number) => req<AttributesResponse>(`/profiles/${id}/attributes`),
  addAttribute: (
    id: number,
    body: {
      type: AttributeType;
      value: string;
      source?: string;
      confirmed?: boolean;
      proficiency?: string;
      evidence_origin?: string;
      family_id?: number;
      pinned?: boolean;
      enforcement?: Enforcement;
    }
  ) =>
    req<Attribute>(`/profiles/${id}/attributes`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateAttribute: (
    attrId: number,
    body: {
      value?: string;
      confirmed?: boolean;
      weight?: number;
      proficiency?: string;
      evidence_origin?: string;
      family_id?: number;
      pinned?: boolean;
      enforcement?: Enforcement;
    }
  ) =>
    req<Attribute>(`/attributes/${attrId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteAttribute: (attrId: number) =>
    req<void>(`/attributes/${attrId}`, { method: "DELETE" }),
  clearMemory: (id: number) =>
    req<void>(`/profiles/${id}/clear-memory`, { method: "POST" }),

  // role families (the engine's per-stream clusters)
  families: (id: number) => req<RoleFamily[]>(`/profiles/${id}/families`),
  addFamily: (id: number, body: { name: string; tier?: FamilyTier }) =>
    req<RoleFamily>(`/profiles/${id}/families`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateFamily: (
    familyId: number,
    body: { name?: string; tier?: FamilyTier; position?: number }
  ) =>
    req<RoleFamily>(`/families/${familyId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteFamily: (familyId: number) =>
    req<void>(`/families/${familyId}`, { method: "DELETE" }),
  regenerateFamily: (familyId: number) =>
    req<Attribute[]>(`/families/${familyId}/regenerate`, { method: "POST" }),

  // onboarding
  parseText: (id: number, text: string) =>
    req<Attribute[]>(`/profiles/${id}/parse-text`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  parseCv: async (id: number, file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/profiles/${id}/parse-cv`, {
      method: "POST",
      body: form,
      headers: authHeaders(),
    });
    if (!res.ok) {
      if (res.status === 401) handleUnauthorized();
      throw new Error((await res.json()).detail || "Upload failed");
    }
    return (await res.json()) as Attribute[];
  },
  suggest: (id: number, type: AttributeType, context?: string) =>
    req<{ suggestions: string[] }>(`/profiles/${id}/suggest`, {
      method: "POST",
      body: JSON.stringify({ type, context }),
    }),
  regenerateTargetRoles: (id: number) =>
    req<Attribute[]>(`/profiles/${id}/regenerate-target-roles`, { method: "POST" }),
  submitComment: (id: number, text: string) =>
    req<{ status: string }>(`/profiles/${id}/comment`, {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  contextHeader: (id: number) => req<ContextHeader>(`/profiles/${id}/context-header`),
  updateContextHeader: (id: number, header: string) =>
    req<ContextHeader>(`/profiles/${id}/context-header`, {
      method: "PATCH",
      body: JSON.stringify({ header }),
    }),

  // search + roles
  startSearch: (id: number) =>
    req<SearchStart>(`/profiles/${id}/search`, { method: "POST" }),
  cancelSearch: (id: number) =>
    req<SearchStatus>(`/profiles/${id}/search/cancel`, { method: "POST" }),
  searchStatus: (id: number) =>
    req<SearchStatus | null>(`/profiles/${id}/search/status`),
  roles: (id: number, status?: string, includeProvisional?: boolean) => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (includeProvisional) params.set("include_provisional", "true");
    const qs = params.toString();
    return req<Role[]>(`/profiles/${id}/roles${qs ? `?${qs}` : ""}`);
  },
  tick: (roleId: number) => req<Role>(`/roles/${roleId}/tick`, { method: "POST" }),
  cross: (roleId: number) => req<Role>(`/roles/${roleId}/cross`, { method: "POST" }),
  save: (roleId: number) => req<Role>(`/roles/${roleId}/save`, { method: "POST" }),
  apply: (roleId: number) => req<Role>(`/roles/${roleId}/apply`, { method: "POST" }),
  moveToIgnored: (roleId: number) =>
    req<Role>(`/roles/${roleId}/move-to-ignored`, { method: "POST" }),
  setApplicationStatus: (roleId: number, application_status: string) =>
    req<Role>(`/roles/${roleId}/application-status`, {
      method: "PATCH",
      body: JSON.stringify({ application_status }),
    }),
  deleteRole: (roleId: number) =>
    req<void>(`/roles/${roleId}`, { method: "DELETE" }),
  clearAllRoles: (profileId: number) =>
    req<{ cleared: number }>(`/profiles/${profileId}/roles/clear-all`, { method: "POST" }),

  // settings
  sources: () => req<SourceInfo[]>("/settings/sources"),
  sourceStats: () => req<SourceStat[]>("/settings/source-stats"),
  runFunnel: (profileId: number) =>
    req<RunFunnel>(`/settings/run-funnel?profile_id=${profileId}`),
  runTimings: (profileId: number) =>
    req<RunTimings>(`/settings/run-timings?profile_id=${profileId}`),
  snapshot: (profileId: number) =>
    req<Snapshot>(`/settings/snapshot?profile_id=${profileId}`),
  setSources: (disabled: string[]) =>
    req<SourceInfo[]>("/settings/sources", {
      method: "PUT",
      body: JSON.stringify({ disabled }),
    }),
  scrapeSetting: () => req<ScrapeSetting>("/settings/scrape"),
  setScrapeSetting: (enabled: boolean) =>
    req<ScrapeSetting>("/settings/scrape", {
      method: "PUT",
      body: JSON.stringify({ enabled }),
    }),
  blocklist: () => req<Blocklist>("/settings/blocklist"),
  setBlocklist: (domains: string[]) =>
    req<Blocklist>("/settings/blocklist", {
      method: "PUT",
      body: JSON.stringify({ domains }),
    }),
  cvParseTiming: () => req<CvParseTiming>("/settings/cv-parse-timing"),
  runCvParseTiming: async (file: File) => {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${BASE}/settings/cv-parse-timing`, {
      method: "POST",
      body: form,
      headers: authHeaders(),
    });
    if (!res.ok) {
      if (res.status === 401) handleUnauthorized();
      throw new Error((await res.json()).detail || "Timing run failed");
    }
    return (await res.json()) as CvParseTiming;
  },

  // ATS harvesting
  harvestAts: (id: number, force = false) =>
    req<{ status: string }>(`/profiles/${id}/harvest-ats?force=${force}`, {
      method: "POST",
    }),
};
