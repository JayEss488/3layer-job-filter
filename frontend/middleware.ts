// Mounts the Auth0 SDK's own routes (/auth/login, /auth/logout, /auth/callback,
// /auth/profile, /auth/access-token, /auth/backchannel-logout). Next.js 14 uses
// `middleware.ts` at the project root (the `proxy.ts` rename is Next 16+).
//
// This only intercepts the /auth/* paths the SDK owns; every other route is a
// pure pass-through, so the app's real routing (providers.tsx, PUBLIC_ROUTES,
// etc.) is unaffected. See lib/auth0.ts for why this exists as a standalone
// trial rather than the app's real auth.
import { auth0 } from "./lib/auth0";
import type { NextRequest } from "next/server";

export async function middleware(request: NextRequest) {
  return auth0.middleware(request);
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt).*)",
  ],
};
