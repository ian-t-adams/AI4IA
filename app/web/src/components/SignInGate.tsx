"use client";

// Gates the app behind Entra sign-in. Rendered only inside MsalProvider (Entra
// mode), so the MSAL hooks are always valid here. While unauthenticated it shows
// a minimal sign-in screen; `loginRedirect` requests the API scope so the very
// first silent token acquisition for /api/* calls succeeds.
import { InteractionStatus } from "@azure/msal-browser";
import { useState, useSyncExternalStore } from "react";
import { useIsAuthenticated, useMsal } from "@azure/msal-react";

import {
  getAuthRecoveryState,
  getWebAuthConfig,
  retryInteractiveTokenRecovery,
  subscribeToAuthRecovery,
} from "@/lib/auth";
import { DOCS_PORTAL_URL } from "@/lib/docs";

export function SignInGate({ children }: { children: React.ReactNode }) {
  const isAuthenticated = useIsAuthenticated();
  const { instance, inProgress } = useMsal();
  const recovery = useSyncExternalStore(
    subscribeToAuthRecovery,
    getAuthRecoveryState,
    getAuthRecoveryState,
  );
  const [signInFailed, setSignInFailed] = useState(false);

  const config = getWebAuthConfig();
  const busy = inProgress !== InteractionStatus.None;

  const signIn = () => {
    setSignInFailed(false);
    void instance
      .loginRedirect({
        scopes: config?.provider === "entra" ? [config.apiScope] : [],
      })
      .catch(() => {
        setSignInFailed(true);
      });
  };

  if (recovery.status === "redirecting") {
    return <SignInShell message="Your session needs attention. Redirecting you to sign in…" />;
  }

  if (recovery.status === "error") {
    return (
      <SignInShell>
        <div role="alert">
          <p style={{ margin: "0 0 0.75rem", color: "var(--danger)" }}>
            We couldn&apos;t redirect you to renew your Microsoft Entra ID session.
          </p>
          <button
            type="button"
            onClick={() => {
              void retryInteractiveTokenRecovery().catch(() => {
                // The shared recovery state keeps this alert visible on failure.
              });
            }}
          >
            Try signing in again
          </button>
        </div>
      </SignInShell>
    );
  }

  if (isAuthenticated) return <>{children}</>;

  return (
    <SignInShell>
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
          <p style={{ margin: 0, color: "var(--fg-muted)", maxWidth: 520 }}>
            Sign in with Microsoft Entra ID. Your account must be authorized for this
            organization&apos;s tenant, including invited B2B guest accounts.
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
          background: "var(--accent)",
          color: "var(--accent-fg)",
          cursor: busy ? "default" : "pointer",
          opacity: busy ? 0.7 : 1,
        }}
      >
        {busy ? "Signing in…" : "Sign in"}
      </button>
      {signInFailed ? (
        <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>
          We couldn&apos;t redirect you to Microsoft Entra ID. Try signing in again.
        </p>
      ) : null}
      <DocsLink />
    </SignInShell>
  );
}

function SignInShell({
  children,
  message,
}: {
  children?: React.ReactNode;
  message?: string;
}) {
  return (
    <main
      id="main"
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
      {message ? <p aria-live="polite">{message}</p> : children}
    </main>
  );
}

function DocsLink() {
  return (
    <a
      href={DOCS_PORTAL_URL}
      target="_blank"
      rel="noopener noreferrer"
      style={{
        fontSize: "0.85em",
        color: "var(--fg-muted)",
        textDecoration: "underline",
      }}
    >
      Docs &amp; status
      <span className="visually-hidden"> (opens in a new tab)</span>
    </a>
  );
}
