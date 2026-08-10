"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { useAttributes } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

/**
 * First-run router for a signed-in user: no profile data yet -> onboarding,
 * otherwise -> search.
 *
 * This used to be "/" itself. It moved when the homepage became the public
 * landing + sign-in page: "/" is now rendered OUTSIDE ProfileProvider (see
 * app/providers.tsx), and this decision needs `useProfiles`/`useAttributes`,
 * which only exist inside it. So the homepage sends an authed visitor here and
 * this page makes the same call it always did.
 */
export default function Start() {
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
