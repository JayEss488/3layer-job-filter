"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api";
import { clearAuth, getUsername } from "@/lib/auth";
import { useSearchStatus } from "@/lib/hooks";
import { useProfiles } from "@/lib/ProfileContext";

// Profile = what the candidate WANTS (role families, requirements, preferences).
// Memory = who they ARE (past roles, qualifications, skills, CV context). The
// split is the whole organising idea of both pages -- see app/memory/page.tsx.
const TABS = [
  { href: "/search", label: "Search" },
  { href: "/my-roles", label: "My Roles" },
  { href: "/dashboard", label: "Profile" },
  { href: "/memory", label: "Memory" },
  { href: "/settings", label: "Analytics" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const qc = useQueryClient();
  const { activeId } = useProfiles();
  const [starting, setStarting] = useState(false);
  // Reflects the actual backend state for the active profile, not just this
  // button's own click state -- so a search kicked off from another page
  // (dashboard/onboarding), or one still running after this component
  // remounts, correctly disables the button here too.
  const { data: status } = useSearchStatus(activeId);
  const searchInFlight = status?.status === "running";
  const running = starting || searchInFlight;

  async function runSearch() {
    if (!activeId || running) return;
    setStarting(true);
    try {
      await api.startSearch(activeId);
      qc.invalidateQueries({ queryKey: ["searchStatus", activeId] });
      router.push("/search");
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setStarting(false);
    }
  }

  function logout() {
    clearAuth();
    qc.clear();
    // "/" (the homepage + Google sign-in), not "/login". Since self-serve
    // sign-up, /login only serves the original hand-assigned beta credentials --
    // dropping a Google user there shows them a username/password form they have
    // never had and cannot use.
    router.replace("/");
  }

  const username = getUsername();

  return (
    <div className="nav">
      <div className="nav-left">
        <div className="logo">4</div>
        <div className="tabs">
          {TABS.map((t) => (
            <Link
              key={t.href}
              href={t.href}
              className={`tab${pathname.startsWith(t.href) ? " active" : ""}`}
            >
              {t.label}
            </Link>
          ))}
        </div>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <button className="btn btn-primary" onClick={runSearch} disabled={running || !activeId}>
          {starting ? "Starting…" : searchInFlight ? "Search running…" : "▶ Run New Search"}
        </button>
        {username && (
          <span style={{ fontSize: 12, color: "var(--muted, #888)" }}>{username}</span>
        )}
        <button className="btn btn-secondary" onClick={logout} title="Log out">
          Log out
        </button>
      </div>
    </div>
  );
}
