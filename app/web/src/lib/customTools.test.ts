import { describe, expect, it } from "vitest";
import {
  approvalPosture,
  attachableMcpTools,
  isMcpToolName,
  mcpEndpointError,
  mcpSecretError,
  mcpServerNameError,
  namespacedToolName,
  parseEnabledFlag,
  parseMcpToolName,
  type UserMcpServer,
} from "./customTools";

describe("parseEnabledFlag", () => {
  it("defaults OFF for undefined / null / empty", () => {
    expect(parseEnabledFlag(undefined)).toBe(false);
    expect(parseEnabledFlag(null)).toBe(false);
    expect(parseEnabledFlag("")).toBe(false);
    expect(parseEnabledFlag("   ")).toBe(false);
  });

  it("accepts the documented truthy spellings, case/space-insensitively", () => {
    for (const v of ["1", "true", "yes", "on", "TRUE", " On ", "YeS"]) {
      expect(parseEnabledFlag(v)).toBe(true);
    }
  });

  it("treats anything else as OFF", () => {
    for (const v of ["0", "false", "no", "off", "enabled", "2"]) {
      expect(parseEnabledFlag(v)).toBe(false);
    }
  });
});

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
  it("requires approval for untrusted servers and scopes egress to the host", () => {
    const p = approvalPosture({ trusted: false, host: "api.example.com" });
    expect(p.requiresApproval).toBe(true);
    expect(p.label).toMatch(/approval/i);
    expect(p.detail).toContain("api.example.com");
  });

  it("runs without approval for trusted servers", () => {
    const p = approvalPosture({ trusted: true, host: "api.example.com" });
    expect(p.requiresApproval).toBe(false);
    expect(p.label).toMatch(/trusted/i);
    expect(p.detail).toContain("api.example.com");
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
    createdAt: "",
    updatedAt: "",
    lastConnectedAt: null,
    lastError: null,
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
});
