"use client";

// Browser-side auth singleton (Phase 9). Holds the single MSAL instance and the
// resolved web auth config, and exposes `apiFetch` — a fetch wrapper that
// attaches the API bearer token to same-origin /api/* calls when Entra sign-in
// is enabled. In the default `dev` provider this module is inert: no MSAL is
// constructed and `apiFetch` is a thin passthrough to `fetch`, so the existing
// dev/demo flow (proxy-injected X-Dev-User) is completely unchanged.
import {
  InteractionRequiredAuthError,
  PublicClientApplication,
  type AccountInfo,
  type Configuration,
} from "@azure/msal-browser";

export type WebAuthProviderKind = "dev" | "entra";

export interface WebAuthConfig {
  provider: WebAuthProviderKind;
  clientId: string;
  tenantId: string;
  apiScope: string;
  redirectUri?: string;
}

let _config: WebAuthConfig | null = null;
let _msal: PublicClientApplication | null = null;

export function isEntraEnabled(): boolean {
  return _config?.provider === "entra";
}

export function getWebAuthConfig(): WebAuthConfig | null {
  return _config;
}

// Records the config and (for Entra) lazily constructs the singleton MSAL
// instance. Safe to call repeatedly. Never constructs during SSR — MSAL touches
// browser globals, so construction is deferred to the client (in AuthProvider's
// effect). Returns the instance when Entra is enabled in the browser, else null.
export function initAuth(config: WebAuthConfig): PublicClientApplication | null {
  _config = config;
  if (config.provider !== "entra") return null;
  if (_msal) return _msal;
  if (typeof window === "undefined") return null;

  const origin = window.location.origin;
  const msalConfig: Configuration = {
    auth: {
      clientId: config.clientId,
      authority: `https://login.microsoftonline.com/${config.tenantId}`,
      redirectUri: config.redirectUri || origin,
      postLogoutRedirectUri: config.redirectUri || origin,
      navigateToLoginRequestUrl: true,
    },
    cache: {
      cacheLocation: "sessionStorage",
      storeAuthStateInCookie: false,
    },
  };
  _msal = new PublicClientApplication(msalConfig);
  return _msal;
}

export function getMsalInstance(): PublicClientApplication | null {
  return _msal;
}

function activeAccount(): AccountInfo | null {
  if (!_msal) return null;
  return _msal.getActiveAccount() ?? _msal.getAllAccounts()[0] ?? null;
}

// Returns an API access token for the active account, or null when Entra is
// disabled, no account is present, or a silent refresh needs interaction. On the
// interaction-required edge the caller proceeds without a token (the request may
// 401); the SignInGate owns the interactive sign-in flow.
export async function getApiAccessToken(): Promise<string | null> {
  if (!isEntraEnabled() || !_msal || !_config) return null;
  const account = activeAccount();
  if (!account) return null;
  try {
    const result = await _msal.acquireTokenSilent({
      account,
      scopes: [_config.apiScope],
    });
    return result.accessToken;
  } catch (err) {
    if (err instanceof InteractionRequiredAuthError) return null;
    return null;
  }
}

// fetch wrapper that attaches `Authorization: Bearer <token>` for same-origin
// /api/* calls when Entra is enabled. Only the Authorization header is added, so
// multipart uploads (browser-set Content-Type/boundary) are left untouched.
export async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  if (!isEntraEnabled()) return fetch(input, init);
  const token = await getApiAccessToken();
  if (!token) return fetch(input, init);
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);
  return fetch(input, { ...init, headers });
}
