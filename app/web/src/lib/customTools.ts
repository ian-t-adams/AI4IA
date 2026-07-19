// Custom tools / bring-your-own remote MCP servers: shared types
// and pure helpers for the web UI.
//
// The runtime feature flag is surfaced to the browser via the same server-env ->
// provider-prop pattern as the auth, voice, and library configs (see
// customToolsConfig.ts + CustomToolsProvider.tsx), NOT a NEXT_PUBLIC_*
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

// Per-tool human-approval posture, overriding the server-level default. Mirrors
// the backend McpToolApproval enum: `default` inherits the server posture
// (approval-unless-trusted), `always` forces approval even on a trusted server,
// `never` pre-approves the tool even on an untrusted server.
export type McpToolApproval = "default" | "always" | "never";

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
  // Per-tool approval overrides, keyed by the *bare* discovered tool name.
  // Absent / `default` -> inherit the server posture. Mirrors the backend record.
  toolApprovals: Record<string, McpToolApproval>;
  createdAt: string;
  updatedAt: string;
  lastConnectedAt: string | null;
  lastError: string | null;
  // --- Health / quarantine -------------------------------------------------
  // Per-server health surfaced for badges. `consecutiveFailures` counts
  // connect/execute transport failures since the last success; `quarantinedUntil`
  // (when set and in the future) means the server is skipped until it elapses;
  // `lastHealthCheck` is when health was last observed; `lastHealthError` is a
  // bounded, redacted summary of the latest failure.
  consecutiveFailures: number;
  quarantinedUntil: string | null;
  lastHealthCheck: string | null;
  lastHealthError: string | null;
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
  // Per-tool approval overrides (bare tool name -> posture). Omitted (`undefined`)
  // leaves the stored overrides unchanged; an explicit map (possibly empty)
  // replaces them. Unknown tool names are pruned by the backend on re-discovery.
  toolApprovals?: Record<string, McpToolApproval> | null;
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

// Per-tool approval options for the builder select. `default` inherits the server
// posture; `always`/`never` override it. Mirrors the backend McpToolApproval enum.
export const MCP_TOOL_APPROVALS: { value: McpToolApproval; label: string; hint: string }[] = [
  {
    value: "default",
    label: "Default (inherit server)",
    hint:
      "Chat has no live approval prompt, so on an untrusted server this " +
      "tool is left out of what the model can call. Mark the server " +
      "trusted, or set this override to Never, to make it available.",
  },
  {
    value: "always",
    label: "Always require approval",
    hint:
      "Chat has no live approval prompt, so this tool is simply left out of " +
      "what the model can call — even on a trusted server — until you pick " +
      "a different option here.",
  },
  {
    value: "never",
    label: "Never require approval",
    hint: "Pre-approve this tool, even on an untrusted server.",
  },
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
  const scopeDetail = `External tool; network access limited to ${server.host}.`;
  if (server.trusted) {
    return {
      requiresApproval: false,
      label: "Trusted — runs without approval",
      detail: scopeDetail,
    };
  }
  return {
    requiresApproval: true,
    label: "Unavailable until trusted",
    detail:
      `${scopeDetail} Chat has no live approval prompt, so these tools are ` +
      "left out of what the model can call until the server is trusted, or " +
      "a tool is individually pre-approved below.",
  };
}

// --- Per-tool approval (mirror mcp_servers.effective_tool_approval / _requires) ---

// The per-tool approval posture in force for `toolName` on `server`, falling back
// to `default` (inherit the server posture) when no override is set.
export function effectiveToolApproval(
  server: { toolApprovals?: Record<string, McpToolApproval> | null },
  toolName: string,
): McpToolApproval {
  return server.toolApprovals?.[toolName] ?? "default";
}

// Whether a remote tool needs human approval on each use: `always` -> true,
// `never` -> false, `default` -> the existing rule (required unless trusted).
// Single source of truth shared by the badge + the attach projection, mirroring
// the backend tool_requires_approval so the UI matches the runtime gate.
export function toolRequiresApproval(
  server: { trusted: boolean; toolApprovals?: Record<string, McpToolApproval> | null },
  toolName: string,
): boolean {
  const posture = effectiveToolApproval(server, toolName);
  if (posture === "always") return true;
  if (posture === "never") return false;
  return !server.trusted;
}

// Compact, badge-ready posture for a single discovered tool: the resolved
// approval requirement plus a short label/detail describing why.
export function toolApprovalPosture(
  server: { trusted: boolean; host: string; toolApprovals?: Record<string, McpToolApproval> | null },
  toolName: string,
): ApprovalPosture & { posture: McpToolApproval } {
  const posture = effectiveToolApproval(server, toolName);
  const requiresApproval = toolRequiresApproval(server, toolName);
  const scopeDetail = `External tool; network access limited to ${server.host}.`;
  let label: string;
  let detail = scopeDetail;
  if (posture === "always") label = "Always requires approval";
  else if (posture === "never") label = "Pre-approved — runs without approval";
  else if (requiresApproval) {
    label = "Unavailable until trusted";
    detail =
      `${scopeDetail} Chat has no live approval prompt, so this tool is ` +
      "left out of what the model can call unless the server is trusted, " +
      "or this tool's approval is set to Never.";
  } else label = "Trusted — runs without approval";
  return {
    posture,
    requiresApproval,
    label,
    detail,
  };
}

// --- Health / quarantine (mirror mcp_health) -------------------------------

export type McpHealthStatus = "healthy" | "degraded" | "quarantined" | "unknown";

// Minimal slice of the record the health helpers read (so the badges can be fed
// either a full UserMcpServer or a lighter shape in tests).
export interface McpHealthFields {
  consecutiveFailures: number;
  quarantinedUntil: string | null;
  lastHealthCheck: string | null;
  lastHealthError: string | null;
  lastConnectedAt: string | null;
}

function parseTime(value: string | null): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? null : ms;
}

// True if the server is currently quarantined (window not yet elapsed). `now`
// (ms) is injectable for deterministic tests; defaults to wall-clock.
export function isQuarantined(server: McpHealthFields, now: number = Date.now()): boolean {
  const until = parseTime(server.quarantinedUntil);
  return until !== null && now < until;
}

// Derive the coarse health status from the tracked state, mirroring the backend
// mcp_health.health_status precedence (quarantined > degraded > unknown > healthy).
export function healthStatus(
  server: McpHealthFields,
  now: number = Date.now(),
): McpHealthStatus {
  if (isQuarantined(server, now)) return "quarantined";
  if ((server.consecutiveFailures ?? 0) > 0) return "degraded";
  if (server.lastHealthCheck === null && server.lastConnectedAt === null) return "unknown";
  return "healthy";
}

// A short, human-readable reason a server is being skipped, or null if it is not
// quarantined. Mirrors mcp_health.quarantine_reason for the badge tooltip.
export function quarantineReason(
  server: McpHealthFields,
  now: number = Date.now(),
): string | null {
  const until = parseTime(server.quarantinedUntil);
  if (until === null || now >= until) return null;
  const secs = Math.max(0, Math.round((until - now) / 1000));
  const when = secs >= 60 ? `${Math.floor(secs / 60)}m` : `${secs}s`;
  const why = server.lastHealthError || "repeated connection failures";
  return `quarantined for ~${when} after ${server.consecutiveFailures} failures: ${why}`;
}

// A compact label + tone for the health badge. `tone` maps to the existing status
// pill classes used by the C components.
export interface HealthBadge {
  status: McpHealthStatus;
  label: string;
  tone: "ok" | "warn" | "error" | "muted";
  detail: string | null;
}

export function healthBadge(server: McpHealthFields, now: number = Date.now()): HealthBadge {
  const status = healthStatus(server, now);
  switch (status) {
    case "quarantined":
      return {
        status,
        label: "Quarantined",
        tone: "error",
        detail: quarantineReason(server, now),
      };
    case "degraded":
      return {
        status,
        label: "Degraded",
        tone: "warn",
        detail: server.lastHealthError || "Recent connection failures.",
      };
    case "unknown":
      return { status, label: "Unknown", tone: "muted", detail: "Not checked yet." };
    default:
      return { status, label: "Healthy", tone: "ok", detail: null };
  }
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
  // Resolved per-tool approval requirement + posture (mirrors the runtime gate),
  // so the agent builder can show whether attaching the tool will prompt.
  requiresApproval: boolean;
  approval: McpToolApproval;
  // True when the tool comes from the curated **official** plane (APIM-fronted),
  // not a user's own BYO server. Drives the read-only "official" badge and lets
  // the picker show both planes in one list.
  official: boolean;
}

// Groups a list of servers into their attachable MCP tools (namespaced), skipping
// nothing — disabled servers are surfaced too so the builder can show why a tool
// is unavailable. Stable order: server order, then discovered-tool order.
export function attachableMcpTools(
  servers: UserMcpServer[],
  opts: { official?: boolean } = {},
): AttachableMcpTool[] {
  const official = opts.official ?? false;
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
        requiresApproval: toolRequiresApproval(s, t.name),
        approval: effectiveToolApproval(s, t.name),
        official,
      });
    }
  }
  return out;
}
