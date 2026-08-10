"use client";

import { useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { AuthResult } from "@/lib/api";

const GSI_SRC = "https://accounts.google.com/gsi/client";

/** Minimal shape of the bits of Google Identity Services we actually call. */
declare global {
  interface Window {
    google?: {
      accounts: {
        id: {
          initialize: (o: {
            client_id: string;
            callback: (r: { credential?: string }) => void;
            auto_select?: boolean;
          }) => void;
          renderButton: (el: HTMLElement, o: Record<string, unknown>) => void;
        };
      };
    };
  }
}

/**
 * The whole sign-up flow, in one button.
 *
 * Google Identity Services renders its own button into a div we hand it, and
 * calls back with a signed ID token (`credential`). We never see a password and
 * never handle an OAuth code — the backend verifies the token (see
 * services/auth.py) and mints its own session token from it.
 *
 * Three states this deliberately distinguishes, because collapsing them makes
 * the one real failure unfixable:
 *   - `loading`   — the GSI script hasn't answered yet.
 *   - `disabled`  — the server has no GOOGLE_CLIENT_ID, so sign-in genuinely
 *                   cannot work. Say so, rather than render a button that 503s.
 *   - `error`     — the user tried and it failed; the server's own message.
 *
 * The client id comes from GET /auth/config rather than from
 * NEXT_PUBLIC_GOOGLE_CLIENT_ID. Both are public, but the env var is baked in at
 * BUILD time while the backend reads it at RUN time — a frontend built before
 * the credential existed would otherwise render a permanently broken button
 * against a server that is perfectly configured. The env var is honoured as a
 * fallback for the case where the API is unreachable at first paint.
 */
export function GoogleSignInButton({
  onSuccess,
  text = "signup_with",
}: {
  onSuccess: (r: AuthResult) => void;
  /** GSI's own button label key: "signup_with" | "signin_with" | "continue_with". */
  text?: "signup_with" | "signin_with" | "continue_with";
}) {
  const holder = useRef<HTMLDivElement | null>(null);
  const [clientId, setClientId] = useState<string | null>(null);
  const [status, setStatus] = useState<"loading" | "ready" | "disabled" | "working">("loading");
  const [error, setError] = useState("");

  // 1. Ask the server whether sign-in is configured, and for the client id.
  useEffect(() => {
    let cancelled = false;
    api
      .authConfig()
      .then((c) => {
        if (cancelled) return;
        if (c.google_enabled && c.google_client_id) {
          setClientId(c.google_client_id);
        } else {
          setStatus("disabled");
        }
      })
      .catch(() => {
        if (cancelled) return;
        const fallback = process.env.NEXT_PUBLIC_GOOGLE_CLIENT_ID;
        if (fallback) setClientId(fallback);
        else setStatus("disabled");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // 2. Load the GSI script once, then render Google's button into our div.
  useEffect(() => {
    if (!clientId) return;
    let cancelled = false;

    function mount() {
      if (cancelled || !holder.current || !window.google) return;
      window.google.accounts.id.initialize({
        client_id: clientId!,
        callback: async (resp) => {
          if (!resp.credential) {
            setError("Google did not return a sign-in. Please try again.");
            return;
          }
          setStatus("working");
          setError("");
          try {
            onSuccess(await api.googleAuth(resp.credential));
          } catch (e) {
            setError((e as Error).message);
            setStatus("ready");
          }
        },
      });
      holder.current.innerHTML = "";
      window.google.accounts.id.renderButton(holder.current, {
        type: "standard",
        theme: "outline",
        size: "large",
        text,
        shape: "rectangular",
        logo_alignment: "left",
        width: 320,
      });
      setStatus("ready");
    }

    if (window.google) {
      mount();
      return () => {
        cancelled = true;
      };
    }

    // Reuse an existing tag if another instance of this component already added
    // one — two <script src> tags for GSI re-initialise it and drop the first
    // button's callback.
    let script = document.querySelector<HTMLScriptElement>(`script[src="${GSI_SRC}"]`);
    if (!script) {
      script = document.createElement("script");
      script.src = GSI_SRC;
      script.async = true;
      script.defer = true;
      document.head.appendChild(script);
    }
    script.addEventListener("load", mount);
    script.addEventListener("error", () => {
      if (!cancelled) setStatus("disabled");
    });
    return () => {
      cancelled = true;
      script?.removeEventListener("load", mount);
    };
  }, [clientId, text, onSuccess]);

  if (status === "disabled") {
    return (
      <div className="lp-signin-note lp-signin-down">
        Sign-in is temporarily unavailable. Please try again shortly.
      </div>
    );
  }

  return (
    <div className="lp-google">
      {/* Google renders its button in here. Kept mounted in every state so the
          script always has a node to render into. */}
      <div ref={holder} className="lp-google-holder" />
      {status === "loading" && (
        <div className="lp-signin-note">
          <span className="spinner">◴</span> Loading sign-in…
        </div>
      )}
      {status === "working" && (
        <div className="lp-signin-note">
          <span className="spinner">◴</span> Signing you in…
        </div>
      )}
      {error && <div className="lp-signin-error">{error}</div>}
    </div>
  );
}
