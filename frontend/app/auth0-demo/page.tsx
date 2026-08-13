// Standalone trial of the Auth0 Next.js SDK. Not linked from anywhere in the
// real app and does not touch the existing custom auth (lib/auth.ts) or any
// backend API call — it only proves the Auth0 tenant/app/SDK wiring works.
import { auth0 } from "@/lib/auth0";

export default async function Auth0DemoPage() {
  const session = await auth0.getSession();

  return (
    <main style={{ maxWidth: 520, margin: "60px auto", padding: 24, fontFamily: "sans-serif" }}>
      <h1 style={{ fontSize: 20, marginBottom: 8 }}>Auth0 SDK trial</h1>
      <p style={{ fontSize: 13, color: "#888", marginBottom: 24 }}>
        Experimental page, isolated from the app&rsquo;s real sign-in. Nothing here affects
        your Google/Apple/email login.
      </p>

      {!session ? (
        <>
          <a href="/auth/login?screen_hint=signup" style={{ marginRight: 16 }}>
            Sign up
          </a>
          <a href="/auth/login">Log in</a>
        </>
      ) : (
        <>
          <p>Logged in as {session.user.email}</p>
          <pre style={{ background: "#f5f5f5", padding: 12, fontSize: 12, overflowX: "auto" }}>
            {JSON.stringify(session.user, null, 2)}
          </pre>
          <a href="/auth/logout">Log out</a>
        </>
      )}
    </main>
  );
}
