"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";

import { ProfileProvider } from "@/lib/ProfileContext";

/**
 * There is no auth gate. This app runs as a single local user against your own
 * API keys, so every route is simply the app (see backend/app/main.py for why an
 * account system would protect nothing here).
 *
 * The wrapping div matters: body is `display:flex` (row) to center a single
 * .app/.screen card via justify-content, so children of body are laid out side
 * by side rather than stacked. See .app-shell in globals.css.
 */
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: { queries: { refetchOnWindowFocus: false, retry: 1 } },
      })
  );
  return (
    <QueryClientProvider client={client}>
      <ProfileProvider>
        <div className="app-shell">{children}</div>
      </ProfileProvider>
    </QueryClientProvider>
  );
}
