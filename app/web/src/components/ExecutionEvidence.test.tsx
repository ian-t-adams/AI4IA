// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import type { ExecutionReceipt, ModelCallEvidence, ReceiptCostSummary } from "@/lib/types";
import { ExecutionReceiptPanel, WorkflowStepReceiptPanels } from "./ExecutionEvidence";
import tokenUsageAccounting from "../../test-fixtures/token_usage_accounting.json";

afterEach(cleanup);

it("keeps long post-call values readable instead of clipping them to a fixed character count", () => {
  const css = readFileSync(resolve("src", "app", "globals.css"), "utf8");
  const detail = css.match(/\.activity-detail\s*\{([^}]+)\}/)?.[1];
  expect(detail).toBeDefined();
  expect(detail).toMatch(/overflow-wrap:\s*anywhere/);
  expect(detail).not.toMatch(/white-space:\s*nowrap|text-overflow:\s*ellipsis|overflow:\s*hidden|max-width:\s*24ch/);
  expect(css.match(/\.activity-trace\s*\{([^}]+)\}/)?.[1]).toMatch(/font-size:\s*var\(--text-sm/);
});

it("retains full selectable payload digests rather than shortening them in the browser", () => {
  const sha256 = "0123456789abcdef".repeat(4);
  render(<ExecutionReceiptPanel receipt={receipt({
    promptMessageCount: 1, promptBytes: 12,
    prompt: [{ role: "user", content: { text: "Synthetic", sha256, bytes: 12, truncated: false } }],
  })} embedded />);
  expect(screen.getByText(`sha256 ${sha256}`)).toBeInTheDocument();
  expect(screen.queryByText(`sha256 ${sha256.slice(0, 16)}…`)).toBeNull();
});

it("keeps model-call detail beside runtime instead of adding another disclosure level", () => {
  render(<ExecutionReceiptPanel receipt={priced()} embedded />);
  const runtime = screen.getByText("Runtime").parentElement;
  const calls = screen.getByText("Model calls and parameters").parentElement;
  expect(calls?.parentElement).toBe(runtime?.parentElement);
});

it("groups absent scalar controls once without claiming unknown defaults were sent", async () => {
  render(<ExecutionReceiptPanel receipt={priced()} embedded />);
  await userEvent.click(screen.getByText("Model calls and parameters"));
  const summary = screen.getByText("Model call 1 · responses · recorded");
  await userEvent.click(summary);
  const detail = within(summary.parentElement!);
  expect(detail.getByText("Controls not sent").parentElement).toHaveTextContent(/Temperature.*Top p.*Effort setting.*provider defaults unknown/i);
  expect(detail.queryAllByText("Not sent (provider default unknown)")).toHaveLength(0);
});

it("labels saved description truncation without confusing it with browser clipping", () => {
  render(<ExecutionReceiptPanel receipt={receipt({
    toolsOfferedCount: 1,
    toolsOffered: [{
      name: "long_tool_name", description: "Retained description", parametersSha256: "schema-digest",
      descriptionBytes: 4096, descriptionSha256: "description-digest", descriptionTruncated: true,
    }],
  })} embedded />);
  expect(screen.getByText("Retained description")).toBeInTheDocument();
  expect(screen.getByText(/Description shortened.*4\.0 KB/)).toBeInTheDocument();
  expect(screen.getByText("sha256 description-digest")).toBeInTheDocument();
});

it("does not infer description completeness on historical receipts", () => {
  render(<ExecutionReceiptPanel receipt={receipt({
    toolsOfferedCount: 1,
    toolsOffered: [{ name: "old_tool", description: "Old saved description", parametersSha256: "" }],
  })} embedded />);
  expect(screen.getByText(/Older descriptions may have been shortened without a recorded marker/)).toBeInTheDocument();
  expect(screen.queryByText(/Description shortened.*original/)).toBeNull();
});

function receipt(overrides: Partial<ExecutionReceipt> = {}): ExecutionReceipt {
  return {
    version: 1, runtime: {}, prompt: [], promptMessageCount: 0, promptBytes: 0,
    contextBlocks: [], droppedHistoryMessages: 0, droppedContextBlocks: [],
    toolsOffered: [], toolsOfferedCount: 0, toolCalls: [], toolCallCount: 0,
    approvalsRequested: 0, approvalsGranted: 0, iterations: 1,
    status: "complete", partial: false, truncated: false, notes: [], ...overrides,
  };
}

function call(iteration: number, maxOutputTokens: number): ModelCallEvidence {
  return {
    iteration, scope: "application_effective", providerInternals: "unknown", api: "responses",
    modelSource: "session", parameterSource: "request", requestOverrides: ["max_tokens"],
    coverage: "recorded", parameters: { maxOutputTokens, outputTokenField: "max_output_tokens" },
    httpAttempts: 1, providerCompleted: true, usageKnown: true, usageComplete: true,
    promptTokens: 1000, completionTokens: 250,
    cost: {
      coverage: "known", estCostMicroUsd: 4000, currency: "USD",
      pricingBasis: "input_output_tokens", priceVersion: "original-prices",
      priceInputPer1M: 2, priceOutputPer1M: 8,
    },
  };
}

function priced(cost: Partial<ReceiptCostSummary> = {}): ExecutionReceipt {
  return receipt({
    runtime: { modelCalls: [call(1, 16384), call(2, 32768)], modelCallCount: 2 },
    usage: {
      known: true, complete: true, calls: 2,
      cost: {
        coverage: "known", estCostMicroUsd: 8000, currency: "USD",
        pricingBasis: "model_tokens_only", totalCalls: 2, pricedCalls: 2,
        priceVersions: ["original-prices"], priceVersionsTruncated: false, ...cost,
      },
    },
  });
}

it("labels historical parameters and cost as not recorded, never defaults or zero", async () => {
  render(<ExecutionReceiptPanel receipt={receipt()} />);
  await userEvent.click(screen.getByText(/Execution receipt/));
  await userEvent.click(screen.getByText("Runtime"));
  expect(screen.getByText("Model parameters not recorded")).toBeVisible();
  expect(screen.getByText("Cost not recorded")).toBeVisible();
  expect(screen.queryByText(/\$0/)).toBeNull();
});

it("progressively renders initial and later adapted controls and immutable price evidence", async () => {
  render(<ExecutionReceiptPanel receipt={priced()} />);
  await userEvent.click(screen.getByText(/Execution receipt/));
  await userEvent.click(screen.getByText("Runtime"));
  expect(screen.getByText("Estimated $0.0080")).toBeVisible();
  await userEvent.click(screen.getByText("Model calls and parameters"));
  for (const [iteration, limit] of [[1, 16384], [2, 32768]]) {
    const summary = screen.getByText(`Model call ${iteration} · responses · recorded`);
    await userEvent.click(summary);
    const panel = within(summary.parentElement!);
    expect(panel.getByText(`${limit} (max_output_tokens)`)).toBeVisible();
    expect(panel.getByText("original-prices")).toBeVisible();
    expect(panel.getByText("2 / 8 USD per million input / output tokens")).toBeVisible();
    expect(panel.getByText("session")).toBeVisible();
    expect(panel.getByText("Controls not sent").parentElement).toHaveTextContent("provider defaults unknown");
  }
  expect(screen.getByText(/Provider-internal values are unknown/)).toBeVisible();
});

it("distinguishes a known subtotal, unknown total, and an explicitly reported zero", async () => {
  const { rerender } = render(<ExecutionReceiptPanel receipt={priced({
    coverage: "partial", pricedCalls: 1, estCostMicroUsd: 4000,
  })} embedded />);
  expect(screen.getByText("Known subtotal $0.0040; total unknown")).toBeInTheDocument();
  rerender(<ExecutionReceiptPanel receipt={priced({
    coverage: "unknown", pricedCalls: 0, estCostMicroUsd: null,
  })} embedded />);
  expect(screen.getByText("Unknown (no priced model calls)")).toBeInTheDocument();
  expect(screen.getByText("Estimated model cost").parentElement).not.toHaveTextContent(/\$0/);
  rerender(<ExecutionReceiptPanel receipt={priced({ estCostMicroUsd: 0 })} embedded />);
  expect(screen.getByText("Estimated $0.00")).toBeInTheDocument();
  rerender(<ExecutionReceiptPanel receipt={priced({ estCostMicroUsd: 1 })} embedded />);
  expect(screen.getByText("Estimated <$0.0001")).toBeInTheDocument();
});

it.each(tokenUsageAccounting.cases)("renders truthful per-call usage and cost for $name", async (fixture) => {
  const modelCall: ModelCallEvidence = {
    ...call(1, 1024), modelId: "grok-4.7", api: "chat",
    promptTokens: fixture.usage.prompt_tokens,
    completionTokens: fixture.usage.completion_tokens,
    totalTokens: fixture.usage.total_tokens,
    cost: {
      ...call(1, 1024).cost,
      coverage: fixture.costKnown ? "known" : "unknown",
      estCostMicroUsd: fixture.costMicroUsd,
      reason: fixture.costKnown ? null : "usage_totals_mismatch",
    },
  };
  render(<ExecutionReceiptPanel receipt={receipt({
    runtime: { modelCalls: [modelCall], modelCallCount: 1 },
    usage: {
      known: true, complete: true, calls: 1, totalTokens: fixture.usage.total_tokens,
      cost: {
        coverage: fixture.costKnown ? "known" : "unknown",
        estCostMicroUsd: fixture.costMicroUsd, currency: "USD", pricingBasis: "model_tokens_only",
        totalCalls: 1, pricedCalls: Number(fixture.costKnown),
        priceVersions: ["original-prices"], priceVersionsTruncated: false,
      },
    },
  })} embedded />);
  await userEvent.click(screen.getByText("Runtime"));
  expect(screen.getByText(`${fixture.usage.total_tokens} tokens across 1 model call`)).toBeVisible();
  expect(screen.getByText("Estimated model cost").parentElement).toHaveTextContent(
    fixture.costKnown ? "Estimated <$0.0001" : "Unknown (no priced model calls)",
  );
  await userEvent.click(screen.getByText("Model calls and parameters"));
  const summary = screen.getByText("Model call 1 · chat · recorded");
  await userEvent.click(summary);
  const detail = within(summary.parentElement!);
  expect(detail.getByText(`22 input / 5 output / ${fixture.usage.total_tokens} total tokens`)).toBeVisible();
  expect(detail.getByText("Call cost").parentElement).toHaveTextContent(
    fixture.costKnown ? "Estimated <$0.0001" : "Unknown",
  );
  const warning = detail.queryByText(/Reported total differs from input \+ output/);
  if (fixture.costKnown) {
    expect(warning).toBeNull();
  } else {
    expect(warning).toBeVisible();
    expect(warning).toHaveTextContent("usage category and billed amount are unknown");
    expect(detail.getByText("Call cost").parentElement).not.toHaveTextContent(/\$/);
  }
});

it("does not derive missing per-call totals or reclassify saved historical costs", () => {
  render(<ExecutionReceiptPanel receipt={priced()} embedded />);
  expect(screen.getAllByText("1000 input / 250 output / total not recorded")).toHaveLength(2);
  expect(screen.getByText("Estimated $0.0080")).toBeInTheDocument();
  expect(screen.queryByText(/Reported total differs/)).toBeNull();
  expect(screen.queryByText(/1250 total tokens/)).toBeNull();
});

it("uses the same recorded controls and cost in delegated and workflow step receipts", () => {
  const child = priced();
  child.runtime.agent = "helper";
  child.runtime.modelCalls![0].parameterSource = "delegation_default";
  render(<>
    <ExecutionReceiptPanel receipt={receipt({ delegations: [child] })} embedded />
    <WorkflowStepReceiptPanels receipts={[child]} />
  </>);
  expect(screen.getAllByText("Estimated $0.0080")).toHaveLength(2);
  expect(screen.getAllByText("delegation defaults (no parent overrides)")).toHaveLength(2);
});

it("distinguishes the requested router from each provider-reported response model", async () => {
  const first = { ...call(1, 1024), modelId: "router-fixture",
    responseModel: { status: "reported" as const, modelId: "member-large", version: "2026-09-03" } };
  const last = { ...call(2, 1024), modelId: "router-fixture",
    responseModel: { status: "reported" as const, modelId: "member-medium", version: "2026-09-22" } };
  render(<ExecutionReceiptPanel receipt={receipt({
    runtime: { modelId: "router-fixture", modelCallCount: 2, modelCalls: [first, last] },
  })} />);
  await userEvent.click(screen.getByText(/Execution receipt/));
  await userEvent.click(screen.getByText("Runtime"));
  expect(screen.getAllByText("Requested model")[0].parentElement).toHaveTextContent("router-fixture");
  expect(screen.getAllByText("Response model")[0].parentElement).toHaveTextContent("member-medium (2026-09-22)");
  await userEvent.click(screen.getByText("Model calls and parameters"));
  for (const [iteration, name] of [[1, "member-large"], [2, "member-medium"]]) {
    const summary = screen.getByText(`Model call ${iteration} · responses · recorded`);
    await userEvent.click(summary);
    const detail = within(summary.parentElement!);
    expect(detail.getByText("Response model").parentElement).toHaveTextContent(String(name));
    expect(detail.getByText("Response model").nextElementSibling).toHaveClass("response-model-detail");
    expect(detail.getByText("Requested model").parentElement).toHaveTextContent("router-fixture");
  }
});

it("does not substitute an earlier retained model for a capped final call", () => {
  render(<ExecutionReceiptPanel receipt={receipt({
    runtime: { modelId: "router-fixture", modelCallCount: 9, modelCalls: [{
      ...call(1, 1024), responseModel: { status: "reported", modelId: "earlier-member", version: null },
    }] },
  })} embedded />);
  expect(screen.getAllByText("Response model")[0].parentElement).toHaveTextContent("Not recorded");
});

it("shows live avatar evidence with its record prefix, seconds and per-second estimate", async () => {
  render(<ExecutionReceiptPanel receipt={receipt({
    runtime: { modelId: "gpt-realtime", api: "speech" },
    notes: ["avatar_media_not_recorded"],
    avatar: {
      recordRef: "0123abcd", baseModel: "vasa-1", outputProtocol: "websocket", confirmed: true,
      billableSeconds: 95, videoFrames: 2375, endReason: "idle_timeout",
      cost: {
        known: true, estCostMicroUsd: 950_000, currency: "USD", priceVersion: "v-live",
        billingModelId: "photo-avatar-realtime-standard", basis: "second",
      },
    },
  })} />);
  await userEvent.click(screen.getByText(/Execution receipt/));
  await userEvent.click(screen.getByText("Live avatar"));
  expect(screen.getByText("Record 0123abcd · vasa-1")).toBeVisible();
  expect(screen.getByText("95 seconds, from confirmation to close")).toBeVisible();
  expect(screen.getByText("$0.95 USD at price version v-live")).toBeVisible();
  expect(screen.getByText("idle timeout")).toBeVisible();
});

it("labels an unconfirmed or unpriced avatar honestly and omits the section otherwise", async () => {
  const { rerender } = render(<ExecutionReceiptPanel receipt={receipt({
    avatar: {
      recordRef: "0123abcd", outputProtocol: "websocket", confirmed: false,
      billableSeconds: 0, videoFrames: 0, cost: null,
    },
  })} />);
  await userEvent.click(screen.getByText(/Execution receipt/));
  await userEvent.click(screen.getByText("Live avatar"));
  expect(screen.getByText("Never confirmed by the avatar service, so not metered")).toBeVisible();
  rerender(<ExecutionReceiptPanel receipt={receipt({
    avatar: {
      recordRef: "0123abcd", outputProtocol: "websocket", confirmed: true,
      billableSeconds: 1, videoFrames: 25,
      cost: {
        known: false, estCostMicroUsd: null, currency: "USD", priceVersion: null,
        billingModelId: "photo-avatar-realtime-standard", basis: "second",
      },
    },
  })} />);
  expect(screen.getByText("Unknown (no price recorded)")).toBeVisible();
  rerender(<ExecutionReceiptPanel receipt={receipt()} />);
  expect(screen.queryByText("Live avatar")).toBeNull();
});
