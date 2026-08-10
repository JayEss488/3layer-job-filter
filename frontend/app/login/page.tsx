"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { postSignInRoute, setAuth } from "@/lib/auth";

/**
 * The ORIGINAL closed-beta sign-in: a hand-assigned username and password
 * (scripts/gen_beta_users.py).
 *
 * This is no longer the way in. Since self-serve sign-up the homepage ("/") is
 * the landing page and the Google button, and everything that used to redirect
 * here now redirects there. This page survives for exactly one reason: the
 * first cohort of beta testers hold credentials that have no Google account
 * attached, and deleting the form would lock them out of their own profiles,
 * saved roles and search history. It is linked only from the homepage footer.
 *
 * A Google account can never sign in here whatever password is typed — those
 * rows store an empty password hash, which the backend's verify_password can't
 * match (see backend/app/services/auth.py).
 */
export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!username.trim() || !password) return;
    setBusy(true);
    setError("");
    try {
      const r = await api.login(username.trim(), password);
      setAuth(r.token, r.username, r.needs_survey, r.needs_exit_survey, r.beta_expired);
      router.replace(postSignInRoute(r));
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="lp lp-plain">
      <div className="lp-sheet lp-narrow">
        <header className="lp-header">
          <div className="lp-brand">
            <div className="lp-mark">4</div>
            <div className="lp-brandname">Four in a Thousand</div>
          </div>
        </header>

        <div className="lp-legacy">
          <form onSubmit={submit} className="lp-legacy-form">
            <div>
              <div className="lp-legacy-h">Sign in with a beta username</div>
              <div className="lp-legacy-sub">
                For testers who were given credentials before self-serve sign-up existed. Everyone
                else should{" "}
                <Link href="/" className="lp-inline-link">
                  sign in with Google
                </Link>
                .
              </div>
            </div>

            <label className="lp-field">
              Username
              <input
                className="lp-input"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                autoFocus
              />
            </label>

            <label className="lp-field">
              Password
              <input
                className="lp-input"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </label>

            {error && <div className="lp-signin-error">{error}</div>}

            <button className="btn btn-primary" type="submit" disabled={busy}>
              {busy ? (
                <>
                  <span className="spinner">◴</span> Signing in…
                </>
              ) : (
                "Sign in"
              )}
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}
