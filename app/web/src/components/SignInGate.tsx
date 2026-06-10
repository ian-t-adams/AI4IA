"use client";

// Gates the app behind Entra sign-in. Rendered only inside MsalProvider (Entra
// mode), so the MSAL hooks are always valid here. While unauthenticated it shows
// a minimal sign-in screen; `loginRedirect` requests the API scope so the very
// first silent token acquisition for /api/* calls succeeds.
import { InteractionStatus } from "@azure/msal-browser";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";

import { getWebAuthConfig } from "@/lib/auth";

export function SignInGate({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useIsAuthenticated();
  const { instance, inProgress } = useMsal();

  if (isAuthenticated) return <>{children}</>;

  const config = getWebAuthConfig();
  const busy = inProgress !== InteractionStatus.None;

  const signIn = () => {
    void instance
      .loginRedirect({ scopes: config ? [config.apiScope] : [] })
      .catch(() => {
        /* user closed the flow or a transient error; they can retry */
      });
  };

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 20,
        height: "100vh",
        padding: 24,
        textAlign: "center",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 14,
        }}
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- small static brand mark; next/image adds no value here */}
        <img
          src="/ai4ia-mark.png"
          alt="AI4IA"
          width={72}
          height={72}
          style={{ borderRadius: 16, display: "block" }}
        />
        <div>
          <h1 style={{ margin: "0 0 8px", fontSize: "1.5em" }}>AI4IA</h1>
          <p style={{ margin: 0, color: "var(--fg-muted)" }}>
            Sign in with your Microsoft account to continue.
          </p>
        </div>
      </div>
      <button
        type="button"
        onClick={signIn}
        disabled={busy}
        style={{
          padding: "10px 20px",
          fontSize: "1em",
          borderRadius: 8,
          border: "1px solid var(--border)",
          background: "var(--accent, #2563eb)",
          color: "#fff",
          cursor: busy ? "default" : "pointer",
          opacity: busy ? 0.7 : 1,
        }}
      >
        {busy ? "Signing in…" : "Sign in"}
      </button>
    </div>
  );
}
