"use client";

// Auth boundary. In the default `dev` provider this is a pure passthrough
// (no MSAL, existing behavior). Under `entra` it constructs/initializes the MSAL
// singleton on the client, wires the active account, and wraps the app in
// MsalProvider + SignInGate so nothing renders until the user is signed in.
import { useEffect, useState } from "react";
import { MsalProvider } from "@azure/msal-react";
import {
  EventType,
  type AuthenticationResult,
  type EventMessage,
  type PublicClientApplication,
} from "@azure/msal-browser";

import { initAuth, type WebAuthConfig } from "@/lib/auth";
import { SignInGate } from "./SignInGate";

const AUTH_INIT_TIMEOUT_MS = 15_000;

type AuthState =
  | { status: "initializing" }
  | { status: "ready"; instance: PublicClientApplication }
  | { status: "error" };

function withTimeout<T>(operation: Promise<T>): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timeout = window.setTimeout(
      () => reject(new Error("Authentication initialization timed out")),
      AUTH_INIT_TIMEOUT_MS,
    );
    operation.then(
      (value) => {
        window.clearTimeout(timeout);
        resolve(value);
      },
      (error: unknown) => {
        window.clearTimeout(timeout);
        reject(error);
      },
    );
  });
}

function FullScreenNote({ children }: { children: React.ReactNode }) {
  return (
    <main
      id="main"
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        minHeight: "100vh",
        padding: "1.5rem",
        color: "var(--fg-muted)",
        fontSize: "0.95em",
        textAlign: "center",
      }}
    >
      {children}
    </main>
  );
}

function EntraAuthProvider({
  config,
  children,
}: {
  config: WebAuthConfig;
  children: React.ReactNode;
}) {
  const [authState, setAuthState] = useState<AuthState>({
    status: "initializing",
  });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const msal = initAuth(config);
    let cancelled = false;
    let callbackId: string | null = null;

    void (async () => {
      try {
        // Yield once so every state transition from initialization is async and
        // can be cancelled by the effect cleanup.
        await Promise.resolve();
        if (!msal) throw new Error("Authentication is unavailable");
        await withTimeout(msal.initialize());
        const redirect = await withTimeout(msal.handleRedirectPromise());
        if (redirect?.account) {
          msal.setActiveAccount(redirect.account);
        } else if (!msal.getActiveAccount()) {
          const existing = msal.getAllAccounts();
          if (existing.length > 0) msal.setActiveAccount(existing[0]);
        }

        const registeredId = msal.addEventCallback((event: EventMessage) => {
          if (
            (event.eventType === EventType.LOGIN_SUCCESS ||
              event.eventType === EventType.ACQUIRE_TOKEN_SUCCESS) &&
            event.payload
          ) {
            const account = (event.payload as AuthenticationResult).account;
            if (account) msal.setActiveAccount(account);
          }
        });

        if (cancelled) {
          if (registeredId) msal.removeEventCallback(registeredId);
          return;
        }
        callbackId = registeredId;
        setAuthState({ status: "ready", instance: msal });
      } catch {
        if (!cancelled) setAuthState({ status: "error" });
      }
    })();

    return () => {
      cancelled = true;
      if (msal && callbackId) msal.removeEventCallback(callbackId);
    };
  }, [attempt, config]);

  if (authState.status === "initializing") {
    return <FullScreenNote>Signing you in…</FullScreenNote>;
  }

  if (authState.status === "error") {
    return (
      <FullScreenNote>
        <div role="alert">
          <p style={{ margin: "0 0 0.75rem", color: "var(--fg)" }}>
            We couldn&apos;t finish signing you in.
          </p>
          <button
            type="button"
            onClick={() => {
              setAuthState({ status: "initializing" });
              setAttempt((value) => value + 1);
            }}
            style={{
              minHeight: 44,
              padding: "0.65rem 1rem",
              border: "1px solid var(--border)",
              borderRadius: 8,
              background: "var(--accent)",
              color: "var(--accent-fg)",
              font: "inherit",
              fontWeight: 650,
              cursor: "pointer",
            }}
          >
            Try again
          </button>
        </div>
      </FullScreenNote>
    );
  }

  return (
    <MsalProvider instance={authState.instance}>
      <SignInGate>{children}</SignInGate>
    </MsalProvider>
  );
}

export function AuthProvider({
  config,
  children,
}: {
  config: WebAuthConfig;
  children: React.ReactNode;
}) {
  // Dev mode: record the config (so apiFetch knows Entra is off) and render the
  // app directly — no MSAL, no sign-in gate. This is the unchanged default path.
  if (config.provider !== "entra") {
    initAuth(config);
    return <>{children}</>;
  }
  return <EntraAuthProvider config={config}>{children}</EntraAuthProvider>;
}
