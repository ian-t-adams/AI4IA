import { describe, expect, it } from "vitest";
import {
  approvalPosture,
  attachableMcpTools,
  attachableToolApprovalPosture,
  effectiveToolApproval,
  healthBadge,
  healthStatus,
  isMcpToolName,
  isQuarantined,
  mcpEndpointError,
  MCP_TOOL_APPROVALS,
  mcpSecretError,
  mcpServerNameError,
  namespacedToolName,
  parseMcpToolName,
  quarantineReason,
  toolApprovalPosture,
  toolRequiresApproval,
  type UserMcpServer,
} from "./customTools";



describe("mcpServerNameError", () => {
  it("requires a name", () => {
    expect(mcpServerNameError("")).toMatch(/required/i);
  });

  it("accepts valid lowercase names with interior . _ -", () => {
    for (const n of ["a", "weather", "my_server", "svc-1", "a.b_c-d", "x9"]) {
      expect(mcpServerNameError(n)).toBeNull();
    }
  });

  it("rejects names that break the grammar", () => {
    for (const n of ["Weather", "1svc", "_svc", "-svc", "svc-", "svc.", "a b", "café"]) {
      expect(mcpServerNameError(n)).not.toBeNull();
    }
  });

  it("rejects over-long names", () => {
    expect(mcpServerNameError("a".repeat(33))).toMatch(/32/);
  });
});

describe("mcpEndpointError", () => {
  it("requires an endpoint", () => {
    expect(mcpEndpointError("")).toMatch(/required/i);
    expect(mcpEndpointError("   ")).toMatch(/required/i);
  });

  it("accepts https URLs", () => {
    expect(mcpEndpointError("https://example.com/mcp")).toBeNull();
  });

  it("rejects non-https and malformed URLs", () => {
    expect(mcpEndpointError("http://example.com/mcp")).toMatch(/https/i);
    expect(mcpEndpointError("not a url")).toMatch(/valid url/i);
  });

  it("rejects over-long URLs", () => {
    expect(mcpEndpointError("https://e.co/" + "a".repeat(2048))).toMatch(/too long/i);
  });
});

describe("mcpSecretError", () => {
  it("never requires a secret for auth mode none", () => {
    expect(mcpSecretError("none", "")).toBeNull();
    expect(mcpSecretError("none", undefined)).toBeNull();
  });

  it("requires a secret for api_key / bearer on create", () => {
    expect(mcpSecretError("api_key", "")).toMatch(/required/i);
    expect(mcpSecretError("bearer", "  ")).toMatch(/required/i);
  });

  it("allows a blank secret when reuse of the stored credential is permitted", () => {
    expect(mcpSecretError("api_key", "", true)).toBeNull();
    expect(mcpSecretError("bearer", undefined, true)).toBeNull();
  });

  it("accepts a provided secret and rejects an over-long one", () => {
    expect(mcpSecretError("api_key", "s3cr3t")).toBeNull();
    expect(mcpSecretError("api_key", "a".repeat(8193))).toMatch(/too long/i);
  });
});

describe("namespaced tool names", () => {
  it("builds mcp:<server>/<tool>", () => {
    expect(namespacedToolName("weather", "forecast")).toBe("mcp:weather/forecast");
  });

  it("detects MCP tool names", () => {
    expect(isMcpToolName("mcp:weather/forecast")).toBe(true);
    expect(isMcpToolName("web_search")).toBe(false);
  });

  it("round-trips via parseMcpToolName", () => {
    expect(parseMcpToolName("mcp:weather/forecast")).toEqual({
      server: "weather",
      tool: "forecast",
    });
  });

  it("preserves slashes inside the tool segment", () => {
    expect(parseMcpToolName("mcp:svc/a/b")).toEqual({ server: "svc", tool: "a/b" });
  });

  it("returns null for non-MCP or malformed names", () => {
    expect(parseMcpToolName("web_search")).toBeNull();
    expect(parseMcpToolName("mcp:weather")).toBeNull();
    expect(parseMcpToolName("mcp:/forecast")).toBeNull();
    expect(parseMcpToolName("mcp:weather/")).toBeNull();
  });
});

describe("approvalPosture", () => {
  it("separates untrusted discovery posture from invocation approval", () => {
    const p = approvalPosture({ trusted: false, host: "api.example.com" });
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/withheld from model/i);
    expect(p.detail).toContain("api.example.com");
    expect(p.detail).toMatch(/standing discovery posture/i);
    expect(p.detail).toMatch(/invocation approval is a separate gate/i);
  });

  it("labels trusted servers for discovery without claiming invocation approval", () => {
    const p = approvalPosture({ trusted: true, host: "api.example.com" });
    expect(p.requiresApproval).toBe(false);
    expect(p.label).toMatch(/trusted for discovery/i);
    expect(p.detail).toContain("api.example.com");
    expect(p.detail).toMatch(/default interactive policy holds external\/destructive calls/i);
  });

  it("is unavailable when the server is disabled, even if trusted", () => {
    const p = approvalPosture({ trusted: true, host: "api.example.com", blocking: "disabled" });
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/unavailable/i);
    expect(p.label).toMatch(/off/i);
    expect(p.detail).toContain("api.example.com");
    expect(p.detail).toMatch(/turned off/i);
    // Trust is beside the point here — being off blocks it regardless.
    expect(p.detail).toMatch(/no matter (their|its) trust or approval/i);
  });

  it("is unavailable when the server is quarantined, even if trusted", () => {
    const p = approvalPosture({ trusted: true, host: "api.example.com", blocking: "quarantined" });
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/unavailable/i);
    expect(p.label).toMatch(/quarantined/i);
    expect(p.detail).toMatch(/quarantined/i);
    expect(p.detail).toMatch(/recovers automatically/i);
  });
});

function makeServer(overrides: Partial<UserMcpServer> = {}): UserMcpServer {
  return {
    id: "id",
    userId: "u",
    name: "weather",
    displayName: "",
    description: "",
    endpoint: "https://example.com/mcp",
    host: "example.com",
    transport: "streamable_http",
    authMode: "none",
    trusted: false,
    enabled: true,
    secretRef: null,
    discoveredTools: [],
    toolApprovals: {},
    createdAt: "",
    updatedAt: "",
    lastConnectedAt: null,
    lastError: null,
    consecutiveFailures: 0,
    quarantinedUntil: null,
    lastHealthCheck: null,
    lastHealthError: null,
    ...overrides,
  };
}

describe("attachableMcpTools", () => {
  it("flattens discovered tools with namespaced names + posture, preserving order", () => {
    const servers = [
      makeServer({
        name: "weather",
        displayName: "Weather",
        trusted: true,
        host: "w.example.com",
        discoveredTools: [
          { name: "forecast", description: "d1", inputSchema: {} },
          { name: "alerts", description: "", inputSchema: {} },
        ],
      }),
      makeServer({
        name: "math",
        displayName: "",
        enabled: false,
        discoveredTools: [{ name: "add", description: "d2", inputSchema: {} }],
      }),
    ];
    const tools = attachableMcpTools(servers);
    expect(tools.map((t) => t.namespacedName)).toEqual([
      "mcp:weather/forecast",
      "mcp:weather/alerts",
      "mcp:math/add",
    ]);
    expect(tools[0]).toMatchObject({
      serverName: "weather",
      serverDisplayName: "Weather",
      toolName: "forecast",
      trusted: true,
      enabled: true,
      host: "w.example.com",
    });
    // falls back to server name when displayName is blank, and surfaces disabled servers
    expect(tools[2]).toMatchObject({
      serverName: "math",
      serverDisplayName: "math",
      enabled: false,
    });
  });

  it("returns an empty list when there are no discovered tools", () => {
    expect(attachableMcpTools([makeServer()])).toEqual([]);
  });

  it("tags tools with their plane: BYO by default, official when requested", () => {
    const servers = [
      makeServer({
        name: "ms-learn",
        discoveredTools: [{ name: "search", description: "", inputSchema: {} }],
      }),
    ];
    expect(attachableMcpTools(servers)[0].official).toBe(false);
    expect(attachableMcpTools(servers, { official: true })[0].official).toBe(true);
  });
});


describe("per-tool approval", () => {
  it("inherits the server posture by default", () => {
    expect(effectiveToolApproval(makeServer(), "forecast")).toBe("default");
    expect(toolRequiresApproval(makeServer({ trusted: false }), "forecast")).toBe(true);
    expect(toolRequiresApproval(makeServer({ trusted: true }), "forecast")).toBe(false);
  });

  it("honors an `always` override even on a trusted server", () => {
    const s = makeServer({ trusted: true, toolApprovals: { forecast: "always" } });
    expect(toolRequiresApproval(s, "forecast")).toBe(true);
    const p = toolApprovalPosture(s, "forecast");
    expect(p.posture).toBe("always");
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/unavailable/i);
    expect(p.label).toMatch(/withheld from model/i);
    expect(p.detail).toMatch(/standing posture/i);
    expect(p.detail).toMatch(/even on a trusted server/i);
    expect(p.detail).toMatch(/invocation approval is a separate gate/i);
  });

  it("honors a `never` override even on an untrusted server", () => {
    const s = makeServer({ trusted: false, toolApprovals: { forecast: "never" } });
    expect(toolRequiresApproval(s, "forecast")).toBe(false);
    const p = toolApprovalPosture(s, "forecast");
    expect(p.posture).toBe("never");
    expect(p.requiresApproval).toBe(false);
    expect(p.label).toMatch(/attachable/i);
    expect(p.label).toMatch(/standing grant/i);
  });

  it("resolves the default posture on an untrusted server as unavailable, not a per-use prompt", () => {
    const s = makeServer({ trusted: false });
    const p = toolApprovalPosture(s, "forecast");
    expect(p.posture).toBe("default");
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/withheld from model/i);
    expect(p.detail).toMatch(/standing discovery posture/i);
    expect(p.detail).toMatch(/invocation approval is a separate gate/i);
  });

  it("only overrides the named tool", () => {
    const s = makeServer({ trusted: false, toolApprovals: { forecast: "never" } });
    expect(toolRequiresApproval(s, "forecast")).toBe(false);
    expect(toolRequiresApproval(s, "alerts")).toBe(true);
  });

  it("threads per-tool posture into attachableMcpTools", () => {
    const servers = [
      makeServer({
        name: "weather",
        trusted: false,
        toolApprovals: { forecast: "never" },
        discoveredTools: [
          { name: "forecast", description: "", inputSchema: {} },
          { name: "alerts", description: "", inputSchema: {} },
        ],
      }),
    ];
    const tools = attachableMcpTools(servers);
    expect(tools[0]).toMatchObject({ toolName: "forecast", requiresApproval: false, approval: "never" });
    expect(tools[1]).toMatchObject({ toolName: "alerts", requiresApproval: true, approval: "default" });
  });

  it("gives attachableToolApprovalPosture the same label/detail as toolApprovalPosture, without a fake server object", () => {
    const s = makeServer({
      trusted: true,
      toolApprovals: { forecast: "always" },
      discoveredTools: [{ name: "forecast", description: "", inputSchema: {} }],
    });
    const [tool] = attachableMcpTools([s]);
    const fromTool = attachableToolApprovalPosture(tool);
    const fromServer = toolApprovalPosture(s, "forecast");
    expect(fromTool).toEqual(fromServer);
    expect(fromTool.label).toMatch(/withheld from model/i);
  });

  it("is unavailable when the server is disabled, even with a `never` override", () => {
    const s = makeServer({ enabled: false, trusted: true, toolApprovals: { forecast: "never" } });
    const p = toolApprovalPosture(s, "forecast");
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/unavailable/i);
    expect(p.label).toMatch(/off/i);
    expect(p.detail).toMatch(/turned off/i);
  });

  it("is unavailable when the server is quarantined, even with a `never` override", () => {
    const s = makeServer({ trusted: false, toolApprovals: { forecast: "never" } });
    const p = toolApprovalPosture(s, "forecast", /* quarantined */ true);
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/unavailable/i);
    expect(p.label).toMatch(/quarantined/i);
    expect(p.detail).toMatch(/quarantined/i);
  });

  it("threads the quarantined flag into attachableToolApprovalPosture identically to toolApprovalPosture", () => {
    const s = makeServer({
      trusted: true,
      toolApprovals: { forecast: "always" },
      discoveredTools: [{ name: "forecast", description: "", inputSchema: {} }],
    });
    const [tool] = attachableMcpTools([s]);
    const fromTool = attachableToolApprovalPosture(tool, true);
    const fromServer = toolApprovalPosture(s, "forecast", true);
    expect(fromTool).toEqual(fromServer);
    // Quarantine overrides even an `always` override's own "Unavailable" label.
    expect(fromTool.label).toMatch(/quarantined/i);
  });

  it("attachableToolApprovalPosture reflects a disabled server via the tool's own `enabled` field", () => {
    const s = makeServer({
      enabled: false,
      trusted: true,
      discoveredTools: [{ name: "forecast", description: "", inputSchema: {} }],
    });
    const [tool] = attachableMcpTools([s]);
    expect(tool.enabled).toBe(false);
    const p = attachableToolApprovalPosture(tool);
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/unavailable/i);
    expect(p.label).toMatch(/off/i);
  });
});

describe("MCP_TOOL_APPROVALS copy", () => {
  it("describes always as standing discovery posture, not invocation approval", () => {
    const always = MCP_TOOL_APPROVALS.find((a) => a.value === "always");
    expect(always).toBeDefined();
    // Must not claim the model/user is prompted for approval on each use.
    expect(always?.hint).not.toMatch(/prompt for approval/i);
    expect(always?.hint).not.toMatch(/on every use/i);
    expect(always?.hint).toMatch(/standing posture/i);
    expect(always?.hint).toMatch(/leaves the tool out of what the model can call/i);
    expect(always?.hint).toMatch(/separate from invocation approval/i);
  });

  it("describes default attachment separately from invocation approval", () => {
    const def = MCP_TOOL_APPROVALS.find((a) => a.value === "default");
    expect(def).toBeDefined();
    expect(def?.hint).not.toMatch(/prompt for approval/i);
    expect(def?.hint).not.toMatch(/on every use/i);
    expect(def?.hint).not.toMatch(/on each use/i);
    expect(def?.hint).toMatch(/left out of what the model can call/i);
    expect(def?.hint).toMatch(/default interactive policy/i);
    expect(def?.hint).toMatch(/fresh exact-call approval/i);
  });

  it("does not promise a per-use prompt for 'never' either", () => {
    const option = MCP_TOOL_APPROVALS.find((a) => a.value === "never");
    expect(option?.hint).not.toMatch(/prompt/i);
  });

  it("warns that 'default' and 'never' can't rescue a disabled or quarantined server", () => {
    // These are the two options a user might read as "this will make the tool
    // work" — so they must not overpromise when a server-level block applies.
    const def = MCP_TOOL_APPROVALS.find((a) => a.value === "default");
    const never = MCP_TOOL_APPROVALS.find((a) => a.value === "never");
    for (const option of [def, never]) {
      expect(option?.hint).toMatch(/turned off|disabled/i);
      expect(option?.hint).toMatch(/quarantined/i);
    }
  });
});

describe("health / quarantine", () => {
  const NOW = Date.parse("2025-01-01T12:00:00Z");

  it("reports unknown before any check, healthy after a successful connect", () => {
    expect(healthStatus(makeServer(), NOW)).toBe("unknown");
    expect(healthStatus(makeServer({ lastConnectedAt: "2025-01-01T11:00:00Z" }), NOW)).toBe(
      "healthy",
    );
  });

  it("reports degraded with failures below quarantine", () => {
    const s = makeServer({ consecutiveFailures: 1, lastHealthCheck: "2025-01-01T11:59:00Z" });
    expect(healthStatus(s, NOW)).toBe("degraded");
    expect(isQuarantined(s, NOW)).toBe(false);
  });

  it("reports quarantined while the window is in the future and recovers after", () => {
    const future = makeServer({
      consecutiveFailures: 3,
      quarantinedUntil: "2025-01-01T12:05:00Z",
      lastHealthError: "connection refused",
    });
    expect(isQuarantined(future, NOW)).toBe(true);
    expect(healthStatus(future, NOW)).toBe("quarantined");
    const reason = quarantineReason(future, NOW);
    expect(reason).toMatch(/quarantined/i);
    expect(reason).toContain("connection refused");
    expect(reason).toContain("3 failures");

    // Auto-recovery: once `now` passes the window it is no longer quarantined.
    const past = Date.parse("2025-01-01T12:06:00Z");
    expect(isQuarantined(future, past)).toBe(false);
    expect(quarantineReason(future, past)).toBeNull();
  });

  it("produces badge tone + label per status", () => {
    expect(healthBadge(makeServer(), NOW)).toMatchObject({ status: "unknown", tone: "muted" });
    expect(
      healthBadge(makeServer({ lastConnectedAt: "2025-01-01T11:00:00Z" }), NOW),
    ).toMatchObject({ status: "healthy", tone: "ok" });
    expect(
      healthBadge(makeServer({ consecutiveFailures: 2, lastHealthCheck: "x" }), NOW),
    ).toMatchObject({ status: "degraded", tone: "warn" });
    expect(
      healthBadge(
        makeServer({ consecutiveFailures: 3, quarantinedUntil: "2025-01-01T12:05:00Z" }),
        NOW,
      ),
    ).toMatchObject({ status: "quarantined", tone: "error" });
  });

  it("treats a missing/blank quarantinedUntil as not quarantined", () => {
    expect(isQuarantined(makeServer({ quarantinedUntil: null }), NOW)).toBe(false);
    expect(quarantineReason(makeServer({ quarantinedUntil: null }), NOW)).toBeNull();
  });
});
