import { describe, expect, it } from "vitest";
import {
  barScale,
  canShowAdmin,
  entitlementLabel,
  formatCompact,
  formatPercent,
  formatTokens,
  formatUsd,
  linePoints,
  microUsdToUsd,
  shortUserId,
  type EntitlementView,
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
