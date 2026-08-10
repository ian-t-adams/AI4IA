// Server-only: resolves the web frontend's auth configuration from runtime
// environment variables. Read in the protected route-group layout (a server component) and passed
// as plain props into the client AuthProvider, so values are evaluated at
// request time in the container — NOT inlined at build time the way NEXT_PUBLIC_*
// vars are. This mirrors how API_BASE_URL is handled (server-side env only).
//
// Defaults to the `dev` provider (no MSAL), so existing dev/demo deployments are
// unaffected unless WEB_AUTH_PROVIDER=entra. An explicitly requested but
// incomplete Entra configuration fails closed into a configuration-error screen.
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
  const missingValues = [
    !clientId ? "ENTRA_CLIENT_ID" : null,
    !tenantId ? "ENTRA_TENANT_ID" : null,
    !apiScope ? "ENTRA_API_SCOPE" : null,
  ].filter((value): value is string => value !== null);
  if (missingValues.length > 0) {
    return { provider: "configuration-error", missingValues };
  }

  return {
    provider: "entra",
    clientId,
    tenantId,
    apiScope,
    redirectUri: process.env.ENTRA_REDIRECT_URI || undefined,
  };
}
