import { describe, expect, it } from "vitest";
import responseContract from "../../test-fixtures/workflow_money.json";
import {
  formatWorkflowUsd, parseWorkflowUsd, validateWorkflowBudget, validateWorkflowSpend, workflowUsdInput,
  type WorkflowBudget, type WorkflowSpendView,
} from "./workflowAutomation";

const budget: WorkflowBudget = {
  mode: "usd_app_meter", currency: "USD", budgetId: "a".repeat(64), revision: 4,
  limitMicroUsd: 1_000_000, settledMicroUsd: 200_000, heldMicroUsd: 400_000,
  unknownMicroUsd: 300_000, remainingMicroUsd: 400_000, blocked: false,
};
const local: WorkflowSpendView = {
  scope: "exact_tool_operation", status: "quoted",
  impact: {
    coverage: "bounded", amountMicroUsd: 0, currency: "USD", basis: "local-no-metered-effects-v1",
    reason: "repository-local-handler", bounds: null, localContractDigest: "b".repeat(64),
  },
  quote: null,
};
local.quote = {
  version: "workflow-approval-spend-v1", quoteDigest: "c".repeat(64), bindingDigest: "d".repeat(64),
  quotedAt: "2099-01-01T00:00:00Z", expiresAt: "2099-01-01T00:10:00Z",
  budget, impact: local.impact,
};

describe("exact workflow monetary amounts", () => {
  it.each([
    ["0", 0], ["0.000001", 1], ["1.000001", 1_000_001], ["9007199254.740991", Number.MAX_SAFE_INTEGER],
  ])("parses %s without binary floating-point rounding", (value, expected) => {
    expect(parseWorkflowUsd(value)).toBe(expected);
    expect(parseWorkflowUsd(workflowUsdInput(expected))).toBe(expected);
  });

  it.each(["", "-1", "+1", "1e3", "NaN", "Infinity", "0.0000001", "9007199254.740992", "1,000", ".1"])(
    "refuses ambiguous or unrepresentable input %s", (value) => expect(parseWorkflowUsd(value)).toBeNull(),
  );

  it("keeps unknown distinct from known zero and retains every micro-USD digit", () => {
    expect(formatWorkflowUsd(null)).toBe("Unknown");
    expect(formatWorkflowUsd(0)).toBe("USD 0.00");
    expect(formatWorkflowUsd(1)).toBe("USD 0.000001");
    expect(formatWorkflowUsd(Number.MAX_SAFE_INTEGER)).toBe("USD 9,007,199,254.740991");
    expect(() => formatWorkflowUsd(Number.MAX_SAFE_INTEGER + 1)).toThrow("Invalid");
  });
});

describe("monetary consumer guards", () => {
  it("accepts actual serialized API financial contracts and rejects missing discriminators", () => {
    for (const value of [responseContract.uncappedBudget, responseContract.cappedBudget]) {
      expect(() => validateWorkflowBudget(value)).not.toThrow();
      const missingCurrency = Object.fromEntries(Object.entries(value).filter(([key]) => key !== "currency"));
      expect(() => validateWorkflowBudget(missingCurrency)).toThrow("monetary evidence");
    }
    for (const value of [responseContract.legacySpend, responseContract.quotedSpend]) {
      expect(() => validateWorkflowSpend(value)).not.toThrow();
      const missingScope = Object.fromEntries(Object.entries(value).filter(([key]) => key !== "scope"));
      expect(() => validateWorkflowSpend(missingScope)).toThrow("monetary evidence");
      expect(() => validateWorkflowSpend({
        ...value, impact: Object.fromEntries(Object.entries(value.impact).filter(([key]) => key !== "currency")),
      })).toThrow("monetary evidence");
    }
  });
  it("accepts coherent held and unknown balances but not a lowered liability or missing field", () => {
    expect(() => validateWorkflowBudget(budget)).not.toThrow();
    for (const changed of [
      { heldMicroUsd: 0 }, { heldMicroUsd: undefined }, { unknownMicroUsd: 400_001 },
      { settledMicroUsd: -1 }, { remainingMicroUsd: 0 }, { currency: "EUR" },
    ]) expect(() => validateWorkflowBudget({ ...budget, ...changed })).toThrow("monetary evidence");
  });

  it("requires explicit unknown/null values for an uncapped run", () => {
    const uncapped: WorkflowBudget = {
      mode: "no_hard_dollar_cap", currency: "USD", budgetId: null, revision: null,
      limitMicroUsd: null, settledMicroUsd: null, heldMicroUsd: null,
      unknownMicroUsd: null, remainingMicroUsd: null, blocked: false,
    };
    expect(() => validateWorkflowBudget(uncapped)).not.toThrow();
    expect(() => validateWorkflowBudget({ ...uncapped, remainingMicroUsd: 0 })).toThrow("monetary evidence");
  });

  it("requires the full matching quote and refuses unknown-as-zero", () => {
    expect(() => validateWorkflowSpend(local)).not.toThrow();
    expect(() => validateWorkflowSpend({ ...local, quote: null })).toThrow("monetary evidence");
    expect(() => validateWorkflowSpend({
      ...local, impact: { ...local.impact, coverage: "unknown", amountMicroUsd: 0 },
    })).toThrow("monetary evidence");
    expect(() => validateWorkflowSpend({
      ...local, quote: { ...local.quote, impact: { ...local.impact, localContractDigest: "e".repeat(64) } },
    })).toThrow("monetary evidence");
  });
});
