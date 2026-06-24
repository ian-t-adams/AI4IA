import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the same-origin proxy so the typed fetchers can be tested without a network.
vi.mock("./auth", () => ({ apiFetch: vi.fn() }));

import { fetchByUser, fetchDistributions, fetchUserAgents } from "./admin";
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

describe("fetchUserAgents", () => {
  it("requests the user-agents endpoint with the window and returns the body", async () => {
    const body = {
      sinceDays: 30,
      truncated: false,
      scannedRecords: 1,
      userAgents: [{ userId: "alice", agent: "research", requests: 2, totalTokens: 30, erroredRequests: 1 }],
    };
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    const out = await fetchUserAgents(30);

    expect(mockApiFetch).toHaveBeenCalledWith("/api/admin/usage/user-agents?days=30", { cache: "no-store" });
    expect(out.userAgents[0].agent).toBe("research");
  });

  it("carries the optional directory displayName/email through the typed shape", async () => {
    const body = {
      sinceDays: 30,
      truncated: false,
      scannedRecords: 1,
      userAgents: [
        {
          userId: "alice",
          agent: "research",
          requests: 2,
          totalTokens: 30,
          erroredRequests: 1,
          displayName: "Ada Lovelace",
          email: "ada@example.com",
        },
        { userId: "bob", agent: "coder", requests: 1, totalTokens: 5, erroredRequests: 0, displayName: null, email: null },
      ],
    };
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    const out = await fetchUserAgents(30);

    expect(out.userAgents[0].displayName).toBe("Ada Lovelace");
    expect(out.userAgents[0].email).toBe("ada@example.com");
    expect(out.userAgents[1].displayName).toBeNull();
  });
});

describe("fetchByUser", () => {
  it("carries the optional directory displayName/email on each row", async () => {
    const body = {
      sinceDays: 30,
      fromTime: "2024-01-01T00:00:00Z",
      toTime: "2024-01-31T00:00:00Z",
      truncated: false,
      scannedRecords: 1,
      totalUsers: 2,
      limit: 20,
      offset: 0,
      byUser: [
        {
          userId: "alice",
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
          userId: "bob",
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
    };
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    const out = await fetchByUser(30, 20, 0);

    expect(mockApiFetch).toHaveBeenCalledWith(
      "/api/admin/usage/by-user?days=30&limit=20&offset=0",
      { cache: "no-store" },
    );
    expect(out.byUser[0].displayName).toBe("Ada Lovelace");
    expect(out.byUser[0].email).toBe("ada@example.com");
    expect(out.byUser[1].displayName).toBeNull();
  });
});

describe("fetchDistributions", () => {
  it("requests the distributions endpoint and returns every rollup", async () => {
    const body = {
      sinceDays: 7,
      truncated: false,
      scannedRecords: 3,
      byRegion: [{ key: "eastus", requests: 2, erroredRequests: 1, totalTokens: 30, costMicroUsd: 0, costKnown: true }],
      byDataZone: [],
      byDeployment: [],
      byStatus: [{ key: "error", requests: 1, erroredRequests: 1, totalTokens: 0, costMicroUsd: 0, costKnown: true }],
    };
    mockApiFetch.mockResolvedValue(jsonResponse(body));

    const out = await fetchDistributions(7);

    expect(mockApiFetch).toHaveBeenCalledWith("/api/admin/usage/distributions?days=7", { cache: "no-store" });
    expect(out.byRegion[0].key).toBe("eastus");
    expect(out.byStatus[0].requests).toBe(1);
  });

  it("throws an error carrying the HTTP status on a non-ok response", async () => {
    mockApiFetch.mockResolvedValue({
      ok: false,
      status: 403,
      statusText: "Forbidden",
      json: async () => ({ detail: "admin only" }),
    } as unknown as Response);

    await expect(fetchDistributions(30)).rejects.toMatchObject({ status: 403 });
  });
});
