"use client";

import { createContext, useContext, useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { api } from "./api";
import type { Profile } from "./types";

interface ProfileCtx {
  profiles: Profile[];
  activeId: number | null;
  setActiveId: (id: number) => void;
  isLoading: boolean;
}

const Ctx = createContext<ProfileCtx | null>(null);
const STORAGE_KEY = "jobmatch.activeProfileId";

export function ProfileProvider({ children }: { children: React.ReactNode }) {
  const { data: profiles = [], isLoading } = useQuery({
    queryKey: ["profiles"],
    queryFn: api.listProfiles,
  });

  const [activeId, setActiveIdState] = useState<number | null>(null);

  // Pick a sensible active profile once profiles load.
  useEffect(() => {
    if (!profiles.length) return;
    const stored = Number(localStorage.getItem(STORAGE_KEY));
    const valid = profiles.find((p) => p.id === stored);
    setActiveIdState(valid ? valid.id : profiles[0].id);
  }, [profiles]);

  const setActiveId = (id: number) => {
    setActiveIdState(id);
    localStorage.setItem(STORAGE_KEY, String(id));
  };

  return (
    <Ctx.Provider value={{ profiles, activeId, setActiveId, isLoading }}>
      {children}
    </Ctx.Provider>
  );
}

export function useProfiles() {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useProfiles must be used within ProfileProvider");
  return ctx;
}
