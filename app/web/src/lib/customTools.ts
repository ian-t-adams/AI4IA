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

// Per-tool standing discovery/attachment posture, overriding the server-level
// default. This decides whether the model is offered a discovered tool; it does
// not replace interactive exact-argument invocation approval.
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

// Per-tool discovery options for the builder select. `default` inherits the
// server posture; `always`/`never` override it. Mirrors McpToolApproval's stored
// wire values while distinguishing this standing posture from invocation approval.
// `default` and `never` are the two options a user might read as "this will make
// the tool work" — so their hints call out that they only ever clear the
// *approval* gate, not a separately disabled or quarantined server (see
// McpBlockingState below). `always` never claims to help, so it needs no caveat.
export const MCP_TOOL_APPROVALS: { value: McpToolApproval; label: string; hint: string }[] = [
  {
    value: "default",
    label: "Default discovery posture (inherit server)",
    hint:
      "On an untrusted server this tool is left out of what the model can " +
      "call. Trusting the server or setting Never makes it attachable, but " +
      "invocation is governed separately. Under the default interactive policy, " +
      "external/destructive calls require fresh exact-call approval. A server " +
      "that is turned off or quarantined remains blocked.",
  },
  {
    value: "always",
    label: "Always withhold from model",
    hint:
      "This standing posture leaves the tool out of what the model can call, " +
      "even on a trusted server. It is separate from invocation approval.",
  },
  {
    value: "never",
    label: "Allow model attachment",
    hint:
      "Makes the tool attachable even on an untrusted server. Interactive " +
      "invocation is governed separately; the default policy holds external/" +
      "destructive calls for fresh exact-call approval, while unattended " +
      "workflows explicitly run with ApprovalPolicy.off. " +
      "A server that is turned off or quarantined remains blocked.",
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

// A hard block on a tool that exists independent of its approval posture: the
// server itself is turned off, or automatically quarantined after repeated
// connection failures. Either overrides trust/approval entirely — mirrors the
// backend precedence exactly: mcp_servers.discovered_tool_to_spec sets
// ToolSpec.enabled from server.enabled (checked first by tools.py authorize),
// and mcp_execution.attached_tool_definitions skips a quarantined server
// wholesale, before the per-tool approval gate is even consulted. Trusting the
// server, or setting a tool's approval override to Never, only ever clears the
// approval gate below — it cannot undo either of these.
export type McpBlockingState = "disabled" | "quarantined" | null;

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
  blocking?: McpBlockingState;
}): ApprovalPosture {
  const scopeDetail = `External tool; network access limited to ${server.host}.`;
  if (server.blocking === "disabled") {
    return {
      requiresApproval: true,
      label: "Unavailable — server off",
      detail:
        `${scopeDetail} The server is turned off, so its tools can't be ` +
        "called no matter their trust or approval setting. Turn the server " +
        "back on to make them available again.",
    };
  }
  if (server.blocking === "quarantined") {
    return {
      requiresApproval: true,
      label: "Unavailable — quarantined",
      detail:
        `${scopeDetail} The server is quarantined after repeated connection ` +
        "failures, so its tools can't be called no matter their trust or " +
        "approval setting. It recovers automatically once the server " +
        "reconnects successfully.",
    };
  }
  if (server.trusted) {
    return {
      requiresApproval: false,
      label: "Trusted for discovery",
      detail:
        `${scopeDetail} Eligible for model attachment. Invocation is governed ` +
        "separately; the default interactive policy holds external/destructive calls.",
    };
  }
  return {
    requiresApproval: true,
    label: "Withheld from model by default",
    detail:
      `${scopeDetail} This standing discovery posture leaves the tools out ` +
      "of what the model can call until the server is trusted or a tool is " +
      "individually allowed below. Invocation approval is a separate gate.",
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

// Whether standing discovery posture withholds a remote tool: `always` -> true,
// `never` -> false, `default` -> withheld unless trusted. The historical field
// name is retained for API compatibility; invocation approval is separate.
export function toolRequiresApproval(
  server: { trusted: boolean; toolApprovals?: Record<string, McpToolApproval> | null },
  toolName: string,
): boolean {
  const posture = effectiveToolApproval(server, toolName);
  if (posture === "always") return true;
  if (posture === "never") return false;
  return !server.trusted;
}

// Shared badge-copy core for a resolved posture, so a server+toolName pair
// (toolApprovalPosture) and an already-projected AttachableMcpTool
// (attachableToolApprovalPosture, below) render identical label/detail text.
// `blocking`, when set, takes priority over the approval-derived label/detail
// entirely — a disabled or quarantined server leaves the tool unavailable no
// matter what its approval posture resolves to (see McpBlockingState).
function describeToolApprovalPosture(
  posture: McpToolApproval,
  requiresApproval: boolean,
  host: string,
  blocking: McpBlockingState = null,
): ApprovalPosture & { posture: McpToolApproval } {
  const scopeDetail = `External tool; network access limited to ${host}.`;
  if (blocking === "disabled") {
    return {
      posture,
      requiresApproval: true,
      label: "Unavailable — server off",
      detail:
        `${scopeDetail} The server is turned off, so this tool can't be ` +
        "called no matter its approval setting. Turn the server back on to " +
        "make it available again.",
    };
  }
  if (blocking === "quarantined") {
    return {
      posture,
      requiresApproval: true,
      label: "Unavailable — quarantined",
      detail:
        `${scopeDetail} The server is quarantined after repeated connection ` +
        "failures, so this tool can't be called no matter its approval " +
        "setting. It recovers automatically once the server reconnects " +
        "successfully.",
    };
  }
  let label: string;
  let detail = scopeDetail;
  if (posture === "always") {
    label = "Unavailable — withheld from model";
    detail =
      `${scopeDetail} This standing posture leaves the tool out of what the ` +
      "model can call, even on a trusted server, until the override changes. " +
      "Invocation approval is a separate gate.";
  } else if (posture === "never") label = "Attachable — standing grant";
  else if (requiresApproval) {
    label = "Withheld from model";
    detail =
      `${scopeDetail} This standing discovery posture leaves the tool out of ` +
      "what the model can call unless the server is trusted or the tool is " +
      "set to Never. Invocation approval is a separate gate.";
  } else label = "Attachable — trusted discovery";
  return { posture, requiresApproval, label, detail };
}

// Compact, badge-ready posture for a single discovered tool: the resolved
// approval requirement plus a short label/detail describing why. `quarantined`
// is passed separately (rather than re-derived here) since callers already
// compute it once per server via isQuarantined/healthBadge for the health
// badge, and server.enabled is read directly off the record.
export function toolApprovalPosture(
  server: {
    trusted: boolean;
    host: string;
    enabled?: boolean;
    toolApprovals?: Record<string, McpToolApproval> | null;
  },
  toolName: string,
  quarantined = false,
): ApprovalPosture & { posture: McpToolApproval } {
  const posture = effectiveToolApproval(server, toolName);
  const requiresApproval = toolRequiresApproval(server, toolName);
  const blocking: McpBlockingState =
    server.enabled === false ? "disabled" : quarantined ? "quarantined" : null;
  return describeToolApprovalPosture(posture, requiresApproval, server.host, blocking);
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
// Coarse status precedence (quarantined > degraded > unknown > healthy). This is
// the sole owner of that ordering: the API tracks the raw failure state but no
// longer derives a coarse status server-side, so keep this authoritative.
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
  // Resolved standing discovery requirement + posture. The historical field
  // name is retained on the API contract. `true` means the tool is withheld
  // from the model; interactive invocation approval is separate.
  requiresApproval: boolean;
  approval: McpToolApproval;
  // True when the tool comes from the curated **official** plane (APIM-fronted),
  // not a user's own BYO server. Drives the read-only "official" badge and lets
  // the picker show both planes in one list.
  official: boolean;
}

// Same label/detail as toolApprovalPosture, but for a tool that's already
// been flattened into an AttachableMcpTool (e.g. the agent builder's per-tool
// checkbox list) — reuses the values it already carries instead of
// reconstructing a fake server object just to re-derive them. `quarantined` is
// a separate argument (not a field on AttachableMcpTool) because it's a
// per-server, not per-tool, health fact the caller already has from the one
// healthBadge/isQuarantined lookup it makes per server group.
export function attachableToolApprovalPosture(
  t: Pick<AttachableMcpTool, "approval" | "requiresApproval" | "host" | "enabled">,
  quarantined = false,
): ApprovalPosture & { posture: McpToolApproval } {
  const blocking: McpBlockingState = !t.enabled ? "disabled" : quarantined ? "quarantined" : null;
  return describeToolApprovalPosture(t.approval, t.requiresApproval, t.host, blocking);
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
