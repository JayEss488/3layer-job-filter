// Server-side Auth0 client for the experimental /auth0-demo route.
//
// This is deliberately NOT wired into the app's real sign-in path. The
// existing custom auth (Google/Apple/email + JWT bearer, see lib/auth.ts and
// backend/app/routers/auth_router.py) still owns every real route and every
// API call. This file exists only so the Auth0 Next.js SDK has somewhere to
// mount its session logic for a standalone trial page.
import { Auth0Client } from "@auth0/nextjs-auth0/server";

export const auth0 = new Auth0Client();
