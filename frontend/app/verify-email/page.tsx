"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { postSignInRoute, setAuth } from "@/lib/auth";

/**
 * Where a confirm-your-address email lands.
 *
 * Verifying SIGNS THE USER IN (the endpoint returns a full AuthResult) and this
 * page then routes them onward exactly as the homepage does after any sign-in.
 * That is the whole reason the page exists rather than the link hitting the API
 * directly: mail is usually opened on a different device from the one the
 * account was created on, so ending at "confirmed — now go and sign in" would
 * strand precisely the people who clicked. The link is proof of mailbox
 * control, which is the same proof every password-reset flow already treats as
 * sufficient to take over an account, so this grants nothing new to whoever
 * holds it.
 *
 * It runs on mount with no confirm button. A page that asks "are you sure you
 * want to confirm your email?" is asking about something the user already did
 * by clicking, and a second click is the most common way these get abandoned.
 *
 * StrictMode double-invokes effects in dev, so the call is guarded by a ref —
 * without it the second run posts an already-consumed state and paints an error
 * over a success.
 */
function VerifyEmailInner() {
  const router = useRouter();
  const params = useSearchParams();
  const token = params.get("token") || "";
  const [error, setError] = useState("");
  const fired = useRef(false);

  useEffect(() => {
    if (fired.current) return;
    fired.current = true;

    if (!token) {
      setError("That link is missing its confirmation code. Try opening it from the email again.");
      return;
    }
    api
      .emailVerify(token)
      .then((r) => {
        setAuth(r.token, r.username, r.needs_survey, r.needs_exit_survey, r.beta_expired);
        router.replace(postSignInRoute(r));
      })
      .catch((e) => setError((e as Error).message));
  }, [token, router]);

  return (
    <div className="lp-authpage">
      <div className="lp-authcard">
        <div className="lp-authbrand">
          <div className="lp-mark">4</div>
          <div className="lp-brandname">Four in a Thousand</div>
        </div>

        {error ? (
          <>
            <h1 className="lp-auth-h">That link didn&rsquo;t work</h1>
            <p className="lp-auth-p">{error}</p>
            <p className="lp-auth-p">
              Confirmation links expire after 3 days. Sign in and we&rsquo;ll offer you a fresh
              one.
            </p>
            <div className="lp-auth-links">
              <Link href="/">Back to sign in</Link>
            </div>
          </>
        ) : (
          <>
            <h1 className="lp-auth-h">Confirming your email…</h1>
            <p className="lp-auth-p">
              <span className="spinner">◴</span> One moment.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

// useSearchParams needs a Suspense boundary or the whole route opts out of
// static rendering and the build warns.
export default function VerifyEmailPage() {
  return (
    <Suspense fallback={<div className="lp-authpage" />}>
      <VerifyEmailInner />
    </Suspense>
  );
}
