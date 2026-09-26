// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it } from "vitest";
import type { ExecutionReceipt, ModelCallEvidence, ReceiptCostSummary } from "@/lib/types";
import { ExecutionReceiptPanel, WorkflowStepReceiptPanels } from "./ExecutionEvidence";

afterEach(cleanup);

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
  await userEvent.click(screen.getByText("Application-effective model parameters"));
  for (const [iteration, limit] of [[1, 16384], [2, 32768]]) {
    const summary = screen.getByText(`Model call ${iteration} · responses · recorded`);
    await userEvent.click(summary);
    const panel = within(summary.parentElement!);
    expect(panel.getByText(`${limit} (max_output_tokens)`)).toBeVisible();
    expect(panel.getByText("original-prices")).toBeVisible();
    expect(panel.getByText("2 / 8 USD per million input / output tokens")).toBeVisible();
    expect(panel.getByText("session")).toBeVisible();
    expect(panel.getAllByText("Not sent (provider default unknown)").length).toBeGreaterThan(0);
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
