"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { api } from "@/lib/api";
import { setAuth } from "@/lib/auth";

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
      setAuth(r.token, r.username);
      router.replace("/");
    } catch (err) {
      setError((err as Error).message);
      setBusy(false);
    }
  }

  return (
    <div className="screen">
      <div className="center-pad">
        <form
          onSubmit={submit}
          style={{
            width: "100%",
            maxWidth: 360,
            display: "flex",
            flexDirection: "column",
            gap: 14,
            border: "1px solid var(--line)",
            borderRadius: "var(--r-panel, 12px)",
            padding: 24,
            background: "var(--surface, #fff)",
          }}
        >
          <div>
            <div className="logo" style={{ fontSize: 18 }}>
              Four in a Thousand
            </div>
            <div className="page-sub" style={{ paddingTop: 4 }}>
              Sign in with the credentials you were given.
            </div>
          </div>

          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
            Username
            <input
              className="text-input"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              autoComplete="username"
              autoFocus
              style={inputStyle}
            />
          </label>

          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 12 }}>
            Password
            <input
              className="text-input"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="current-password"
              style={inputStyle}
            />
          </label>

          {error && (
            <div style={{ color: "var(--danger, #c0392b)", fontSize: 12.5 }}>{error}</div>
          )}

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
  );
}

const inputStyle: React.CSSProperties = {
  border: "1px solid var(--line)",
  borderRadius: 8,
  padding: "9px 11px",
  fontSize: 14,
  background: "var(--surface-2, #fff)",
  color: "var(--ink, inherit)",
};
