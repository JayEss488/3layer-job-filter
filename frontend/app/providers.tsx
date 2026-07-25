"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { getToken } from "@/lib/auth";
import { ProfileProvider } from "@/lib/ProfileContext";

/**
 * Gates the app behind a login token. The /login route renders on its own (no
 * ProfileProvider, so it never fires authed queries). Every other route requires
 * a token: without one we redirect to /login and render a loader in the meantime,
 * so protected pages never mount their data queries while logged out.
 */
function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    if (pathname === "/login") {
      setReady(true);
      return;
    }
    if (!getToken()) {
      router.replace("/login");
      return;
    }
    setReady(true);
  }, [pathname, router]);

  if (pathname === "/login") return <>{children}</>;

  if (!ready) {
    return (
      <div className="screen">
        <div className="center-pad">
          <span className="spinner">◴</span> Loading…
        </div>
      </div>
    );
  }

  return <ProfileProvider>{children}</ProfileProvider>;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
      })
  );
  return (
    <QueryClientProvider client={client}>
      <AuthGate>{children}</AuthGate>
    </QueryClientProvider>
  );
}
