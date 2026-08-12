"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";

import { api } from "@/lib/api";
import { postSignInRoute, setAuth } from "@/lib/auth";

/**
 * Where a reset-password email lands: choose a new password, then straight into
 * the app.
 *
 * Unlike /verify-email this does NOT act on mount — it needs input, and there
 * is a real destructive effect (the old password stops working) that the user
 * should be the one to trigger. Signing in afterwards is the same
 * different-device reasoning as the verification page: someone who just proved
 * mailbox control and set a password should not be handed a sign-in form to
 * immediately retype it into.
 *
 * The token is not validated until submit. Checking it on load would need a
 * second endpoint whose only purpose is to say "this link is dead" slightly
 * earlier, and would spend the link's one use to find out.
 */
function ResetPasswordInner() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token") || "";
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    // Checked here rather than server-side: the server is given one password
    // and has no business knowing the user typed it twice. A mismatch is a typo
    // to catch before spending the link, not a validation rule.
    if (password !== confirm) {
      setError("Those two passwords don't match.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const r = await api.resetPassword(token, password);
      setAuth(r.token, r.username, r.needs_survey, r.needs_exit_survey, r.beta_expired);
      router.replace(postSignInRoute(r));
    } catch (err) {
      setError((err as Error).message);
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

        {!token ? (
          <>
            <h1 className="lp-auth-h">That link is incomplete</h1>
            <p className="lp-auth-p">
              It&rsquo;s missing its reset code. Open it straight from the email, or request a
              fresh one.
            </p>
            <div className="lp-auth-links">
              <Link href="/forgot-password">Request a new reset link</Link>
            </div>
          </>
        ) : (
          <>
            <h1 className="lp-auth-h">Choose a new password</h1>
            <p className="lp-auth-p">
              At least 8 characters. You&rsquo;ll be signed in as soon as it&rsquo;s set.
            </p>
            <form className="lp-authform" onSubmit={submit}>
              <input
                className="lp-emailinput"
                type="password"
                autoComplete="new-password"
                placeholder="New password"
                value={password}
                onChange={(ev) => setPassword(ev.target.value)}
                required
                minLength={8}
                autoFocus
              />
              <input
                className="lp-emailinput"
                type="password"
                autoComplete="new-password"
                placeholder="Repeat it"
                value={confirm}
                onChange={(ev) => setConfirm(ev.target.value)}
                required
                minLength={8}
              />
              <button className="lp-emailsubmit" type="submit" disabled={busy}>
                {busy ? "Setting…" : "Set password and sign in"}
              </button>
              {error && <div className="lp-signin-error">{error}</div>}
            </form>
            <div className="lp-auth-links">
              <Link href="/forgot-password">Request a new reset link</Link>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// useSearchParams needs a Suspense boundary or the route opts out of static
// rendering and the build warns.
export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<div className="lp-authpage" />}>
      <ResetPasswordInner />
    </Suspense>
  );
}
