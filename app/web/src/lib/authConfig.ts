// Server-only: resolves the web frontend's auth configuration from runtime
// environment variables. Read in the root layout (a server component) and passed
// as plain props into the client AuthProvider, so values are evaluated at
// request time in the container — NOT inlined at build time the way NEXT_PUBLIC_*
// vars are. This mirrors how API_BASE_URL is handled (server-side env only).
//
// Defaults to the `dev` provider (no MSAL), so existing dev/demo deployments are
// unaffected unless WEB_AUTH_PROVIDER=entra and the three ENTRA_* values are set.
// If Entra is requested but a value is missing it fails open to `dev`: the API is
// the real auth boundary, so the web simply can't mint tokens (calls 401) rather
// than rendering a blank, un-recoverable screen.
import type { WebAuthConfig } from "./auth";

const DEV_CONFIG: WebAuthConfig = {
  provider: "dev",
  clientId: "",
  tenantId: "",
  apiScope: "",
};

export function getAuthConfig(): WebAuthConfig {
  const provider = (process.env.WEB_AUTH_PROVIDER || "dev").toLowerCase();
  if (provider !== "entra") return DEV_CONFIG;

  const clientId = process.env.ENTRA_CLIENT_ID || "";
  const tenantId = process.env.ENTRA_TENANT_ID || "";
  const apiScope = process.env.ENTRA_API_SCOPE || "";
  if (!clientId || !tenantId || !apiScope) return DEV_CONFIG;

  return {
    provider: "entra",
    clientId,
    tenantId,
    apiScope,
    redirectUri: process.env.ENTRA_REDIRECT_URI || undefined,
  };
}
