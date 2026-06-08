"use client";

// Phase 9 auth boundary. In the default `dev` provider this is a pure passthrough
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

function FullScreenNote({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        height: "100vh",
        color: "var(--fg-muted)",
        fontSize: "0.95em",
      }}
    >
      {children}
    </div>
  );
}

function EntraAuthProvider({
  config,
  children,
}: {
  config: WebAuthConfig;
  children: React.ReactNode;
}) {
  const [instance, setInstance] = useState<PublicClientApplication | null>(null);

  useEffect(() => {
    const msal = initAuth(config);
    if (!msal) return;
    let cancelled = false;
    void (async () => {
      await msal.initialize();
      const redirect = await msal.handleRedirectPromise();
      if (redirect?.account) {
        msal.setActiveAccount(redirect.account);
      } else if (!msal.getActiveAccount()) {
        const existing = msal.getAllAccounts();
        if (existing.length > 0) msal.setActiveAccount(existing[0]);
      }
      // Keep the active account current across future logins / token refreshes.
      msal.addEventCallback((event: EventMessage) => {
        if (
          (event.eventType === EventType.LOGIN_SUCCESS ||
            event.eventType === EventType.ACQUIRE_TOKEN_SUCCESS) &&
          event.payload
        ) {
          const account = (event.payload as AuthenticationResult).account;
          if (account) msal.setActiveAccount(account);
        }
      });
      if (!cancelled) setInstance(msal);
    })();
    return () => {
      cancelled = true;
    };
  }, [config]);

  if (!instance) return <FullScreenNote>Signing you in…</FullScreenNote>;

  return (
    <MsalProvider instance={instance}>
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
