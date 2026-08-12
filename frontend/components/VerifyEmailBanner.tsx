"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";

/**
 * "Confirm your email" strip, shown across the app shell until the address is
 * verified.
 *
 * Scoped to `auth_provider === "email"` and nothing else. A Google or Apple
 * account can perfectly well carry `email_verified: false` — that means the
 * PROVIDER told us the address was unverified and we dropped it — and there is
 * nothing such a user could click to fix it. Nagging them would be nagging
 * about someone else's setting.
 *
 * Dismissal is per-session (component state, not localStorage), and the middle
 * ground is the point: permanent dismissal makes the nag pointless, and no
 * dismissal at all punishes someone who is mid-task and cannot deal with it
 * right now. It comes back next visit.
 *
 * It reads /me itself rather than taking props. The alternative is threading a
 * value through the layout into every page, and TanStack isn't in play here
 * because this renders outside the query-client tree for some routes — one
 * fetch on mount is the cheaper correctness.
 */
export function VerifyEmailBanner() {
  const [show, setShow] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [state, setState] = useState<"idle" | "sending" | "sent" | "failed">("idle");
  const [email, setEmail] = useState("");
  // Whether the server can actually send. With no mail service the resend
  // button is a button that does nothing, so the banner drops to a statement.
  const [mail, setMail] = useState(true);

  useEffect(() => {
    let cancelled = false;
    Promise.all([api.me(), api.authConfig().catch(() => null)])
      .then(([m, cfg]) => {
        if (cancelled) return;
        setShow(m.auth_provider === "email" && !m.email_verified);
        setEmail(m.email || "");
        if (cfg) setMail(cfg.mail_enabled);
      })
      .catch(() => {
        /* Non-blocking: a nag must never be the thing that breaks a page. */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!show || dismissed) return null;

  async function resend() {
    setState("sending");
    try {
      await api.resendVerification();
      setState("sent");
    } catch {
      // Includes the 429. The message is deliberately the same either way —
      // "we couldn't send another right now" covers both a transport failure
      // and a throttled caller, and neither is something the user can act on
      // differently.
      setState("failed");
    }
  }

  return (
    <div className="verify-banner">
      <div className="verify-banner-msg">
        {state === "sent" ? (
          <>Sent. Check {email ? <strong>{email}</strong> : "your inbox"} for the link.</>
        ) : mail ? (
          <>
            Confirm your email{email ? <> (<strong>{email}</strong>)</> : null} so we can reach you
            and reset your password if you ever need to.
          </>
        ) : (
          <>Your email address hasn&rsquo;t been confirmed yet.</>
        )}
        {state === "failed" && <> Couldn&rsquo;t send another right now — try again later.</>}
      </div>
      {mail && state !== "sent" && (
        <button className="verify-banner-act" onClick={resend} disabled={state === "sending"}>
          {state === "sending" ? "Sending…" : "Resend the link"}
        </button>
      )}
      <button
        className="verify-banner-x"
        onClick={() => setDismissed(true)}
        title="Hide until next visit"
        aria-label="Hide until next visit"
      >
        ✕
      </button>
    </div>
  );
}
