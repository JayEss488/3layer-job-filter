"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAttributes } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

/** First-run router: no profile data yet -> onboarding, otherwise -> search. */
export default function Home() {
  const router = useRouter();
  const { activeId, isLoading } = useProfiles();
  const { data: attrs, isLoading: attrsLoading } = useAttributes(activeId);

  useEffect(() => {
    if (isLoading || !activeId || attrsLoading || !attrs) return;
    router.replace(attrs.items.length === 0 ? "/onboarding" : "/search");
  }, [isLoading, activeId, attrsLoading, attrs, router]);

  return (
    <div className="screen">
      <div className="center-pad">
        <span className="spinner">◴</span> Loading…
      </div>
    </div>
  );
}
