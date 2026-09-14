// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AutomationReview, AutomationRun } from "@/lib/workflowAutomation";
import { WorkflowApprovalInbox } from "./WorkflowApprovalInbox";

const mocks = vi.hoisted(() => ({
  list: vi.fn(), review: vi.fn(), decide: vi.fn(), cancel: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  listWorkflowApprovals: mocks.list, reviewWorkflowApproval: mocks.review,
  decideWorkflowApproval: mocks.decide, cancelAutomationRun: mocks.cancel,
  apiErrorDetail: (reason: unknown) => reason instanceof Error ? reason.message : "Unavailable",
}));

const run: AutomationRun = {
  runId: "owner:run", sessionId: "session", workflow: "Prepare report",
  status: "awaiting_approval", reason: null, revision: 2, step: 0, deadline: "2099-01-01T01:00:00Z",
  approval: {
    id: "draft-one", label: "mcp:courier/send", tool: "mcp:courier/send",
    purpose: "Send the report.", risk: "external", destination: "https://example.org/send",
    expiresAt: "2099-01-01T00:10:00Z", state: "pending", argumentsDigest: "d".repeat(64),
  },
};
const reviewed: AutomationReview = {
  ...run, revision: 3, argumentsJson: '{"body":"one\\n two","to":"owner@example.org"}',
  requestId: "challenge-one", grant: "single-use-value",
  approvedDigest: "a".repeat(64), effectiveDigest: "b".repeat(64),
  spendImpact: "Additional spend is unknown. No hard dollar cap.",
  spendEvidence: {
    scope: "exact_tool_operation", status: "legacy_unquoted", quote: null,
    impact: {
      coverage: "unknown", amountMicroUsd: null, currency: "USD", basis: "legacy-unquoted",
      reason: "legacy-unquoted", bounds: null, localContractDigest: null,
    },
  },
};

beforeEach(() => {
  mocks.list.mockResolvedValue({ runs: [run] });
  mocks.review.mockResolvedValue(reviewed);
  mocks.decide.mockImplementation(async () => {
    mocks.list.mockResolvedValue({ runs: [] });
    return { ...run, status: "running", approval: null };
  });
  mocks.cancel.mockResolvedValue({ ...run, status: "cancelled" });
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("durable workflow approval inbox", () => {
  it("loads persisted pending work and requires exact review before one-time approval", async () => {
    const user = userEvent.setup();
    render(<WorkflowApprovalInbox />);
    expect(await screen.findByText("Prepare report")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve this call and resume" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Review exact call" }));
    expect(await screen.findByText(reviewed.argumentsJson)).toBeInTheDocument();
    expect(screen.getByText(/complete argument JSON/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Approve this call and resume" }));
    await waitFor(() => expect(mocks.decide).toHaveBeenCalledExactlyOnceWith(
      run.runId, run.approval!.id, "approve",
      { requestId: reviewed.requestId, grant: reviewed.grant },
    ));
    expect(await screen.findByText(/Decision recorded/)).toBeInTheDocument();
  });

  it("denies durably without minting or sending an approval grant", async () => {
    const user = userEvent.setup();
    render(<WorkflowApprovalInbox />);
    await user.click(await screen.findByRole("button", { name: "Deny and stop" }));
    expect(mocks.review).not.toHaveBeenCalled();
    expect(mocks.decide).toHaveBeenCalledWith(run.runId, run.approval!.id, "deny", undefined);
  });

  it("does not retain the one-time secret across reload or a late response", async () => {
    const user = userEvent.setup();
    let resolve: (value: AutomationReview) => void = () => {};
    mocks.review.mockReturnValueOnce(new Promise<AutomationReview>((done) => { resolve = done; }));
    const first = render(<WorkflowApprovalInbox />);
    await user.click(await screen.findByRole("button", { name: "Review exact call" }));
    first.unmount();
    await act(async () => resolve(reviewed));
    render(<WorkflowApprovalInbox />);
    expect(await screen.findByRole("button", { name: "Review exact call" })).toBeEnabled();
    expect(screen.queryByText(reviewed.argumentsJson)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve this call and resume" })).not.toBeInTheDocument();
  });

  it("refuses expired review controls and distinguishes an unavailable list from empty", async () => {
    const user = userEvent.setup();
    mocks.review.mockResolvedValueOnce({
      ...reviewed, approval: { ...reviewed.approval, expiresAt: "2000-01-01T00:00:00Z" },
    });
    const view = render(<WorkflowApprovalInbox />);
    await user.click(await screen.findByRole("button", { name: "Review exact call" }));
    expect(await screen.findByRole("button", { name: "Approve this call and resume" })).toBeDisabled();
    expect(mocks.decide).not.toHaveBeenCalled();
    view.unmount();
    mocks.list.mockRejectedValueOnce(new Error("The approval store is unavailable."));
    render(<WorkflowApprovalInbox />);
    expect(await screen.findByRole("alert")).toHaveTextContent("unavailable");
    expect(screen.queryByText("No workflow decisions are pending.")).not.toBeInTheDocument();
  });

  it("shows unpriced exact-call impact as unknown, not zero or a price for continuation", async () => {
    const user = userEvent.setup();
    render(<WorkflowApprovalInbox />);
    await user.click(await screen.findByRole("button", { name: "Review exact call" }));
    expect(await screen.findByText("Unknown")).toBeInTheDocument();
    expect(screen.getByText(/not a zero-cost estimate/)).toBeInTheDocument();
    expect(screen.getByText(/only the stored tool operation/)).toBeInTheDocument();
    expect(screen.queryByText("USD 0.00")).not.toBeInTheDocument();
  });

  it("refreshes the quote only on explicit action and retains no old grant after failure", async () => {
    const user = userEvent.setup();
    render(<WorkflowApprovalInbox />);
    await user.click(await screen.findByRole("button", { name: "Review exact call" }));
    await screen.findByText(reviewed.argumentsJson);
    expect(mocks.review).toHaveBeenLastCalledWith(run.runId, run.approval!.id, false);
    mocks.review.mockRejectedValueOnce(new Error("The immutable budget changed."));
    await user.click(screen.getByRole("button", { name: "Refresh spend quote" }));
    expect(mocks.review).toHaveBeenLastCalledWith(run.runId, run.approval!.id, true);
    expect(await screen.findByRole("alert")).toHaveTextContent("budget changed");
    expect(screen.queryByRole("button", { name: "Approve this call and resume" })).not.toBeInTheDocument();
    expect(mocks.decide).not.toHaveBeenCalled();
  });
});
