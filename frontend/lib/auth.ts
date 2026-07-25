// Client-side auth token storage for the closed beta. The token is a signed
// Bearer string minted by POST /login and sent on every API request (see api.ts).
// localStorage is fine here: closed beta, HTTPS, short-lived signed tokens.

const TOKEN_KEY = "fiat.token";
const USER_KEY = "fiat.username";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(TOKEN_KEY);
}

export function getUsername(): string | null {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(USER_KEY);
}

export function setAuth(token: string, username: string): void {
  if (typeof window === "undefined") return;
  localStorage.setItem(TOKEN_KEY, token);
  localStorage.setItem(USER_KEY, username);
}

export function clearAuth(): void {
  if (typeof window === "undefined") return;
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}
