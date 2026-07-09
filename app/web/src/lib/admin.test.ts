import { describe, expect, it } from "vitest";
import {
  barScale,
  canShowAdmin,
  dimensionShare,
  entitlementLabel,
  errorLabel,
  formatCompact,
  formatPercent,
  formatTokens,
  formatUsd,
  groupUserAgents,
  linePoints,
  microUsdToUsd,
  shortUserId,
  statusLabel,
  sumRequests,
  userLabel,
  webSearchCategoryLabel,
  webSearchHint,
  type EntitlementView,
  type UserAgentBucket,
  type WebSearchHealthReport,
} from "./admin";

describe("canShowAdmin", () => {
  it("is true only for an explicit admin identity", () => {
    expect(canShowAdmin({ subject: "a", isAdmin: true })).toBe(true);
  });
  it("is false for a non-admin, null, or undefined", () => {
    expect(canShowAdmin({ subject: "a", isAdmin: false })).toBe(false);
    expect(canShowAdmin(null)).toBe(false);
    expect(canShowAdmin(undefined)).toBe(false);
  });
});

describe("formatCompact / formatTokens", () => {
  it("passes through small numbers", () => {
    expect(formatCompact(0)).toBe("0");
    expect(formatCompact(950)).toBe("950");
    expect(formatCompact(999)).toBe("999");
  });
  it("scales to K / M / B and trims trailing .0", () => {
    expect(formatCompact(1000)).toBe("1K");
    expect(formatCompact(1234)).toBe("1.2K");
    expect(formatCompact(3_400_000)).toBe("3.4M");
    expect(formatCompact(2_000_000_000)).toBe("2B");
  });
  it("drops the decimal for large magnitudes (>=100 of a unit)", () => {
    expect(formatCompact(150_000)).toBe("150K");
  });
  it("guards null / undefined / NaN", () => {
    expect(formatTokens(null)).toBe("0");
    expect(formatTokens(undefined)).toBe("0");
    expect(formatTokens(NaN)).toBe("0");
  });
});

describe("microUsdToUsd / formatUsd", () => {
  it("converts micro-USD to USD", () => {
    expect(microUsdToUsd(1_000_000)).toBe(1);
    expect(microUsdToUsd(3500)).toBe(0.0035);
    expect(microUsdToUsd(null)).toBe(0);
  });
  it("uses 4 decimals for sub-dollar and 2 otherwise", () => {
    expect(formatUsd(3500)).toBe("$0.0035");
    expect(formatUsd(1_230_000)).toBe("$1.23");
    expect(formatUsd(0)).toBe("$0.00");
  });
  it("adds thousands separators for large totals", () => {
    expect(formatUsd(1_234_560_000)).toBe("$1,234.56");
  });
});

describe("formatPercent", () => {
  it("renders a [0,1] rate as a percent", () => {
    expect(formatPercent(0.5)).toBe("50%");
    expect(formatPercent(0.123)).toBe("12.3%");
    expect(formatPercent(0)).toBe("0%");
  });
  it("guards null / NaN", () => {
    expect(formatPercent(null)).toBe("0%");
    expect(formatPercent(NaN)).toBe("0%");
  });
});

describe("barScale", () => {
  it("scales within [0, maxPx]", () => {
    expect(barScale(5, 10, 100)).toBe(50);
    expect(barScale(10, 10, 100)).toBe(100);
  });
  it("returns 0 for non-positive value or max", () => {
    expect(barScale(0, 10, 100)).toBe(0);
    expect(barScale(5, 0, 100)).toBe(0);
    expect(barScale(-3, 10, 100)).toBe(0);
  });
  it("clamps overflow to maxPx", () => {
    expect(barScale(20, 10, 100)).toBe(100);
  });
});

describe("linePoints", () => {
  it("returns empty for no data", () => {
    expect(linePoints([], 100, 50)).toBe("");
  });
  it("places a single max point at the top-left", () => {
    expect(linePoints([5], 100, 50)).toBe("0,0");
  });
  it("normalizes to the series max with inverted y", () => {
    // max=10 -> first point y=height (bottom), last y=0 (top).
    expect(linePoints([0, 10], 100, 50)).toBe("0,50 100,0");
  });
  it("spaces x evenly across the width", () => {
    expect(linePoints([10, 10, 10], 100, 50)).toBe("0,0 50,0 100,0");
  });
});

describe("entitlementLabel", () => {
  it("treats no override as Unlimited", () => {
    expect(entitlementLabel(null)).toBe("Unlimited");
    expect(entitlementLabel(undefined)).toBe("Unlimited");
  });
  it("flags disabled and unlimited", () => {
    const disabled = { isUnlimited: false, disabled: true } as EntitlementView;
    expect(entitlementLabel(disabled)).toBe("Disabled");
    const unlimited = { isUnlimited: true, disabled: false } as EntitlementView;
    expect(entitlementLabel(unlimited)).toBe("Unlimited");
  });
  it("summarizes the configured limits compactly", () => {
    const ent = {
      isUnlimited: false,
      disabled: false,
      tokensPerDay: 5000,
      requestsPerMinute: 10,
    } as EntitlementView;
    expect(entitlementLabel(ent)).toBe("10/min, 5K tok/day");
  });
});

describe("shortUserId", () => {
  it("passes short ids through", () => {
    expect(shortUserId("abc123")).toBe("abc123");
  });
  it("elides the middle of long ids", () => {
    expect(shortUserId("bff17e95-af43-5ac4-bc00-c9bf624047c7")).toBe("bff17e95…47c7");
  });
});

describe("userLabel", () => {
  it("prefers a trimmed display name when present", () => {
    expect(userLabel("Ada Lovelace", "bff17e95-af43-5ac4-bc00-c9bf624047c7")).toBe(
      "Ada Lovelace",
    );
    expect(userLabel("  Grace Hopper  ", "x")).toBe("Grace Hopper");
  });
  it("falls back to the short hash when the name is absent or blank", () => {
    expect(userLabel(null, "bff17e95-af43-5ac4-bc00-c9bf624047c7")).toBe("bff17e95…47c7");
    expect(userLabel(undefined, "bff17e95-af43-5ac4-bc00-c9bf624047c7")).toBe("bff17e95…47c7");
    expect(userLabel("   ", "bff17e95-af43-5ac4-bc00-c9bf624047c7")).toBe("bff17e95…47c7");
  });
});

describe("errorLabel", () => {
  it("is empty for zero / null / negative so callers skip danger styling", () => {
    expect(errorLabel(0)).toBe("");
    expect(errorLabel(null)).toBe("");
    expect(errorLabel(undefined)).toBe("");
    expect(errorLabel(-3)).toBe("");
    expect(errorLabel(NaN)).toBe("");
  });
  it("renders singular vs plural and compacts large counts", () => {
    expect(errorLabel(1)).toBe("1 error");
    expect(errorLabel(4)).toBe("4 errors");
    expect(errorLabel(2500)).toBe("2.5K errors");
  });
});

describe("statusLabel", () => {
  it("humanizes the raw record status enum", () => {
    expect(statusLabel("complete")).toBe("Completed");
    expect(statusLabel("cancelled")).toBe("Cancelled");
    expect(statusLabel("error")).toBe("Errored");
  });
  it("passes unknown keys through unchanged", () => {
    expect(statusLabel("weird")).toBe("weird");
  });
});

describe("sumRequests / dimensionShare", () => {
  it("sums request counts", () => {
    expect(sumRequests([{ requests: 2 }, { requests: 3 }, { requests: 0 }])).toBe(5);
    expect(sumRequests([])).toBe(0);
  });
  it("computes share as a [0,1] rate and guards div-by-zero", () => {
    expect(dimensionShare(2, 8)).toBe(0.25);
    expect(dimensionShare(5, 0)).toBe(0);
    expect(dimensionShare(0, 8)).toBe(0);
    expect(formatPercent(dimensionShare(2, 8))).toBe("25%");
  });
});

describe("groupUserAgents", () => {
  function row(over: Partial<UserAgentBucket> & Pick<UserAgentBucket, "userId" | "agent">): UserAgentBucket {
    return { requests: 0, totalTokens: 0, erroredRequests: 0, ...over };
  }

  it("collapses cross-tab rows into per-user groups with totals", () => {
    const groups = groupUserAgents([
      row({ userId: "alice", agent: "research", totalTokens: 30, requests: 2, erroredRequests: 1 }),
      row({ userId: "alice", agent: "coder", totalTokens: 5, requests: 1 }),
      row({ userId: "bob", agent: "research", totalTokens: 100, requests: 1 }),
    ]);
    // Heaviest user first (bob 100 > alice 35).
    expect(groups.map((g) => g.userId)).toEqual(["bob", "alice"]);
    const alice = groups.find((g) => g.userId === "alice")!;
    expect(alice.totalTokens).toBe(35);
    expect(alice.totalRequests).toBe(3);
    expect(alice.erroredRequests).toBe(1);
    // Each user's rows are sorted by tokens desc.
    expect(alice.rows.map((r) => r.agent)).toEqual(["research", "coder"]);
  });

  it("returns an empty list for no rows", () => {
    expect(groupUserAgents([])).toEqual([]);
  });

  it("carries the display name/email up to the group from its rows", () => {
    const groups = groupUserAgents([
      row({ userId: "alice", agent: "research", displayName: "Ada", email: "ada@x.test" }),
      row({ userId: "alice", agent: "coder" }),
      row({ userId: "bob", agent: "research" }),
    ]);
    const alice = groups.find((g) => g.userId === "alice")!;
    expect(alice.displayName).toBe("Ada");
    expect(alice.email).toBe("ada@x.test");
    // Unknown user degrades to null -> the UI shows the short hash.
    const bob = groups.find((g) => g.userId === "bob")!;
    expect(bob.displayName ?? null).toBeNull();
    expect(bob.email ?? null).toBeNull();
  });
});

describe("webSearchCategoryLabel", () => {
  it("humanizes known categories and passes through unknown tokens", () => {
    expect(webSearchCategoryLabel("auth")).toBe("Auth");
    expect(webSearchCategoryLabel("rate_limit")).toBe("Rate limit");
    expect(webSearchCategoryLabel("something_new")).toBe("something_new");
  });
});

describe("webSearchHint", () => {
  function report(over: Partial<WebSearchHealthReport> = {}): WebSearchHealthReport {
    return {
      enabled: true,
      authMode: "api_key",
      startedAt: "2024-01-01T00:00:00Z",
      generatedAt: "2024-01-01T01:00:00Z",
      totalCalls: 0,
      successes: 0,
      failures: 0,
      lastSuccessAt: null,
      lastFailureAt: null,
      byCategory: [],
      recent: [],
      ...over,
    };
  }

  it("is info-only and unavailable when there is no report", () => {
    const h = webSearchHint(null);
    expect(h.tone).toBe("info");
    expect(h.text).toMatch(/unavailable/);
  });

  it("is info when the feature is disabled", () => {
    const h = webSearchHint(report({ enabled: false }));
    expect(h.tone).toBe("info");
    expect(h.text).toMatch(/disabled/);
  });

  it("warns when enabled but no credentials are configured", () => {
    const h = webSearchHint(report({ authMode: "unconfigured" }));
    expect(h.tone).toBe("warn");
    expect(h.text).toMatch(/no credentials/);
  });

  it("names the managed-identity entitlement gap when auth calls fail", () => {
    const h = webSearchHint(
      report({
        authMode: "managed_identity",
        totalCalls: 3,
        failures: 3,
        byCategory: [{ category: "auth", count: 3 }],
      }),
    );
    expect(h.tone).toBe("warn");
    expect(h.text).toMatch(/not entitled to Web IQ/);
  });

  it("blames the API key when auth calls fail under api_key mode", () => {
    const h = webSearchHint(
      report({
        authMode: "api_key",
        totalCalls: 2,
        failures: 2,
        byCategory: [{ category: "permission", count: 2 }],
      }),
    );
    expect(h.tone).toBe("warn");
    expect(h.text).toMatch(/API key/);
  });

  it("warns generically for non-auth failures", () => {
    const h = webSearchHint(
      report({ totalCalls: 4, failures: 1, byCategory: [{ category: "connection", count: 1 }] }),
    );
    expect(h.tone).toBe("warn");
    expect(h.text).toMatch(/some recent calls failed/);
  });

  it("is ok when enabled and configured with no failures", () => {
    expect(webSearchHint(report({ totalCalls: 0 })).tone).toBe("ok");
    expect(webSearchHint(report({ totalCalls: 5, successes: 5 })).tone).toBe("ok");
  });
});
