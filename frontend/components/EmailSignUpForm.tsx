"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { AuthResult } from "@/lib/api";

/**
 * Email + password sign-up and sign-in — a peer of the two provider buttons,
 * not a footnote under them.
 *
 * It used to be collapsed behind a small "or sign up with an email address"
 * link on the theory that a form is more attention than a one-click button, so
 * leading with it would make the fast paths look like the fallback. The theory
 * was fine and the presentation still argued the opposite of what it should:
 * this is the only path that needs no third-party account, i.e. the only one
 * available to everyone, and it was drawn as the least of three. It is now
 * always visible, below a divider that says the two groups are alternatives
 * rather than a primary and a remainder.
 *
 * Register and sign-in stay separate submissions against separate endpoints,
 * and the user picks which. That is the opposite of the provider buttons, where
 * sign-up and sign-in are deliberately one call — there the browser genuinely
 * cannot know which it is, whereas here the user knows perfectly well. Merging
 * them would mean a mistyped password on an existing account silently creating
 * a SECOND account, and the user then finding an empty profile with no
 * explanation.
 *
 * Two server errors are worth handling rather than just printing:
 *   - 409 on registering an existing address — that person is trying to sign
 *     in, so say so and flip the form over rather than leaving them re-reading
 *     an error.
 *   - 403 on signing in with an unconfirmed address (only possible when the
 *     server has EMAIL_VERIFICATION_REQUIRED on) — the fix is in their inbox,
 *     not in the form, so the message has to point there.
 */
export function EmailSignUpForm({ onSuccess }: { onSuccess: (r: AuthResult) => void }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  // Whether the server can actually send. With no mail service, promising a
  // confirmation email is a lie the user acts on by waiting.
  const [mail, setMail] = useState(false);
  const [mode, setMode] = useState<"register" | "login">("register");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .authConfig()
      .then((c) => {
        if (cancelled) return;
        setEnabled(c.email_enabled);
        setMail(c.mail_enabled);
      })
      .catch(() => !cancelled && setEnabled(false));
    return () => {
      cancelled = true;
    };
  }, []);

  if (enabled === false) return null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const r =
        mode === "register"
          ? await api.emailRegister(email, password)
          : await api.emailLogin(email, password);
      onSuccess(r);
    } catch (err) {
      const msg = (err as Error).message;
      setError(msg);
      // The server's 409 text names the fix; switching the form to it is what
      // makes that actionable in one click instead of a re-read.
      if (mode === "register" && /already exists/i.test(msg)) setMode("login");
      setBusy(false);
    }
  }

  return (
    <form className="lp-emailform" onSubmit={submit}>
      <div className="lp-emailtabs">
        <button
          type="button"
          className={`lp-emailtab${mode === "register" ? " on" : ""}`}
          onClick={() => {
            setMode("register");
            setError("");
          }}
        >
          Create account
        </button>
        <button
          type="button"
          className={`lp-emailtab${mode === "login" ? " on" : ""}`}
          onClick={() => {
            setMode("login");
            setError("");
          }}
        >
          Sign in
        </button>
      </div>

      <input
        className="lp-emailinput"
        type="email"
        inputMode="email"
        autoComplete="email"
        placeholder="you@example.com"
        value={email}
        onChange={(ev) => setEmail(ev.target.value)}
        required
      />
      <input
        className="lp-emailinput"
        type="password"
        // Tells a password manager to offer a NEW password on the register tab
        // and the saved one on sign-in — without this it offers the wrong thing
        // on whichever tab it guessed.
        autoComplete={mode === "register" ? "new-password" : "current-password"}
        placeholder={mode === "register" ? "Choose a password (8+ characters)" : "Password"}
        value={password}
        onChange={(ev) => setPassword(ev.target.value)}
        required
        minLength={mode === "register" ? 8 : undefined}
      />

      <button className="lp-emailsubmit" type="submit" disabled={busy}>
        {busy ? "Working…" : mode === "register" ? "Create account" : "Sign in"}
      </button>

      {error && <div className="lp-signin-error">{error}</div>}

      {/* Only on the sign-in tab. Offering a password reset next to a "create
          account" button is an invitation to reset a password that does not
          exist yet, and the route answers identically either way — so the user
          would get a reassuring "link on its way" for an account they never
          made, and then wait for it. */}
      {mode === "login" && (
        <Link className="lp-emaillink" href="/forgot-password">
          Forgotten your password?
        </Link>
      )}

      <div className="lp-emailfine">
        {mode === "register"
          ? mail
            ? "You're straight in — we'll email a link to confirm your address so we can reach you and reset your password if you ever need to."
            : "You're straight in. We'll only use your address to reach you about the beta."
          : "Signed up with Google or Apple? Use the buttons above instead."}
      </div>
    </form>
  );
}
