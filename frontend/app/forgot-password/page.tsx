"use client";

import Link from "next/link";
import { useState } from "react";

import { api } from "@/lib/api";

/**
 * Request a password-reset link.
 *
 * The success state says "if there's an account for that address" and MUST keep
 * saying it. The endpoint answers identically whether or not the address is
 * registered — deliberately, so it can't be used to probe who has an account —
 * and a UI that said "sent!" would re-introduce exactly the membership oracle
 * the backend went to the trouble of avoiding.
 *
 * It is also why the success panel repeats the address back: with no
 * confirmation available that an account exists, a typo is the single most
 * likely reason nothing arrives, and showing what we actually sent to is the
 * only self-service diagnosis available to the user.
 */
export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      await api.forgotPassword(email);
      setSent(true);
    } catch (err) {
      // Reaches here only for a transport failure or the 429 rate limit — the
      // "does this account exist" answer never appears on this path.
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="lp-authpage">
      <div className="lp-authcard">
        <div className="lp-authbrand">
          <div className="lp-mark">4</div>
          <div className="lp-brandname">Four in a Thousand</div>
        </div>

        {sent ? (
          <>
            <h1 className="lp-auth-h">Check your inbox</h1>
            <div className="lp-auth-ok">
              If there&rsquo;s an account for <strong>{email}</strong>, a reset link is on its
              way. It works for one hour.
            </div>
            <p className="lp-auth-p" style={{ marginTop: 14 }}>
              Nothing after a few minutes? Check your spam folder, and check the address above for
              a typo. If you signed up with Google or Apple there&rsquo;s no password to reset —
              use those buttons instead.
            </p>
            <div className="lp-auth-links">
              <Link href="/">Back to sign in</Link>
            </div>
          </>
        ) : (
          <>
            <h1 className="lp-auth-h">Reset your password</h1>
            <p className="lp-auth-p">
              Enter the address you signed up with and we&rsquo;ll email you a link to choose a
              new password.
            </p>
            <form className="lp-authform" onSubmit={submit}>
              <input
                className="lp-emailinput"
                type="email"
                inputMode="email"
                autoComplete="email"
                placeholder="you@example.com"
                value={email}
                onChange={(ev) => setEmail(ev.target.value)}
                required
                autoFocus
              />
              <button className="lp-emailsubmit" type="submit" disabled={busy}>
                {busy ? "Sending…" : "Email me a reset link"}
              </button>
              {error && <div className="lp-signin-error">{error}</div>}
            </form>
            <div className="lp-auth-links">
              <Link href="/">Back to sign in</Link>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
