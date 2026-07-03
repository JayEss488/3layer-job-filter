"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { AttributeType } from "./types";

// ── Queries ─────────────────────────────────────────────────────────────────
export function useAttributes(profileId: number | null) {
  return useQuery({
    queryKey: ["attributes", profileId],
    queryFn: () => api.attributes(profileId!),
    enabled: !!profileId,
  });
}

export function useConfidence(profileId: number | null) {
  return useQuery({
    queryKey: ["confidence", profileId],
    queryFn: () => api.confidence(profileId!),
    enabled: !!profileId,
  });
}

export function useStats(profileId: number | null) {
  return useQuery({
    queryKey: ["stats", profileId],
    queryFn: () => api.stats(profileId!),
    enabled: !!profileId,
  });
}

export function useRoles(profileId: number | null, status?: string) {
  return useQuery({
    queryKey: ["roles", profileId, status ?? "all"],
    queryFn: () => api.roles(profileId!, status),
    enabled: !!profileId,
  });
}

/** Polls search status while a run is in progress. */
export function useSearchStatus(profileId: number | null) {
  return useQuery({
    queryKey: ["searchStatus", profileId],
    queryFn: () => api.searchStatus(profileId!),
    enabled: !!profileId,
    refetchInterval: (q) =>
      q.state.data?.status === "running" ? 2500 : false,
  });
}

// ── Attribute mutations ──────────────────────────────────────────────────────
export function useAttributeMutations(profileId: number) {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["attributes", profileId] });
    qc.invalidateQueries({ queryKey: ["confidence", profileId] });
  };

  const add = useMutation({
    mutationFn: (v: { type: AttributeType; value: string; source?: string }) =>
      api.addAttribute(profileId, { confirmed: true, ...v }),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: (v: { id: number; value?: string; confirmed?: boolean }) =>
      api.updateAttribute(v.id, { value: v.value, confirmed: v.confirmed }),
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteAttribute(id),
    onSuccess: invalidate,
  });
  const clearAll = useMutation({
    mutationFn: (ids: number[]) => Promise.all(ids.map((id) => api.deleteAttribute(id))),
    onSuccess: invalidate,
  });

  return { add, update, remove, clearAll, invalidate };
}
