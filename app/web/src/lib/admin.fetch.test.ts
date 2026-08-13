import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the same-origin proxy so the typed fetchers can be tested without a network.
vi.mock("./auth", () => ({ apiFetch: vi.fn() }));

import { fetchOverview, fetchWebSearchHealth } from "./admin";
import { apiFetch } from "./auth";

const mockApiFetch = vi.mocked(apiFetch);

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

afterEach(() => {
  mockApiFetch.mockReset();
});

describe("fetchOverview", () => {
  // Audit P1-15: one request replaces seven concurrent full ledger scans.
  const body = {
    sinceDays: 30,
    fromTime: "2024-01-01T00:00:00Z",
    toTime: "2024-01-31T00:00:00Z",
    truncated: false,
    scannedRecords: 3,
    summary: {
      sinceDays: 30,
      fromTime: "2024-01-01T00:00:00Z",
      toTime: "2024-01-31T00:00:00Z",
      truncated: false,
      scannedRecords: 3,
      activeUsers: 2,
      totalRequests: 3,
      billableRequests: 2,
      unknownUsageRequests: 1,
      cancelledRequests: 0,
      erroredRequests: 0,
      errorRate: 0,
      totalPromptTokens: 10,
      totalCompletionTokens: 5,
      totalTokens: 15,
      totalCostMicroUsd: 1000,
      costUnknownRequests: 1,
      currency: "USD",
      distinctModels: 1,
      distinctAgents: 1,
    },
    byModel: [
      { model: "gpt-5.2", requests: 3, promptTokens: 10, completionTokens: 5, totalTokens: 15, costMicroUsd: 1000, costKnown: false },
    ],
    byDay: [{ day: "2024-01-30", requests: 3, totalTokens: 15, costMicroUsd: 1000 }],
    totalUsers: 2,
    userLimit: 20,
    userOffset: 0,
    byUser: [
      { userId: "alice", requests: 2, erroredRequests: 0, promptTokens: 10, completionTokens: 5, totalTokens: 15, costMicroUsd: 1000, costKnown: false },
    ],
    agents: [
      { agent: "research", requests: 1, erroredRequests: 0, cancelledRequests: 0, totalTokens: 15, costMicroUsd: 1000, users: 1 },
    ],
    userAgents: [{ userId: "alice", agent: "research", requests: 1, totalTokens: 15, erroredRequests: 0 }],
    byRegion: [{ key: "eastus", requests: 3, erroredRequests: 0, totalTokens: 15, costMicroUsd: 1000, costKnown: false }],
    byDataZone: [],
    byProvider: [],
    byDeployment: [],
    byStatus: [{ key: "complete", requests: 3, erroredRequests: 0, totalTokens: 15, costMicroUsd: 1000, costKnown: false }],
    partialSections: [],
  };

  it("requests the consolidated endpoint with the window, paging and identity mode", async () => {
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    const out = await fetchOverview(7, 20, 0, true);

    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/admin/usage/overview?days=7&limit=20&offset=0&identify=true",
      { cache: "no-store" },
    );
    expect(out.summary.activeUsers).toBe(2);
    expect(out.byModel[0].model).toBe("gpt-5.2");
    expect(out.byUser[0].userId).toBe("alice");
    expect(out.agents[0].agent).toBe("research");
    expect(out.userAgents[0].agent).toBe("research");
    expect(out.byRegion[0].key).toBe("eastus");
    expect(out.byStatus[0].key).toBe("complete");
  });

  it("defaults to de-identified paging", async () => {
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    await fetchOverview(30);

    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/admin/usage/overview?days=30&limit=20&offset=0&identify=false",
      { cache: "no-store" },
    );
  });

  it("keeps unknown cost distinguishable from zero on every rollup", async () => {
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    const out = await fetchOverview(30);

    expect(out.summary.costUnknownRequests).toBe(1);
    expect(out.byModel[0].costKnown).toBe(false);
    expect(out.byUser[0].costKnown).toBe(false);
    expect(out.byRegion[0].costKnown).toBe(false);
  });

  it("carries the server's partial-section names so panels degrade individually", async () => {
    mockApiFetch.mockResolvedValue(jsonResponse({ ...body, agents: [], partialSections: ["agents"] }));

    const out = await fetchOverview(30);

    expect(out.partialSections).toEqual(["agents"]);
    expect(out.byModel).toHaveLength(1);
  });

  it("throws an error carrying the HTTP status on a non-ok response", async () => {
    mockApiFetch.mockResolvedValue({
      ok: false,
      status: 403,
      statusText: "Forbidden",
      json: async () => ({ detail: "admin only" }),
    } as unknown as Response);

    await expect(fetchOverview(30)).rejects.toMatchObject({ status: 403 });
  });
});

describe("fetchWebSearchHealth", () => {
  it("requests the web-search health endpoint and returns the posture + counters", async () => {
    const body = {
      enabled: true,
      authMode: "managed_identity",
      startedAt: "2024-01-01T00:00:00Z",
      generatedAt: "2024-01-01T01:00:00Z",
      totalCalls: 3,
      successes: 0,
      failures: 3,
      lastSuccessAt: null,
      lastFailureAt: "2024-01-01T01:00:00Z",
      byCategory: [{ category: "auth", count: 3 }],
      recent: [{ category: "auth", detail: "401", at: "2024-01-01T01:00:00Z" }],
    };
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    const out = await fetchWebSearchHealth();

    expect(mockApiFetch).toHaveBeenCalledWith("/api/admin/metrics/web-search", { cache: "no-store" });
    expect(out.authMode).toBe("managed_identity");
    expect(out.failures).toBe(3);
    expect(out.byCategory[0].category).toBe("auth");
  });
});
