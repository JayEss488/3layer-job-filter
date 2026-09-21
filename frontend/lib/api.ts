import type {
  Attribute,
  AttributesResponse,
  AttributeType,
  Blocklist,
  ContextHeader,
  CvParseTiming,
  Enforcement,
  FamilyTier,
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

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

/**
 * Pull the message out of a FastAPI error body, and the machine-readable code
 * if there is one.
 *
 * `detail` is usually a plain string, but FastAPI allows a dict. Rendering the
 * dict would print "[object Object]" at the user, so both shapes are handled.
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

/**
 * Client-side ceiling on a CV/notes parse.
 *
 * The server has its own budget (config.CV_PARSE_TIMEOUT_SECONDS), and this
 * sits deliberately above it so a server-side timeout wins and the user gets
 * the specific message rather than a generic one. What this catches is the
 * case the server's budget cannot: a stall in the NETWORK rather than in the
 * request — a dropped connection, a sleeping laptop, a proxy holding the socket
 * open — where the response never arrives and `fetch` never settles. Before
 * this, that ended as a spinner with no timeout anywhere in the stack, which is
 * exactly how a parse was reported "not finishing" with nothing in the console.
 */
const PARSE_TIMEOUT_MS = 150_000;

function parseAbortSignal(): AbortSignal {
  return AbortSignal.timeout(PARSE_TIMEOUT_MS);
}

/** Turn the abort into something a user can act on; pass anything else through. */
function asParseTimeout(e: unknown): Error {
  const name = (e as { name?: string } | null)?.name;
  if (name === "TimeoutError" || name === "AbortError") {
    return new Error(
      "That took too long and was stopped. Nothing was changed — please try again.",
    );
  }
  return e instanceof Error ? e : new Error(String(e));
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers as Record<string, string> | undefined),
      },
    });
  } catch (e) {
    // Only reachable for a caller that passed a signal (today: the parse
    // calls); everything else rethrows unchanged, so this adds no behaviour
    // anywhere it isn't asked for.
    throw asParseTimeout(e);
  }
  if (!res.ok) {
    let detail = res.statusText;
    try {
      ({ message: detail } = parseErrorBody(await res.json(), res.statusText));
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
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
      signal: parseAbortSignal(),
    }),
  parseCv: async (id: number, file: File) => {
    const form = new FormData();
    form.append("file", file);
    try {
      const res = await fetch(`${BASE}/profiles/${id}/parse-cv`, {
        method: "POST",
        body: form,
        signal: parseAbortSignal(),
      });
      if (!res.ok) {
        throw new Error((await res.json()).detail || "Upload failed");
      }
      return (await res.json()) as Attribute[];
    } catch (e) {
      throw asParseTimeout(e);
    }
  },
  suggest: (id: number, type: AttributeType, context?: string) =>
    req<{ suggestions: string[] }>(`/profiles/${id}/suggest`, {
      method: "POST",
      body: JSON.stringify({ type, context }),
    }),
  regenerateTargetRoles: (id: number) =>
    req<Attribute[]>(`/profiles/${id}/regenerate-target-roles`, { method: "POST" }),
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
    });
    if (!res.ok) {
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
