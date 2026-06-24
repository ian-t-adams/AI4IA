// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";

import { AdminDashboard } from "./AdminDashboard";

// Keep the real pure helpers (errorLabel, groupUserAgents, statusLabel, …) and
// override only the network fetchers so we exercise the panels' real rendering.
vi.mock("@/lib/admin", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/admin")>();
  return {
    ...actual,
    fetchWhoAmI: vi.fn(),
    fetchSummary: vi.fn(),
    fetchByModel: vi.fn(),
    fetchByDay: vi.fn(),
    fetchByUser: vi.fn(),
    fetchAgents: vi.fn(),
    fetchUserAgents: vi.fn(),
    fetchDistributions: vi.fn(),
    fetchResources: vi.fn(),
  };
});

import {
  fetchAgents,
  fetchByDay,
  fetchByModel,
  fetchByUser,
  fetchDistributions,
  fetchResources,
  fetchSummary,
  fetchUserAgents,
  fetchWhoAmI,
} from "@/lib/admin";

const summary = {
  sinceDays: 30,
  fromTime: "2024-06-01T00:00:00Z",
  toTime: "2024-06-30T00:00:00Z",
  truncated: false,
  scannedRecords: 6,
  activeUsers: 2,
  totalRequests: 6,
  billableRequests: 4,
  unknownUsageRequests: 0,
  cancelledRequests: 1,
  erroredRequests: 2,
  errorRate: 0.33,
  totalPromptTokens: 30,
  totalCompletionTokens: 15,
  totalTokens: 45,
  totalCostMicroUsd: 1000,
  costUnknownRequests: 0,
  currency: "USD",
  distinctModels: 2,
  distinctAgents: 2,
};

beforeEach(() => {
  vi.mocked(fetchWhoAmI).mockResolvedValue({ subject: "alice", isAdmin: true });
  vi.mocked(fetchSummary).mockResolvedValue(summary);
  vi.mocked(fetchByModel).mockResolvedValue({ sinceDays: 30, truncated: false, scannedRecords: 6, byModel: [] });
  vi.mocked(fetchByDay).mockResolvedValue({ sinceDays: 30, truncated: false, scannedRecords: 6, byDay: [] });
  vi.mocked(fetchByUser).mockResolvedValue({
    sinceDays: 30,
    fromTime: "",
    toTime: "",
    truncated: false,
    scannedRecords: 6,
    totalUsers: 0,
    limit: 20,
    offset: 0,
    byUser: [],
  });
  vi.mocked(fetchAgents).mockResolvedValue({
    sinceDays: 30,
    truncated: false,
    scannedRecords: 6,
    agents: [
      { agent: "research", requests: 3, erroredRequests: 2, cancelledRequests: 1, totalTokens: 10, costMicroUsd: 0, users: 2 },
      { agent: "coder", requests: 1, erroredRequests: 0, cancelledRequests: 0, totalTokens: 5, costMicroUsd: 0, users: 1 },
    ],
  });
  vi.mocked(fetchUserAgents).mockResolvedValue({
    sinceDays: 30,
    truncated: false,
    scannedRecords: 6,
    userAgents: [
      { userId: "alice-0000-1111-2222-3333", agent: "research", requests: 2, totalTokens: 30, erroredRequests: 1 },
      { userId: "alice-0000-1111-2222-3333", agent: "coder", requests: 1, totalTokens: 5, erroredRequests: 0 },
    ],
  });
  vi.mocked(fetchDistributions).mockResolvedValue({
    sinceDays: 30,
    truncated: false,
    scannedRecords: 6,
    byRegion: [
      { key: "eastus", requests: 4, erroredRequests: 2, totalTokens: 30, costMicroUsd: 0, costKnown: true },
      { key: "westus", requests: 2, erroredRequests: 0, totalTokens: 15, costMicroUsd: 0, costKnown: true },
    ],
    byDataZone: [{ key: "us", requests: 6, erroredRequests: 2, totalTokens: 45, costMicroUsd: 0, costKnown: true }],
    byDeployment: [{ key: "dep-a", requests: 6, erroredRequests: 2, totalTokens: 45, costMicroUsd: 0, costKnown: true }],
    byStatus: [
      { key: "complete", requests: 3, erroredRequests: 0, totalTokens: 45, costMicroUsd: 0, costKnown: true },
      { key: "error", requests: 2, erroredRequests: 2, totalTokens: 0, costMicroUsd: 0, costKnown: true },
      { key: "cancelled", requests: 1, erroredRequests: 0, totalTokens: 0, costMicroUsd: 0, costKnown: true },
    ],
  });
  vi.mocked(fetchResources).mockResolvedValue({ generatedAt: "", windowMinutes: 5, panels: [] });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Resolve the <section> panel that owns a given heading, so assertions can be
// scoped to one panel (the `· N errors` danger suffix and dimension bars repeat
// across panels, so unscoped text queries are ambiguous by design).
async function panelByHeading(name: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name });
  const section = heading.closest("section");
  if (!section) throw new Error(`heading "${name}" is not inside a <section>`);
  return section as HTMLElement;
}

describe("AdminDashboard new analytics panels", () => {
  it("renders the agent error count in the danger colour", async () => {
    render(<AdminDashboard />);
    const agents = await panelByHeading("Agents in use");
    // research has 2 errors → danger-styled suffix; coder has 0 → no suffix.
    const errors = await within(agents).findByText(/2 errors/);
    expect(errors).toHaveStyle({ color: "var(--danger)" });
    // The zero-error agent must not get a (red) error suffix.
    expect(within(agents).queryByText(/0 errors/)).toBeNull();
  });

  it("renders the user×agent cross-tab grouped by user", async () => {
    render(<AdminDashboard />);
    const panel = await panelByHeading("Who uses which agents");
    // Both agents for alice render as rows…
    expect(await within(panel).findByText("research")).toBeInTheDocument();
    expect(within(panel).getByText("coder")).toBeInTheDocument();
    // …but the opaque id is printed once (grouped), elided via shortUserId.
    expect(within(panel).getAllByText("alice-00…3333")).toHaveLength(1);
  });

  it("renders the distribution panels with humanized labels, keys and shares", async () => {
    render(<AdminDashboard />);
    const status = await panelByHeading("Request status mix");
    // Raw status enum values are humanized for display.
    expect(await within(status).findByText("Completed")).toBeInTheDocument();
    expect(within(status).getByText("Errored")).toBeInTheDocument();
    expect(within(status).getByText("Cancelled")).toBeInTheDocument();
    // complete = 3 of 6 requests → 50% share is surfaced next to the bar.
    expect(within(status).getByText(/50%/)).toBeInTheDocument();

    // The region rollup renders its dimension keys verbatim.
    const region = await panelByHeading("Requests by region");
    expect(within(region).getByText("eastus")).toBeInTheDocument();
    expect(within(region).getByText("westus")).toBeInTheDocument();
  });
});
