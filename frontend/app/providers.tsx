"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import {
  betaExpired,
  getToken,
  needsExitSurvey,
  needsSurvey,
  setBetaExpired,
  setNeedsExitSurvey,
  setNeedsSurvey,
} from "@/lib/auth";
import { ProfileProvider } from "@/lib/ProfileContext";

/**
 * Public routes: rendered without a token and WITHOUT ProfileProvider, so they
 * never fire authed queries.
 *
 * "/" is public because since self-serve sign-up the homepage is the landing +
 * sign-in page. It is also the app's entry point for a LOGGED-IN user, so it
 * renders both branches itself (see app/page.tsx) rather than being redirected
 * away from here — bouncing an authed user off "/" would mean they could never
 * see their own homepage.
 */
const PUBLIC_ROUTES = new Set(["/", "/login"]);

/** Where a logged-in user who still owes us the sign-up survey is held. */
const SURVEY_ROUTE = "/welcome";

/**
 * Where BOTH open-beta gates send the user, and why it is one route:
 *
 * - from day 4, until the wrap-up survey is answered (`needs_exit_survey`);
 * - from day 7, permanently (`beta_expired`).
 *
 * They are independent — a user who never logs in between day 4 and day 7 hits
 * both at once — so the page itself decides what to render from /me rather than
 * there being a separate "expired" screen. See backend services/beta.py.
 */
const EXIT_SURVEY_ROUTE = "/exit-survey";

/** Routes that render outside ProfileProvider: authed, but with no profile to fetch. */
const NO_PROFILE_ROUTES = new Set([SURVEY_ROUTE, EXIT_SURVEY_ROUTE]);

/**
 * Gates the app behind a login token, the two sign-up questions, and the beta window.
 *
 * The survey gates live here, in the client, and NOT as a server-side rejection.
 * A backend that 403'd every request until the survey was answered would also
 * reject the survey submission itself, and would surface as an auth failure that
 * api.ts's 401 handling would misread as a logged-out session — turning
 * optional-in-spirit questions into a lockout. So: the server reports
 * `needs_survey` / `needs_exit_survey`, this gate routes on them, and a
 * determined user with devtools can skip them. That trade is deliberate; GET
 * /admin/signups reports unanswered rows precisely so skipping stays visible.
 *
 * `beta_expired` is the one that IS enforced server-side (a real 403 on every
 * data router). Routing on it here is purely so an expired user lands on the
 * survey immediately, instead of firing a screenful of doomed queries and
 * bouncing off whichever one failed first.
 */
function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [ready, setReady] = useState(false);

  const isPublic = PUBLIC_ROUTES.has(pathname);

  useEffect(() => {
    if (isPublic) {
      setReady(true);
      return;
    }
    if (!getToken()) {
      router.replace("/");
      return;
    }
    // Cached flags first so a fresh sign-up (or an expired session) goes
    // straight to the right page with no flash of the app shell while /me is in
    // flight. Sign-up survey first: a brand-new account can be neither of the
    // other two, and asking someone to wrap up before they have started would
    // be nonsense.
    if (needsSurvey() && pathname !== SURVEY_ROUTE) {
      router.replace(SURVEY_ROUTE);
      return;
    }
    if ((needsExitSurvey() || betaExpired()) && pathname !== EXIT_SURVEY_ROUTE) {
      router.replace(EXIT_SURVEY_ROUTE);
      return;
    }
    setReady(true);
  }, [isPublic, pathname, router]);

  // Re-derive the gate flags from the server once per mount. This is what makes
  // the cache safe: a second device (or cleared storage) has no cached flag, and
  // without this refresh such a user would silently skip the survey forever — or,
  // for the beta window, would see the app shell until the first 403 landed.
  // Failures are ignored — /me 401s already bounce through handleUnauthorized,
  // and any other error must not block someone from reaching the app.
  useEffect(() => {
    if (isPublic || !getToken()) return;
    let cancelled = false;
    api
      .me()
      .then((m) => {
        if (cancelled) return;
        setNeedsSurvey(m.needs_survey);
        setNeedsExitSurvey(m.needs_exit_survey);
        setBetaExpired(m.beta_expired);
        if (m.needs_survey) {
          if (pathname !== SURVEY_ROUTE) router.replace(SURVEY_ROUTE);
        } else if (
          (m.needs_exit_survey || m.beta_expired) &&
          pathname !== EXIT_SURVEY_ROUTE
        ) {
          router.replace(EXIT_SURVEY_ROUTE);
        }
      })
      .catch(() => {
        /* non-blocking by design */
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isPublic]);

  if (isPublic) return <>{children}</>;

  if (!ready) {
    return (
      <div className="screen">
        <div className="center-pad">
          <span className="spinner">◴</span> Loading…
        </div>
      </div>
    );
  }

  // /welcome and /exit-survey are authed but need no profile: /welcome is
  // pre-profile, and an expired user's profile queries would 403 anyway. Keeping
  // them outside ProfileProvider means neither survey can stall behind — or be
  // blocked by — a profile query.
  if (NO_PROFILE_ROUTES.has(pathname)) return <>{children}</>;

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
