// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ExecutionReceipt, Message } from "@/lib/types";
import { WorkflowRunReport } from "./WorkflowRunReport";
import { deriveSteps, resultFromMessage } from "./workflowRun";

afterEach(cleanup);
const payload = (text: string) => ({ text, sha256: "a".repeat(64), bytes: text.length, truncated: false });
function receipt(overrides: Partial<ExecutionReceipt> = {}): ExecutionReceipt {
  return { version: 1, runtime: { agent: "helper", modelId: "model" }, prompt: [{ role: "user", content: payload("Read the report") }],
    promptMessageCount: 1, promptBytes: 15, contextBlocks: [], droppedHistoryMessages: 0, droppedContextBlocks: [],
    toolsOffered: [], toolsOfferedCount: 0, toolCalls: [{ tool: "classic_search", outcome: "result", approval: "run", consentId: "r-consent", arguments: payload('{"query":"weather"}'), result: payload('{"temperature":20}') }],
    toolCallCount: 1, approvalsRequested: 0, approvalsGranted: 0, autoApprovedToolCalls: 1,
    usage: { known: true, complete: true, calls: 1, promptTokens: 10, completionTokens: 5, totalTokens: 15 },
    safety: { status: "reported", coverage: ["prompt", "completion"], signalCount: 2, truncated: false },
    iterations: 1, status: "error", partial: true, truncated: false, notes: [], ...overrides };
}
function message(overrides: Partial<Message> = {}): Message {
  return { id: "m1", sessionId: "s1", userId: "u1", role: "assistant", content: "Step 2: halted", status: "error",
    model: "model", agent: "workflow:research", createdAt: "2026-09-06T00:00:00Z", executionReceipt: receipt(),
    workflowStepReceipts: [receipt()], steps: [
      { kind: "workflow_step", label: "Step 1: helper", detail: "completed" },
      { kind: "tool_result", tool: "classic_search", label: "Structured weather retrieved" },
      { kind: "workflow_error", label: "Step 2: reviewer", detail: "failed" },
    ], ...overrides };
}

describe("WorkflowRunReport execution evidence", () => {
  it("retains real tool traces, prompts, safety and usage after a later step fails", async () => {
    const result = resultFromMessage([{ label: "Helper" }, { label: "Reviewer" }], message(), false, 1000);
    render(<WorkflowRunReport result={result} pollBudgetSeconds={120} onRunAgain={vi.fn()} runAgainDisabled={false} />);
    const steps = within(screen.getByRole("list", { name: "Step results" })).getAllByRole("listitem");
    expect(steps[0]).toHaveTextContent("succeeded");
    expect(steps[1]).toHaveTextContent("Step 2: halted");
    await userEvent.click(screen.getByText("Step execution receipts · 1"));
    await userEvent.click(screen.getByText("Recorded execution 1 · @helper · partial"));
    const stepReceipt = screen.getByText("Recorded execution 1 · @helper · partial").parentElement!;
    await userEvent.click(within(stepReceipt).getByText("Runtime"));
    expect(within(stepReceipt).getByText("15 tokens across 1 model call")).toBeVisible();
    expect(within(stepReceipt).getByText(/reported · prompt \+ completion · 2 assessments/)).toBeVisible();
    await userEvent.click(within(stepReceipt).getByText("Tool calls · 1"));
    expect(within(stepReceipt).getByText(/Auto-approved for this run/)).toBeVisible();
    expect(within(stepReceipt).getByText('{"temperature":20}')).toBeInTheDocument();
    expect(within(stepReceipt).getByText("Read the report")).toBeInTheDocument();
  });

  it("treats cancellation separately from failure while retaining received evidence", () => {
    const result = resultFromMessage([{ label: "Helper" }, { label: "Reviewer" }], message({ status: "cancelled", workflowConsentRevoked: true }), false, 1000);
    expect(result.phase).toBe("cancelled");
    render(<WorkflowRunReport result={result} pollBudgetSeconds={120} onRunAgain={vi.fn()} runAgainDisabled={false} />);
    expect(screen.getByText(/Already in-flight calls may have completed/)).toBeVisible();
    expect(screen.getByText("Step execution receipts · 1")).toBeVisible();
  });

  it("renders the server's full auto-approval count instead of counting a bounded call list", async () => {
    const result = resultFromMessage([{ label: "Helper" }], message({ workflowStepReceipts: [], executionReceipt: receipt({ autoApprovedToolCalls: 13, toolCallCount: 20 }) }), false, 0);
    render(<WorkflowRunReport result={result} pollBudgetSeconds={120} onRunAgain={vi.fn()} runAgainDisabled={false} />);
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Runtime"));
    expect(screen.getByText("13 reported by the server")).toBeVisible();
  });

  it("keeps retained receipts alongside an unconfirmed result rather than erasing the report", () => {
    render(<WorkflowRunReport result={{ phase: "unknown", sessionId: "s1", error: "Polling connection lost",
      steps: deriveSteps([{ label: "Helper" }], false, "", [{ kind: "workflow_step", label: "Step 1: helper" }]),
      workflowStepReceipts: [receipt()], executionReceipt: receipt() }} pollBudgetSeconds={120} onRunAgain={vi.fn()} runAgainDisabled={false} />);
    expect(screen.getByText("outcome unknown")).toBeVisible();
    expect(screen.getByText("Step execution receipts · 1")).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("Retained evidence is shown below");
  });
  it("labels an absent historical auto-approval counter as unknown, never zero", async () => {
    const result = resultFromMessage([{ label: "Helper" }], message({ workflowStepReceipts: [], executionReceipt: receipt({
      autoApprovedToolCalls: undefined,
      toolCalls: [{ tool: "legacy_tool", outcome: "result" }],
    }) }), false, 0);
    render(<WorkflowRunReport result={result} pollBudgetSeconds={120} onRunAgain={vi.fn()} runAgainDisabled={false} />);
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Runtime"));
    expect(screen.getByText("Not recorded — unknown")).toBeVisible();
    expect(screen.queryByText("0 reported by the server")).toBeNull();
    expect(screen.getByText(/Historical approval for those calls is unknown/)).toBeVisible();
  });

  it("preserves a server-provided zero without treating it as historical per-call proof", async () => {
    const result = resultFromMessage([{ label: "Helper" }], message({ workflowStepReceipts: [], executionReceipt: receipt({
      autoApprovedToolCalls: 0,
      toolCalls: [{ tool: "legacy_tool", outcome: "result" }],
    }) }), false, 0);
    render(<WorkflowRunReport result={result} pollBudgetSeconds={120} onRunAgain={vi.fn()} runAgainDisabled={false} />);
    await userEvent.click(screen.getByText(/Execution receipt/));
    await userEvent.click(screen.getByText("Runtime"));
    expect(screen.getByText("0 reported by the server")).toBeVisible();
    expect(screen.getByText(/Historical approval for those calls is unknown/)).toBeVisible();
    await userEvent.click(screen.getByText("Tool calls · 1"));
    expect(screen.getByText("Approval provenance not recorded")).toBeVisible();
  });

});
