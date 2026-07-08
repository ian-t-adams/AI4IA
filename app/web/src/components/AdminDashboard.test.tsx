// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";

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


const localStorageData = new Map<string, string>();
const localStorageMock = {
  getItem: vi.fn((key: string) => localStorageData.get(key) ?? null),
  setItem: vi.fn((key: string, value: string) => {
    localStorageData.set(key, value);
  }),
  removeItem: vi.fn((key: string) => {
    localStorageData.delete(key);
  }),
  clear: vi.fn(() => {
    localStorageData.clear();
  }),
};

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
  Object.defineProperty(window, "localStorage", {
    value: localStorageMock,
    configurable: true,
  });
  window.localStorage.clear();
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
  window.localStorage.clear();
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

  it("defaults to de-identified mode and fetches hash-only rows", async () => {
    vi.mocked(fetchByUser).mockResolvedValue({
      sinceDays: 30,
      fromTime: "",
      toTime: "",
      truncated: false,
      scannedRecords: 1,
      totalUsers: 1,
      limit: 20,
      offset: 0,
      byUser: [
        {
          userId: "alice-0000-1111-2222-3333",
          requests: 1,
          erroredRequests: 0,
          promptTokens: 1,
          completionTokens: 1,
          totalTokens: 2,
          costMicroUsd: 0,
          costKnown: true,
          displayName: "Ada Lovelace",
          email: "ada@example.com",
        },
      ],
    });

    render(<AdminDashboard />);

    const panel = await panelByHeading("Top users");
    expect(await within(panel).findByText("alice-00…3333")).toBeInTheDocument();
    expect(within(panel).queryByText("Ada Lovelace")).toBeNull();
    expect(within(panel).queryByText("ada@example.com")).toBeNull();
    expect(screen.getByRole("checkbox", { name: "Show real identities" })).not.toBeChecked();
    await waitFor(() => expect(fetchByUser).toHaveBeenLastCalledWith(30, 20, 0, false));
    expect(fetchUserAgents).toHaveBeenLastCalledWith(30, false);
  });

  it("refetches and persists when real identities are enabled", async () => {
    render(<AdminDashboard />);
    await panelByHeading("Top users");

    fireEvent.click(screen.getByRole("checkbox", { name: "Show real identities" }));

    await waitFor(() => expect(fetchByUser).toHaveBeenLastCalledWith(30, 20, 0, true));
    expect(fetchUserAgents).toHaveBeenLastCalledWith(30, true);
    await waitFor(() =>
      expect(window.localStorage.getItem("ai4ia.admin.showRealIdentities")).toBe("true"),
    );
  });

  it("shows the directory display name + email in Top users when identified, keeping the hash", async () => {
    window.localStorage.setItem("ai4ia.admin.showRealIdentities", "true");
    vi.mocked(fetchByUser).mockResolvedValue({
      sinceDays: 30,
      fromTime: "",
      toTime: "",
      truncated: false,
      scannedRecords: 2,
      totalUsers: 2,
      limit: 20,
      offset: 0,
      byUser: [
        {
          userId: "alice-0000-1111-2222-3333",
          requests: 2,
          erroredRequests: 0,
          promptTokens: 10,
          completionTokens: 5,
          totalTokens: 15,
          costMicroUsd: 0,
          costKnown: true,
          displayName: "Ada Lovelace",
          email: "ada@example.com",
        },
        {
          userId: "bob-0000-1111-2222-4444",
          requests: 1,
          erroredRequests: 0,
          promptTokens: 1,
          completionTokens: 1,
          totalTokens: 2,
          costMicroUsd: 0,
          costKnown: true,
          displayName: null,
          email: null,
        },
      ],
    });
    render(<AdminDashboard />);
    const panel = await panelByHeading("Top users");
    // Known user: name is primary, email is shown, and the short hash is retained.
    expect(await within(panel).findByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(panel).getByText("ada@example.com")).toBeInTheDocument();
    expect(within(panel).getByText("alice-00…3333")).toBeInTheDocument();
    // Unknown user degrades to just the short hash (no name/email line).
    expect(within(panel).getByText("bob-0000…4444")).toBeInTheDocument();
  });

  it("falls back to the short hash in Top users when no name is known", async () => {
    vi.mocked(fetchByUser).mockResolvedValue({
      sinceDays: 30,
      fromTime: "",
      toTime: "",
      truncated: false,
      scannedRecords: 1,
      totalUsers: 1,
      limit: 20,
      offset: 0,
      byUser: [
        {
          userId: "carol-0000-1111-2222-5555",
          requests: 1,
          erroredRequests: 0,
          promptTokens: 1,
          completionTokens: 1,
          totalTokens: 2,
          costMicroUsd: 0,
          costKnown: true,
        },
      ],
    });
    render(<AdminDashboard />);
    const panel = await panelByHeading("Top users");
    expect(await within(panel).findByText("carol-00…5555")).toBeInTheDocument();
  });

  it("shows the display name in the user×agent panel when identified", async () => {
    window.localStorage.setItem("ai4ia.admin.showRealIdentities", "true");
    vi.mocked(fetchUserAgents).mockResolvedValue({
      sinceDays: 30,
      truncated: false,
      scannedRecords: 3,
      userAgents: [
        {
          userId: "alice-0000-1111-2222-3333",
          agent: "research",
          requests: 2,
          totalTokens: 30,
          erroredRequests: 1,
          displayName: "Ada Lovelace",
          email: "ada@example.com",
        },
        {
          userId: "alice-0000-1111-2222-3333",
          agent: "coder",
          requests: 1,
          totalTokens: 5,
          erroredRequests: 0,
          displayName: "Ada Lovelace",
          email: "ada@example.com",
        },
      ],
    });
    render(<AdminDashboard />);
    const panel = await panelByHeading("Who uses which agents");
    // The name renders once for the grouped user, with the hash retained beneath.
    expect(await within(panel).findByText("Ada Lovelace")).toBeInTheDocument();
    expect(within(panel).getByText("ada@example.com")).toBeInTheDocument();
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
