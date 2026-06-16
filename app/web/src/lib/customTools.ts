// Custom tools / bring-your-own remote MCP servers (Phase 12B) — shared types
// and pure helpers for the web UI.
//
// The runtime feature flag is surfaced to the browser via the same server-env ->
// provider-prop pattern as the Phase 9 auth / Phase 10 voice / Phase 11 library
// configs (see customToolsConfig.ts + CustomToolsProvider.tsx), NOT a NEXT_PUBLIC_*
// var, so it is evaluated at request time. When disabled the whole surface is inert
// and the app behaves exactly as before.
//
// The interfaces below mirror the FastAPI contract in
// app/api/.../agents/mcp_servers.py. The client-side caps/grammar mirror the
// backend validators to drive soft, pre-submit validation only — the backend is the
// source of truth and returns 422 with a `detail` message the UI surfaces verbatim.

export interface CustomToolsConfig {
  // When false (default) no custom-tools UI is rendered and nothing changes.
  enabled: boolean;
}

// Truthy spellings accepted for the env flag (shared by customToolsConfig).
const TRUTHY = new Set(["1", "true", "yes", "on"]);

// Parses the raw env value for the custom-tools feature flag. Default OFF.
export function parseEnabledFlag(raw: string | undefined | null): boolean {
  return TRUTHY.has((raw || "").trim().toLowerCase());
}

// --- Backend contract mirrors ----------------------------------------------

export type McpAuthMode = "none" | "api_key" | "bearer";
export type McpTransport = "streamable_http";

// A tool advertised by a remote MCP server, as cached on the record.
export interface DiscoveredTool {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
}

// Durable, server-side record of a user-registered MCP server. No secret is ever
// returned here — only `authMode` (and `secretRef` presence for authed servers).
export interface UserMcpServer {
  id: string;
  userId: string;
  name: string;
  displayName: string;
  description: string;
  endpoint: string;
  host: string;
  transport: McpTransport;
  authMode: McpAuthMode;
  trusted: boolean;
  enabled: boolean;
  secretRef: string | null;
  discoveredTools: DiscoveredTool[];
  createdAt: string;
  updatedAt: string;
  lastConnectedAt: string | null;
  lastError: string | null;
}

// Create payload — carries `name`; `secret` is transient (used only to connect).
export interface UserMcpServerCreate {
  name: string;
  displayName?: string | null;
  description: string;
  endpoint: string;
  authMode: McpAuthMode;
  secret?: string | null;
  trusted: boolean;
  enabled: boolean;
}

// Update payload — name comes from the path. Omitting `secret` for an authed
// server reuses the durably stored credential (the backend never returns it).
export interface UserMcpServerUpdate {
  displayName?: string | null;
  description: string;
  endpoint: string;
  authMode: McpAuthMode;
  secret?: string | null;
  trusted: boolean;
  enabled: boolean;
}

// Optional payload for the /test endpoint: an authed re-discovery may re-supply
// the secret (the backend never stored the raw value).
export interface UserMcpServerTest {
  secret?: string | null;
}

// --- Client-side caps + grammar (mirror app/api .../agents/mcp_servers.py) ---

export const MCP_MAX_NAME_LEN = 32;
export const MCP_MAX_DISPLAY_NAME_LEN = 80;
export const MCP_MAX_DESCRIPTION_LEN = 280;
export const MCP_MAX_ENDPOINT_LEN = 2048;
export const MCP_MAX_SECRET_LEN = 8192;

// Name grammar: start lowercase, end alphanumeric/underscore; interior . _ -
// allowed. Mirrors the backend NAME_RE exactly.
export const MCP_NAME_RE = /^[a-z](?:[a-z0-9_.-]{0,30}[a-z0-9_])?$/;

// The namespaced-tool-name prefix, mirroring the backend TOOL_NAME_PREFIX.
export const MCP_TOOL_NAME_PREFIX = "mcp";

export const MCP_AUTH_MODES: { value: McpAuthMode; label: string; hint: string }[] = [
  { value: "none", label: "None (public)", hint: "No credential is sent." },
  { value: "api_key", label: "API key", hint: "Sent to the server as an X-API-Key header." },
  { value: "bearer", label: "Bearer token", hint: "Sent as an Authorization: Bearer header." },
];

// Returns a human-readable reason the server name is invalid, or null if valid.
export function mcpServerNameError(name: string): string | null {
  if (!name) return "Name is required.";
  if (name.length > MCP_MAX_NAME_LEN)
    return `Name must be ≤ ${MCP_MAX_NAME_LEN} characters.`;
  if (!MCP_NAME_RE.test(name)) {
    return "Use lowercase letters, digits, . _ - ; start with a letter and end with a letter, digit, or underscore.";
  }
  return null;
}

// Light pre-submit endpoint check. The backend's SSRF guard is the real authority
// (it rejects non-https, private/loopback hosts, etc. with 422); this only catches
// obvious mistakes before a round trip.
export function mcpEndpointError(endpoint: string): string | null {
  const v = (endpoint || "").trim();
  if (!v) return "Endpoint URL is required.";
  if (v.length > MCP_MAX_ENDPOINT_LEN) return "Endpoint URL is too long.";
  let url: URL;
  try {
    url = new URL(v);
  } catch {
    return "Enter a valid URL, e.g. https://example.com/mcp";
  }
  if (url.protocol !== "https:") return "Endpoint must use https://.";
  return null;
}

// Returns a reason a secret is required but missing, or null. When editing an
// authed server, `allowReuseStored` lets the user leave it blank to reuse the
// durably stored credential (mirrors the backend's update semantics).
export function mcpSecretError(
  authMode: McpAuthMode,
  secret: string | undefined | null,
  allowReuseStored = false,
): string | null {
  if (authMode === "none") return null;
  const v = (secret || "").trim();
  if (!v) {
    return allowReuseStored ? null : "A secret is required for this auth mode.";
  }
  if (v.length > MCP_MAX_SECRET_LEN) return "Secret is too long.";
  return null;
}

// --- Namespaced tool names + governance projection -------------------------

// `mcp:<server>/<tool>` — the governed, collision-proof tool name. Mirrors the
// backend's namespaced_tool_name so attaching by this name matches execution.
export function namespacedToolName(serverName: string, toolName: string): string {
  return `${MCP_TOOL_NAME_PREFIX}:${serverName}/${toolName}`;
}

// True if `name` is a namespaced MCP tool (`mcp:<server>/<tool>`).
export function isMcpToolName(name: string): boolean {
  return name.startsWith(`${MCP_TOOL_NAME_PREFIX}:`);
}

// Splits a namespaced MCP tool name back into its server + tool parts, or null if
// it is not a well-formed MCP tool name.
export function parseMcpToolName(
  name: string,
): { server: string; tool: string } | null {
  if (!isMcpToolName(name)) return null;
  const rest = name.slice(MCP_TOOL_NAME_PREFIX.length + 1);
  const slash = rest.indexOf("/");
  if (slash <= 0 || slash >= rest.length - 1) return null;
  return { server: rest.slice(0, slash), tool: rest.slice(slash + 1) };
}

// The approval posture the backend projects for a server's tools
// (discovered_tool_to_spec): external risk, egress scoped to the host, and human
// approval required on every use UNLESS the server is marked trusted.
export interface ApprovalPosture {
  requiresApproval: boolean;
  label: string;
  detail: string;
}

export function approvalPosture(server: {
  trusted: boolean;
  host: string;
}): ApprovalPosture {
  if (server.trusted) {
    return {
      requiresApproval: false,
      label: "Trusted — runs without approval",
      detail: `External tool; network access limited to ${server.host}.`,
    };
  }
  return {
    requiresApproval: true,
    label: "Requires approval on each use",
    detail: `External tool; network access limited to ${server.host}.`,
  };
}

// One attachable MCP tool, flattened with its namespaced name + governance posture
// so the agent builder can render it as a checkbox grouped under its server.
export interface AttachableMcpTool {
  serverName: string;
  serverDisplayName: string;
  toolName: string;
  namespacedName: string;
  description: string;
  trusted: boolean;
  enabled: boolean;
  host: string;
}

// Groups a list of servers into their attachable MCP tools (namespaced), skipping
// nothing — disabled servers are surfaced too so the builder can show why a tool
// is unavailable. Stable order: server order, then discovered-tool order.
export function attachableMcpTools(
  servers: UserMcpServer[],
): AttachableMcpTool[] {
  const out: AttachableMcpTool[] = [];
  for (const s of servers) {
    for (const t of s.discoveredTools) {
      out.push({
        serverName: s.name,
        serverDisplayName: s.displayName || s.name,
        toolName: t.name,
        namespacedName: namespacedToolName(s.name, t.name),
        description: t.description || "",
        trusted: s.trusted,
        enabled: s.enabled,
        host: s.host,
      });
    }
  }
  return out;
}
