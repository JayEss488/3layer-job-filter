"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { AuthResult } from "@/lib/api";

const APPLE_JS_SRC =
  "https://appleid.cdn-apple.com/appleauth/static/jsapi/appleid/1/en_US/appleid.auth.js";

/** The bits of AppleID.js we actually call. */
declare global {
  interface Window {
    AppleID?: {
      auth: {
        init: (o: {
          clientId: string;
          scope: string;
          redirectURI: string;
          usePopup: boolean;
          state?: string;
        }) => void;
        signIn: () => Promise<{
          authorization?: { id_token?: string; code?: string; state?: string };
          user?: { name?: { firstName?: string; lastName?: string }; email?: string };
        }>;
      };
    };
  }
}

/**
 * Sign in with Apple, as the second self-serve path beside Google.
 *
 * Deliberately the POPUP flow (`usePopup: true`), not Apple's default
 * redirect. The redirect flow POSTs a form back to `redirectURI`, which would
 * mean a backend route that accepts a form post, sets a cookie or bounces
 * through a URL fragment, and a full page reload in the middle of sign-up —
 * i.e. a second, quite different session mechanism next to the Bearer token
 * everything else uses. The popup hands the page a signed `id_token` in a
 * promise instead, which is byte-for-byte the same shape the Google button
 * already produces, so it joins the existing flow at exactly the same seam
 * (`api.appleAuth` -> the server verifies -> `onSuccess`).
 *
 * `redirectURI` is still required by Apple even in popup mode, and must be
 * registered on the Services ID or the popup is rejected before it opens. It is
 * the site's own origin.
 *
 * Apple returns the user's NAME once: in `user` on the very first
 * authorization, never in the token and never again. It is forwarded for
 * display only — the server keys the account on the token's verified `sub`.
 *
 * Three states, distinguished for the same reason GoogleSignInButton
 * distinguishes them: `disabled` (no APPLE_CLIENT_ID on the server, so nothing
 * can work) must not look like `error` (the user tried and it failed).
 */
export function AppleSignInButton({
  onSuccess,
  label = "Sign up with Apple",
}: {
  onSuccess: (r: AuthResult) => void;
  label?: string;
}) {
  const [clientId, setClientId] = useState<string | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "disabled" | "working">("loading");
  const [error, setError] = useState("");

  // 1. Ask the server whether Apple sign-in is configured, and for the
  //    Services ID. Same run-time-not-build-time reasoning as the Google button.
  useEffect(() => {
    let cancelled = false;
    api
      .authConfig()
      .then((c) => {
        if (cancelled) return;
        if (c.apple_enabled && c.apple_client_id) setClientId(c.apple_client_id);
        else setStatus("disabled");
      })
      .catch(() => {
        if (!cancelled) setStatus("disabled");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 2. Load AppleID.js once, then init it.
  useEffect(() => {
    if (!clientId) return;
    let cancelled = false;

    function init() {
      if (cancelled || !window.AppleID) return;
      try {
        window.AppleID.auth.init({
          // Non-null by the guard above; TS can't narrow across the closure.
          clientId: clientId!,
          scope: "name email",
          // Must exactly match a Return URL registered on the Services ID.
          redirectURI: window.location.origin,
          usePopup: true,
        });
        setStatus("ready");
      } catch {
        setStatus("disabled");
      }
    }

    if (window.AppleID) {
      init();
      return () => {
        cancelled = true;
      };
    }

    // Reuse an existing tag if another instance already added one, exactly as
    // the Google button does — two <script> tags re-initialise the SDK.
    let script = document.querySelector<HTMLScriptElement>(`script[src="${APPLE_JS_SRC}"]`);
    if (!script) {
      script = document.createElement("script");
      script.src = APPLE_JS_SRC;
      script.async = true;
      script.defer = true;
      document.head.appendChild(script);
    }
    script.addEventListener("load", init);
    script.addEventListener("error", () => {
      if (!cancelled) setStatus("disabled");
    });
    return () => {
      cancelled = true;
      script?.removeEventListener("load", init);
    };
  }, [clientId]);

  async function signIn() {
    if (!window.AppleID) return;
    setStatus("working");
    setError("");
    try {
      const resp = await window.AppleID.auth.signIn();
      const idToken = resp?.authorization?.id_token;
      if (!idToken) {
        setError("Apple did not return a sign-in. Please try again.");
        setStatus("ready");
        return;
      }
      const first = resp.user?.name?.firstName ?? "";
      const last = resp.user?.name?.lastName ?? "";
      onSuccess(await api.appleAuth(idToken, `${first} ${last}`.trim()));
    } catch (e) {
      // Closing the popup rejects with { error: "popup_closed_by_user" }, which
      // is a deliberate cancellation and not something to shout about.
      const code = (e as { error?: string })?.error ?? "";
      if (code !== "popup_closed_by_user" && code !== "user_cancelled_authorize") {
        setError((e as Error)?.message || "Apple sign-in failed. Please try again.");
      }
      setStatus("ready");
    }
  }

  // Rendered as nothing when unavailable: unlike Google (the primary path,
  // whose absence is worth explaining) an unconfigured Apple button should
  // simply not be offered, and a "temporarily unavailable" note next to two
  // working alternatives is noise.
  if (status === "disabled") return null;

  return (
    <div className="lp-apple">
      <button
        type="button"
        className="lp-applebtn"
        onClick={signIn}
        disabled={status !== "ready"}
      >
        <svg className="lp-applemark" viewBox="0 0 384 512" aria-hidden="true" focusable="false">
          <path
            fill="currentColor"
            d="M318.7 268.7c-.2-36.7 16.4-64.4 50-84.8-18.8-26.9-47.2-41.7-84.7-44.6-35.5-2.8-74.3 20.7-88.5 20.7-15 0-49.4-19.7-76.4-19.7C63.3 141.2 4 184.8 4 273.5q0 39.3 14.4 81.2c12.8 36.7 59 126.7 107.2 125.2 25.2-.6 43-17.9 75.8-17.9 31.8 0 48.3 17.9 76.4 17.9 48.6-.7 90.4-82.5 102.6-119.3-65.2-30.7-61.7-90-61.7-91.9zm-56.6-164.2c27.3-32.4 24.8-61.9 24-72.5-24.1 1.4-52 16.4-67.9 34.9-17.5 19.8-27.8 44.3-25.6 71.9 26.1 2 49.9-11.4 69.5-34.3z"
          />
        </svg>
        <span>{status === "working" ? "Signing you in…" : label}</span>
      </button>
      {error && <div className="lp-signin-error">{error}</div>}
    </div>
  );
}
