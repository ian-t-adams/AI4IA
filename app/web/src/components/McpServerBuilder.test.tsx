// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { UserMcpServer } from "@/lib/customTools";
import { McpServerBuilder } from "./McpServerBuilder";

const mocks = vi.hoisted(() => ({
  listMcpServers: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listMcpServers: mocks.listMcpServers,
}));

const SERVER: UserMcpServer = {
  id: "s1",
  userId: "u1",
  name: "weather",
  displayName: "Weather",
  description: "Forecasts",
  endpoint: "https://weather.example.com/mcp",
  host: "weather.example.com",
  transport: "streamable_http",
  authMode: "none",
  trusted: false,
  enabled: true,
  secretRef: null,
  discoveredTools: [
    { name: "forecast", description: "Get a forecast", inputSchema: {} },
  ],
  toolApprovals: {},
  createdAt: "2024-01-01T00:00:00Z",
  updatedAt: "2024-01-01T00:00:00Z",
  lastConnectedAt: "2024-01-01T00:00:00Z",
  lastError: null,
  consecutiveFailures: 2,
  quarantinedUntil: null,
  lastHealthCheck: "2024-01-01T00:00:00Z",
  lastHealthError: "Connection timed out.",
};

beforeEach(() => {
  mocks.listMcpServers.mockResolvedValue([SERVER]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("McpServerBuilder", () => {
  it("surfaces the approval option's meaning and its resolved network-scoped outcome for a discovered tool", async () => {
    const user = userEvent.setup();
    render(<McpServerBuilder />);
    await waitFor(() => expect(mocks.listMcpServers).toHaveBeenCalled());

    await user.click(await screen.findByRole("button", { name: /Weather/ }));
    expect(await screen.findByText(/Get a forecast/)).toBeInTheDocument();

    // The per-tool approval <select> defaults to "default", whose meaning
    // (MCP_TOOL_APPROVALS[].hint) was previously computed but never rendered.
    await user.click(screen.getByRole("button", { name: /Approval option: Default/ }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      /no live approval prompt/i,
    );

    // The resolved outcome pill explains *why* in terms of network scope.
    await user.click(screen.getByRole("button", { name: /Resolved approval for forecast/ }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(/weather\.example\.com/);
  });

  it("explains a degraded health badge via an accessible tooltip instead of a bare hover title", async () => {
    const user = userEvent.setup();
    render(<McpServerBuilder />);
    await waitFor(() => expect(mocks.listMcpServers).toHaveBeenCalled());

    await user.click(await screen.findByRole("button", { name: /Weather/ }));
    const healthButton = await screen.findByRole("button", { name: "Help: Health: Degraded" });
    expect(healthButton.closest("span")).not.toHaveAttribute("title");

    await user.click(healthButton);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Connection timed out.");
  });
});
