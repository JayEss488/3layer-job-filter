// Client-side auth state for the beta. The token is a signed Bearer string
// minted by POST /auth/google (or the legacy POST /login) and sent on every API
// request (see api.ts). localStorage is fine here: HTTPS, short-lived signed
// tokens, and the token is the ONLY secret it holds.

const TOKEN_KEY = "fiat.token";
const USER_KEY = "fiat.username";
const SURVEY_KEY = "fiat.needsSurvey";
const EXIT_SURVEY_KEY = "fiat.needsExitSurvey";
const EXPIRED_KEY = "fiat.betaExpired";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function getUsername(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(USER_KEY);
}

/**
 * Whether this account still owes us the two sign-up questions.
 *
 * A CACHE of the server's `needs_survey`, never the source of truth. The server
 * recomputes it on every /auth/google, /login and /me response, and the gate in
 * providers.tsx refreshes from /me on mount — so signing in on a second device,
 * or clearing storage, re-derives the right answer rather than silently skipping
 * the survey. It is cached at all only to avoid a visible flash of the app shell
 * while /me is in flight on the very first paint after sign-up.
 */
export function needsSurvey(): boolean {
  if (typeof window === "undefined") return false;
  return localStorage.getItem(SURVEY_KEY) === "1";
}

export function setNeedsSurvey(v: boolean): void {
  if (typeof window === "undefined") return;
  if (v) localStorage.setItem(SURVEY_KEY, "1");
  else localStorage.removeItem(SURVEY_KEY);
}

/**
 * Whether this account still owes us the wrap-up survey (asked from day 4 of
 * the beta window onwards, see backend services/beta.py).
 *
 * Same contract as needsSurvey above: a CACHE of the server's answer, refreshed
 * from /me on every mount, cached only to avoid a flash of the app shell before
 * the redirect lands. Independent of betaExpired — both can be true at once, for
 * a user who never logged in between day 4 and day 7.
 */
export function needsExitSurvey(): boolean {
  if (typeof window === "undefined") return false;
  return localStorage.getItem(EXIT_SURVEY_KEY) === "1";
}

export function setNeedsExitSurvey(v: boolean): void {
  if (typeof window === "undefined") return;
  if (v) localStorage.setItem(EXIT_SURVEY_KEY, "1");
  else localStorage.removeItem(EXIT_SURVEY_KEY);
}

/**
 * Whether this account's beta window has lapsed.
 *
 * Advisory only — the real enforcement is the server's 403 (require_active_beta),
 * which holds however stale this flag is. It exists so the app can route to
 * /exit-survey on first paint instead of firing a screenful of doomed queries
 * and bouncing off the first error.
 */
export function betaExpired(): boolean {
  if (typeof window === "undefined") return false;
  return localStorage.getItem(EXPIRED_KEY) === "1";
}

export function setBetaExpired(v: boolean): void {
  if (typeof window === "undefined") return;
  if (v) localStorage.setItem(EXPIRED_KEY, "1");
  else localStorage.removeItem(EXPIRED_KEY);
}

export function setAuth(
  token: string,
  username: string,
  needsSurveyFlag = false,
  needsExitSurveyFlag = false,
  expiredFlag = false
): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, username);
  setNeedsSurvey(needsSurveyFlag);
  setNeedsExitSurvey(needsExitSurveyFlag);
  setBetaExpired(expiredFlag);
}

/**
 * Where to send a user immediately after they sign in.
 *
 * Same precedence as the gate in providers.tsx, and it has to stay that way or
 * sign-in lands somewhere the gate then bounces them off. Sign-up survey first:
 * a brand-new account can be neither of the other two, and asking someone to
 * wrap up before they have started would be nonsense.
 */
export function postSignInRoute(r: {
  needs_survey?: boolean;
  needs_exit_survey?: boolean;
  beta_expired?: boolean;
}): string {
  if (r.needs_survey) return "/welcome";
  if (r.needs_exit_survey || r.beta_expired) return "/exit-survey";
  return "/start";
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(SURVEY_KEY);
  localStorage.removeItem(EXIT_SURVEY_KEY);
  localStorage.removeItem(EXPIRED_KEY);
}
