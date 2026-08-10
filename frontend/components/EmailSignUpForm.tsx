"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { AuthResult } from "@/lib/api";

/**
 * Email + password sign-up, the path that needs no third-party account.
 *
 * Collapsed behind a link by default. The two provider buttons are one click
 * each and a form is four fields' worth of attention, so leading with the form
 * would make the fast paths look like the fallback; but the link has to be
 * visible, because the entire point is to catch the people for whom neither
 * provider is an option.
 *
 * Register and sign-in are separate submissions against separate endpoints, and
 * the user picks which. That is the opposite of the provider buttons, where
 * sign-up and sign-in are deliberately one call — there the browser genuinely
 * cannot know which it is, whereas here the user knows perfectly well. Merging
 * them would mean a mistyped password on an existing account silently creating
 * a SECOND account, and the user then finding an empty profile with no
 * explanation.
 *
 * The one server error worth special handling is the 409 on registering an
 * address that already exists — that user is trying to sign in, so we say so
 * and flip the form over rather than leaving them re-reading an error.
 */
export function EmailSignUpForm({ onSuccess }: { onSuccess: (r: AuthResult) => void }) {
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"register" | "login">("register");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .authConfig()
      .then((c) => !cancelled && setEnabled(c.email_enabled))
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

  if (!open) {
    return (
      <button type="button" className="lp-emaillink" onClick={() => setOpen(true)}>
        Or sign up with an email address
      </button>
    );
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
      <div className="lp-emailfine">
        {mode === "register"
          ? "No verification email — you're straight in. We'll only use your address to reach you about the beta."
          : "Signed up with Google or Apple? Use the buttons above instead."}
      </div>
    </form>
  );
}
