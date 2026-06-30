"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "@/lib/api";
import { useProfiles } from "@/lib/ProfileContext";

const TABS = [
  { href: "/search", label: "Search" },
  { href: "/my-roles", label: "My Roles" },
  { href: "/dashboard", label: "Profile" },
];

export function Nav() {
  const pathname = usePathname();
  const router = useRouter();
  const qc = useQueryClient();
  const { activeId } = useProfiles();
  const [running, setRunning] = useState(false);

  async function runSearch() {
    if (!activeId || running) return;
    setRunning(true);
    try {
      await api.startSearch(activeId);
      qc.invalidateQueries({ queryKey: ["searchStatus", activeId] });
      router.push("/search");
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="nav">
      {TABS.map((t) => (
        <Link
          key={t.href}
          href={t.href}
          className={`nav-tab${pathname.startsWith(t.href) ? " active" : ""}`}
        >
          {t.label}
        </Link>
      ))}
      <div className="nav-spacer" />
      <button className="nav-btn" onClick={runSearch} disabled={running || !activeId}>
        {running ? "Starting…" : "▶ Run New Search"}
      </button>
    </div>
  );
}
