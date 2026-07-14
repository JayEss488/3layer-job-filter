import type {
  Attribute,
  AttributesResponse,
  AttributeType,
  Blocklist,
  Confidence,
  Profile,
  Role,
  RunFunnel,
  ScrapeSetting,
  SearchStart,
  SearchStatus,
  SourceInfo,
  SourceStat,
  Stats,
} from "./types";

const BASE = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || detail;
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
    body: Partial<Pick<Profile, "name" | "is_active" | "intent_text">>
  ) =>
    req<Profile>(`/profiles/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  deleteProfile: (id: number) =>
    req<void>(`/profiles/${id}`, { method: "DELETE" }),
  stats: (id: number) => req<Stats>(`/profiles/${id}/stats`),

  // attributes
  attributes: (id: number) => req<AttributesResponse>(`/profiles/${id}/attributes`),
  addAttribute: (
    id: number,
    body: { type: AttributeType; value: string; source?: string; confirmed?: boolean; proficiency?: string }
  ) =>
    req<Attribute>(`/profiles/${id}/attributes`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateAttribute: (
    attrId: number,
    body: { value?: string; confirmed?: boolean; weight?: number; proficiency?: string }
  ) =>
    req<Attribute>(`/attributes/${attrId}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteAttribute: (attrId: number) =>
    req<void>(`/attributes/${attrId}`, { method: "DELETE" }),
  clearMemory: (id: number) =>
    req<void>(`/profiles/${id}/clear-memory`, { method: "POST" }),

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
    });
    if (!res.ok) throw new Error((await res.json()).detail || "Upload failed");
    return (await res.json()) as Attribute[];
  },
  suggest: (id: number, type: AttributeType, context?: string) =>
    req<{ suggestions: string[] }>(`/profiles/${id}/suggest`, {
      method: "POST",
      body: JSON.stringify({ type, context }),
    }),
  regenerateTargetRoles: (id: number) =>
    req<Attribute[]>(`/profiles/${id}/regenerate-target-roles`, { method: "POST" }),
  confidence: (id: number) => req<Confidence>(`/profiles/${id}/confidence`),

  // search + roles
  startSearch: (id: number) =>
    req<SearchStart>(`/profiles/${id}/search`, { method: "POST" }),
  cancelSearch: (id: number) =>
    req<SearchStatus>(`/profiles/${id}/search/cancel`, { method: "POST" }),
  searchStatus: (id: number) =>
    req<SearchStatus | null>(`/profiles/${id}/search/status`),
  roles: (id: number, status?: string) =>
    req<Role[]>(`/profiles/${id}/roles${status ? `?status=${status}` : ""}`),
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

  // settings
  sources: () => req<SourceInfo[]>("/settings/sources"),
  sourceStats: () => req<SourceStat[]>("/settings/source-stats"),
  runFunnel: () => req<RunFunnel>("/settings/run-funnel"),
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

  // ATS harvesting
  harvestAts: (id: number, force = false) =>
    req<{ status: string }>(`/profiles/${id}/harvest-ats?force=${force}`, {
      method: "POST",
    }),
};
