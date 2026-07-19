// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

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
    fetchWebSearchHealth: vi.fn(),
    fetchOperations: vi.fn(),
    fetchSecurityMetrics: vi.fn(),
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
  fetchWebSearchHealth,
  fetchOperations,
  fetchSecurityMetrics,
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
  vi.mocked(fetchWebSearchHealth).mockResolvedValue({
    enabled: true,
    authMode: "managed_identity",
    startedAt: "2024-06-01T00:00:00Z",
    generatedAt: "2024-06-30T00:00:00Z",
    totalCalls: 5,
    successes: 0,
    failures: 5,
    lastSuccessAt: null,
    lastFailureAt: "2024-06-30T00:00:00Z",
    byCategory: [{ category: "auth", count: 5 }],
    recent: [{ category: "auth", detail: "401 not entitled", at: "2024-06-30T00:00:00Z" }],
  });
  vi.mocked(fetchOperations).mockResolvedValue({
    generatedAt: "2024-06-30T00:00:00Z",
    windowMinutes: 60,
    diagnosticsUrl: "https://portal.azure.com/#resource/test/logs",
    panels: [
      {
        key: "requests",
        displayName: "Requests and route latency",
        status: "ok",
        source: "Application Insights requests",
        generatedAt: "2024-06-30T00:00:00Z",
        sourceTimestamp: "2024-06-30T00:00:00Z",
        lagSeconds: 5,
        rows: [{ route: "POST /api/chat", requests: 4, p95Ms: 120 }],
      },
    ],
  });
  vi.mocked(fetchSecurityMetrics).mockResolvedValue({
    generatedAt: "2024-06-30T00:00:00Z",
    windowMinutes: 60,
    panels: [
      {
        key: "security",
        displayName: "Security and governance blocks",
        status: "partial",
        source: "Application Insights requests/traces",
        generatedAt: "2024-06-30T00:00:00Z",
        reason: "No matching telemetry in this window.",
        rows: [],
      },
    ],
  });
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
  it("renders real operations freshness and explicit no-data states", async () => {
    render(<AdminDashboard />);
    const operations = await panelByHeading("Operations and latency");
    expect(
      await within(operations).findByText("Requests and route latency"),
    ).toBeInTheDocument();
    expect(within(operations).getByText("120")).toBeInTheDocument();
    const security = await panelByHeading("Security and governance blocks");
    expect(
      within(security).getByText(/No matching telemetry/),
    ).toBeInTheDocument();
    expect(
      within(security).getByText(/not a zero value/),
    ).toBeInTheDocument();
  });

  it("renders a partial resource panel's successful metrics and marks the failed one unavailable, not blanking the whole panel", async () => {
    // Production gap: a resource panel where one metric's own Azure Monitor
    // query failed (status "partial") used to be indistinguishable from
    // "unavailable" in the UI -- the whole panel, including metrics that DID
    // resolve, was hidden behind a bare "Unavailable — {detail}" message.
    vi.mocked(fetchResources).mockResolvedValue({
      generatedAt: "2024-06-30T00:00:00Z",
      windowMinutes: 60,
      panels: [
        {
          key: "cosmos",
          displayName: "Cosmos DB",
          status: "partial",
          detail: "Unavailable: Total requests.",
          metrics: [
            { name: "TotalRequestUnits", label: "Request units", aggregation: "total", value: 123 },
            {
              name: "TotalRequests",
              label: "Total requests",
              aggregation: "count",
              value: null,
              error: "metric query failed (BadRequest)",
              errorCode: "BadRequest",
              errorMessage: "Aggregation is unsupported.",
            },
          ],
        },
      ],
    });
    render(<AdminDashboard />);
    const panel = await panelByHeading("Platform resources");

    // The panel is not blanked behind "Unavailable" -- the metric that did
    // resolve still renders its real value.
    const requestUnitsRow = (await within(panel).findByText("Request units")).closest("li");
    expect(requestUnitsRow).not.toBeNull();
    expect(requestUnitsRow).toHaveTextContent("123");

    // The failed metric is marked unavailable inline (never a bare "—"
    // indistinguishable from legitimate no-data yet), and its safe reason is
    // only in the tooltip, never rendered as visible body text.
    const unavailableLabel = within(panel).getByText("Unavailable (BadRequest)");
    expect(unavailableLabel).toHaveStyle({ color: "var(--danger)" });
    expect(unavailableLabel).toHaveAttribute(
      "title",
      "BadRequest: Aggregation is unsupported.",
    );

    // The panel-level partial note surfaces the safe, human-readable detail.
    expect(within(panel).getByText(/^Partial/)).toBeInTheDocument();
    expect(within(panel).getByText(/Unavailable: Total requests\./)).toBeInTheDocument();
  });

  it("still hides the metrics list for a wholly unavailable resource panel", async () => {
    vi.mocked(fetchResources).mockResolvedValue({
      generatedAt: "2024-06-30T00:00:00Z",
      windowMinutes: 60,
      panels: [
        {
          key: "search",
          displayName: "Azure AI Search",
          status: "unavailable",
          detail: "Resource id not configured.",
          metrics: [],
        },
      ],
    });
    render(<AdminDashboard />);
    const panel = await panelByHeading("Platform resources");
    expect(
      within(panel).getByText(/Unavailable — Resource id not configured\./),
    ).toBeInTheDocument();
    expect(within(panel).queryByText("Partial")).toBeNull();
  });

  it("surfaces rejected data sources instead of rendering silent empties", async () => {
    vi.mocked(fetchOperations).mockRejectedValueOnce(new Error("workspace denied"));
    render(<AdminDashboard />);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Some admin data sources failed to load.",
    );
    expect(screen.getByText(/operations: workspace denied/)).toBeInTheDocument();
  });

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
    await waitFor(() =>
      expect(fetchByUser).toHaveBeenLastCalledWith(
        30,
        20,
        0,
        false,
        expect.any(AbortSignal),
      ),
    );
    expect(fetchUserAgents).toHaveBeenLastCalledWith(
      30,
      false,
      expect.any(AbortSignal),
    );
  });

  it("refetches and persists when real identities are enabled", async () => {
    render(<AdminDashboard />);
    await panelByHeading("Top users");

    fireEvent.click(screen.getByRole("checkbox", { name: "Show real identities" }));

    await waitFor(() =>
      expect(fetchByUser).toHaveBeenLastCalledWith(
        30,
        20,
        0,
        true,
        expect.any(AbortSignal),
      ),
    );
    expect(fetchUserAgents).toHaveBeenLastCalledWith(
      30,
      true,
      expect.any(AbortSignal),
    );
    await waitFor(() =>
      expect(window.localStorage.getItem("ai4ia.admin.showRealIdentities")).toBe("true"),
    );
  });

  it("does not let a slow older window overwrite the latest request", async () => {
    let resolveOld!: (value: typeof summary) => void;
    const oldRequest = new Promise<typeof summary>((resolve) => {
      resolveOld = resolve;
    });

    vi.mocked(fetchSummary).mockImplementation((days) =>
      days === 30
        ? oldRequest
        : Promise.resolve({ ...summary, sinceDays: days, activeUsers: 7 }),
    );
    render(<AdminDashboard />);
    await waitFor(() => expect(fetchSummary).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText("Window"), { target: { value: "7" } });
    const activeUsers = await screen.findByText("Active users");
    await waitFor(() =>
      expect(within(activeUsers.parentElement as HTMLElement).getByText("7")).toBeInTheDocument(),
    );
    await act(async () => {
      resolveOld({ ...summary, activeUsers: 30 });
      await oldRequest;
    });
    expect(
      within(activeUsers.parentElement as HTMLElement).queryByText("30"),
    ).toBeNull();
  });

  it("renders wholly unknown and mixed usage without fake zero totals", async () => {
    vi.mocked(fetchSummary).mockResolvedValue({
      ...summary,
      totalRequests: 4,
      totalTokens: 0,
      totalPromptTokens: 0,
      totalCompletionTokens: 0,
      unknownUsageRequests: 4,
      totalCostMicroUsd: 0,
      costUnknownRequests: 4,
    });
    vi.mocked(fetchOperations).mockResolvedValue({
      generatedAt: "2026-07-17T00:00:00Z",
      windowMinutes: 60,
      diagnosticsUrl: null,
      panels: [
        {
          key: "usage",
          displayName: "Model usage coverage",
          status: "partial",
          source: "AI4IA usage events",
          generatedAt: "2026-07-17T00:00:00Z",
          sourceTimestamp: null,
          lagSeconds: null,
          reason: "usage unknown",
          rows: [
            {
              provider: "azure_openai",
              model: "gpt-5.2",
              requests: 2,
              tokens: 0,
              knownCostUsd: 0,
              unknownUsage: 2,
              unknownCost: 2,
            },
          ],
        },
      ],
    });
    render(<AdminDashboard />);
    const tokens = (await screen.findAllByText("Tokens")).find(
      (element) => element.tagName === "DIV",
    ) as HTMLElement;
    await waitFor(() =>
      expect(
        within(tokens.parentElement as HTMLElement).getByText("Unknown"),
      ).toBeInTheDocument(),
    );
    const cost = await screen.findByText("Cost");
    expect(within(cost.parentElement as HTMLElement).getByText("Unknown")).toBeInTheDocument();
    const operations = await screen.findByText("Model usage coverage");
    expect(
      within(operations.closest("article") as HTMLElement).getAllByText("Unknown"),
    ).toHaveLength(2);

    cleanup();
    vi.mocked(fetchSummary).mockResolvedValue({
      ...summary,
      totalRequests: 4,
      totalTokens: 120,
      unknownUsageRequests: 1,
      totalCostMicroUsd: 250,
      costUnknownRequests: 1,
    });
    render(<AdminDashboard />);
    expect(await screen.findByText("Known subtotal 120")).toBeInTheDocument();
    const mixedCost = screen.getByText("Known subtotal $0.0003");
    expect(within(mixedCost.parentElement as HTMLElement).getByText("Cost")).toBeInTheDocument();
    expect(screen.getByText("3/4 requests reported")).toBeInTheDocument();
  });

  it("clears prior-window values while the latest window is loading", async () => {
    render(<AdminDashboard />);
    const activeUsers = await screen.findByText("Active users");
    await waitFor(() =>
      expect(
        within(activeUsers.parentElement as HTMLElement).getByText("2"),
      ).toBeInTheDocument(),
    );
    let resolveLatest!: (value: typeof summary) => void;
    vi.mocked(fetchSummary).mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveLatest = resolve;
        }),
    );
    fireEvent.change(screen.getByLabelText("Window"), { target: { value: "7" } });
    expect(
      await screen.findByRole("status", {
        name: "Loading dashboard data for the last 7 days",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Active users")).toBeNull();
    resolveLatest({ ...summary, sinceDays: 7, activeUsers: 7 });
    await waitFor(() =>
      expect(
        screen.queryByRole("status", {
          name: "Loading dashboard data for the last 7 days",
        }),
      ).toBeNull(),
    );
    expect(screen.getByText("7")).toBeInTheDocument();
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

  it("discloses a user's full id (and email) via focus/click, not hover-only title", async () => {
    window.localStorage.setItem("ai4ia.admin.showRealIdentities", "true");
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
      ],
    });
    const user = userEvent.setup();
    render(<AdminDashboard />);
    const panel = await panelByHeading("Top users");
    await within(panel).findByText("Ada Lovelace");
    // A real <button> (not a hover-only title/tabIndex div) is reachable by
    // keyboard and shows up in the accessibility tree unconditionally.
    const helpButton = within(panel).getByRole("button", {
      name: "Help: full id for alice-00…3333",
    });
    await user.click(helpButton);
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "Full id: alice-0000-1111-2222-3333, email: ada@example.com",
    );
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

  it("surfaces the web-search auth failure diagnosis in the health panel", async () => {
    render(<AdminDashboard />);
    const panel = await panelByHeading("Web search health");
    // The managed-identity + auth-failure smoking gun is spelled out for the operator.
    expect(await within(panel).findByText(/not entitled to Web IQ/)).toBeInTheDocument();
    // Config posture (auth mode) + the categorized recent failure detail are shown.
    expect(within(panel).getByText("Managed identity")).toBeInTheDocument();
    expect(within(panel).getByText(/401 not entitled/)).toBeInTheDocument();
  });
});
