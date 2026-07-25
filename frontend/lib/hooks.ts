"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "./api";
import type { AttributeType, Enforcement, FamilyTier } from "./types";

// ── Queries ─────────────────────────────────────────────────────────────────
export function useAttributes(profileId: number | null) {
  return useQuery({
    queryKey: ["attributes", profileId],
    queryFn: () => api.attributes(profileId!),
    enabled: !!profileId,
  });
}

export function useFamilies(profileId: number | null) {
  return useQuery({
    queryKey: ["families", profileId],
    queryFn: () => api.families(profileId!),
    enabled: !!profileId,
  });
}

export function useContextHeader(profileId: number | null) {
  return useQuery({
    queryKey: ["contextHeader", profileId],
    queryFn: () => api.contextHeader(profileId!),
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

export function useRoles(
  profileId: number | null,
  status?: string,
  opts?: { includeProvisional?: boolean; refetchInterval?: number | false },
) {
  return useQuery({
    queryKey: ["roles", profileId, status ?? "all", opts?.includeProvisional ?? false],
    queryFn: () => api.roles(profileId!, status, opts?.includeProvisional),
    enabled: !!profileId,
    refetchInterval: opts?.refetchInterval ?? false,
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
  };

  const add = useMutation({
    mutationFn: (v: {
      type: AttributeType;
      value: string;
      source?: string;
      proficiency?: string;
      evidence_origin?: string;
      family_id?: number;
      pinned?: boolean;
      enforcement?: Enforcement;
    }) => api.addAttribute(profileId, { confirmed: true, ...v }),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: (v: {
      id: number;
      value?: string;
      confirmed?: boolean;
      proficiency?: string;
      evidence_origin?: string;
      family_id?: number;
      pinned?: boolean;
      enforcement?: Enforcement;
    }) => {
      const { id, ...body } = v;
      return api.updateAttribute(id, body);
    },
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteAttribute(id),
    onSuccess: invalidate,
  });

  return { add, update, remove, invalidate };
}

// ── Role-family mutations ────────────────────────────────────────────────────
export function useFamilyMutations(profileId: number) {
  const qc = useQueryClient();
  // Deleting a family deletes its target roles too (see routers/families.py),
  // and adding one can change how attributes group — so both caches refresh.
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["families", profileId] });
    qc.invalidateQueries({ queryKey: ["attributes", profileId] });
  };

  const add = useMutation({
    mutationFn: (v: { name: string; tier?: FamilyTier }) => api.addFamily(profileId, v),
    onSuccess: invalidate,
  });
  const update = useMutation({
    mutationFn: (v: { id: number; name?: string; tier?: FamilyTier; position?: number }) => {
      const { id, ...body } = v;
      return api.updateFamily(id, body);
    },
    onSuccess: invalidate,
  });
  const remove = useMutation({
    mutationFn: (id: number) => api.deleteFamily(id),
    onSuccess: invalidate,
  });
  const regenerate = useMutation({
    mutationFn: (id: number) => api.regenerateFamily(id),
    onSuccess: invalidate,
  });

  return { add, update, remove, regenerate, invalidate };
}
