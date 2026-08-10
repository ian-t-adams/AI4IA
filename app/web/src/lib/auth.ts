"use client";

// Browser-side auth singleton. Holds the single MSAL instance and the
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

export type WebAuthProviderKind = "dev" | "entra" | "configuration-error";

export type WebAuthConfig =
  | {
      provider: "dev";
      clientId: "";
      tenantId: "";
      apiScope: "";
    }
  | {
      provider: "entra";
      clientId: string;
      tenantId: string;
      apiScope: string;
      redirectUri?: string;
    }
  | {
      provider: "configuration-error";
      missingValues: string[];
    };

export type AuthRecoveryState =
  | { status: "idle" }
  | { status: "redirecting" }
  | { status: "error" };

let _config: WebAuthConfig | null = null;
let _msal: PublicClientApplication | null = null;
let _recoveryState: AuthRecoveryState = { status: "idle" };
let _recoveryFlight: Promise<never> | null = null;
const _recoveryListeners = new Set<() => void>();

export class AuthenticationRecoveryError extends Error {
  constructor() {
    super("Interactive authentication is required");
    this.name = "AuthenticationRecoveryError";
  }
}

function setRecoveryState(state: AuthRecoveryState) {
  _recoveryState = state;
  for (const listener of _recoveryListeners) listener();
}

export function getAuthRecoveryState(): AuthRecoveryState {
  return _recoveryState;
}

export function subscribeToAuthRecovery(listener: () => void): () => void {
  _recoveryListeners.add(listener);
  return () => _recoveryListeners.delete(listener);
}

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
    },
    cache: {
      cacheLocation: "sessionStorage",
    },
  };
  _msal = new PublicClientApplication(msalConfig);
  return _msal;
}

function activeAccount(): AccountInfo | null {
  if (!_msal) return null;
  return _msal.getActiveAccount() ?? _msal.getAllAccounts()[0] ?? null;
}

function startInteractiveTokenRecovery(): Promise<never> {
  if (_recoveryFlight) return _recoveryFlight;
  const client = _msal;
  const config = _config;
  if (
    _recoveryState.status === "error" ||
    !client ||
    config?.provider !== "entra"
  ) {
    return Promise.reject(new AuthenticationRecoveryError());
  }

  const account = activeAccount();
  if (!account) {
    setRecoveryState({ status: "error" });
    return Promise.reject(new AuthenticationRecoveryError());
  }

  setRecoveryState({ status: "redirecting" });
  _recoveryFlight = (async () => {
    try {
      await client.acquireTokenRedirect({
        account,
        scopes: [config.apiScope],
      });
    } catch {
      setRecoveryState({ status: "error" });
      throw new AuthenticationRecoveryError();
    } finally {
      _recoveryFlight = null;
    }

    // A successful redirect unloads the page before this is reached. If a
    // browser or host suppresses navigation, fail visibly rather than leaving
    // an authenticated-looking shell that can only issue unauthenticated calls.
    setRecoveryState({ status: "error" });
    throw new AuthenticationRecoveryError();
  })();
  return _recoveryFlight;
}

export function retryInteractiveTokenRecovery(): Promise<never> {
  setRecoveryState({ status: "idle" });
  return startInteractiveTokenRecovery();
}

// Returns an API access token for the active account, or null when Entra is
// disabled or no account is present. Interaction-required sessions enter a
// single-flight redirect and never fall through to an unauthenticated request.
export async function getApiAccessToken(): Promise<string | null> {
  const client = _msal;
  const config = _config;
  if (!client || config?.provider !== "entra") return null;
  const account = activeAccount();
  if (!account) return null;
  try {
    const result = await client.acquireTokenSilent({
      account,
      scopes: [config.apiScope],
    });
    return result.accessToken;
  } catch (err) {
    if (err instanceof InteractionRequiredAuthError) {
      return startInteractiveTokenRecovery();
    }
    throw err;
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
