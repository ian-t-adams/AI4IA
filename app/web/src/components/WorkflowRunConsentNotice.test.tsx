// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Message, WorkflowRunStatus } from "@/lib/types";
import { findWorkflowRunMessage, WorkflowRunConsentNotice, type WorkflowRunWatch } from "./WorkflowRunConsentNotice";

const mocks = vi.hoisted(() => ({ listMessages: vi.fn(), cancelWorkflowRun: vi.fn(), cancelWorkflowRunByKey: vi.fn() }));
vi.mock("@/lib/api", () => mocks);
const target: WorkflowRunWatch = { sessionId: "s1", workflowName: "research", label: "Research", autoApproveRequested: true };
function message(overrides: Partial<Message> = {}): Message {
  return { id: "m1", sessionId: "s1", userId: "u1", role: "assistant", content: "", status: "streaming",
    model: "model", agent: "workflow:research", createdAt: "2026-09-06T00:00:00Z", workflowRunId: "u1:run1",
    workflowToolConsent: { id: "consent-r1", scope: "run", toolCount: 11, grantedAt: "2026-09-06T00:00:00Z", expiresAt: "2099-09-06T08:00:00Z" },
    ...overrides };
}
beforeEach(() => { mocks.listMessages.mockResolvedValue([message()]); });
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers(); });

describe("live workflow run consent", () => {
  it("discovers a direct run's server id and revokes without abandoning the original request", async () => {
    const observed = vi.fn();
    mocks.cancelWorkflowRun.mockResolvedValue({ runId: "u1:run1", status: "TERMINATED", ok: false });
    render(<WorkflowRunConsentNotice target={target} onMessage={observed} onDismiss={vi.fn()} />);
    expect(await screen.findByText(/Auto-approval enabled for this run/)).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" }));
    expect(mocks.cancelWorkflowRun).toHaveBeenCalledWith("u1:run1", "s1");
    expect(await screen.findByText(/Run auto-approval revoked; stop requested/)).toBeVisible();
    expect(observed).toHaveBeenCalledWith(message());
    expect(screen.getByText(/Already in-flight calls may finish/)).toBeVisible();
  });

  it("lets an accepted durable run revoke before the first message poll resolves", async () => {
    mocks.listMessages.mockReturnValue(new Promise(() => {}));
    mocks.cancelWorkflowRun.mockResolvedValue({ runId: "u1:r2", status: "TERMINATED", ok: false });
    render(<WorkflowRunConsentNotice target={{ ...target, runId: "u1:r2", consent: message().workflowToolConsent }} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" }));
    expect(mocks.cancelWorkflowRun).toHaveBeenCalledWith("u1:r2", "s1");
  });

  it("does not claim a successful revoke when the server says the run already completed", async () => {
    mocks.cancelWorkflowRun.mockResolvedValue({ runId: "u1:run1", status: "COMPLETED", ok: true });
    render(<WorkflowRunConsentNotice target={target} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await screen.findByText(/Auto-approval enabled for this run/);
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" }));
    expect(await screen.findByText(/This run has finished/)).toBeVisible();
    expect(screen.queryByText(/Run auto-approval revoked/)).toBeNull();
  });

  it("preserves the enabled state and exposes retry after cancellation fails", async () => {
    mocks.cancelWorkflowRun.mockRejectedValue(new Error("409: run changed concurrently"));
    render(<WorkflowRunConsentNotice target={target} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await screen.findByText(/Auto-approval enabled for this run/);
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Consent has not been confirmed revoked");
    expect(screen.getByRole("button", { name: "Revoke auto-approval & stop run" })).toBeEnabled();
  });

  it("does not mutate a replacement run after a slow revoke resolves", async () => {
    let resolve!: (status: WorkflowRunStatus) => void;
    mocks.cancelWorkflowRun.mockImplementation(() => new Promise<WorkflowRunStatus>((done) => { resolve = done; }));
    const { rerender } = render(<WorkflowRunConsentNotice key="first" target={target} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await screen.findByText(/Auto-approval enabled for this run/);
    await userEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" }));
    mocks.listMessages.mockResolvedValue([message({ sessionId: "s2", workflowRunId: "u1:new" })]);
    rerender(<WorkflowRunConsentNotice key="second" target={{ ...target, sessionId: "s2" }} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await act(async () => resolve({ runId: "u1:run1", status: "TERMINATED", ok: false }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Revoke auto-approval & stop run" })).toBeEnabled());
    expect(screen.queryByText(/Run auto-approval revoked/)).toBeNull();
  });

  it("never borrows consent from another session or ambiguous invocation", () => {
    expect(findWorkflowRunMessage([message({ sessionId: "other" })], target)).toBeNull();
    expect(findWorkflowRunMessage([message(), message({ id: "m2", workflowRunId: "u1:r2" })], target)).toBeNull();
    expect(findWorkflowRunMessage([message(), message({ id: "m2", workflowRunId: "u1:r2" })], { ...target, runId: "u1:r2" })?.id).toBe("m2");
  });
  it("does not promote a requested run or accepted run id into proof of auto-approval", async () => {
    mocks.listMessages.mockResolvedValue([]);
    render(<WorkflowRunConsentNotice target={{ ...target, runId: "u1:requested", consent: null }} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalled());
    expect(screen.getByText(/Auto-approval requested; waiting for the server's run record/)).toBeVisible();
    expect(screen.queryByText(/Auto-approval enabled for this run/)).toBeNull();
    expect(screen.queryByText(/enabled tool contracts in this consent/)).toBeNull();
  });

  it("keeps cancellation explicit through the bounded evidence window and supports a later refresh", async () => {
    vi.useFakeTimers();
    const observed = vi.fn();
    const cancelled = message({ status: "cancelled", workflowConsentRevoked: true, content: "Early cancellation checkpoint" });
    mocks.listMessages.mockResolvedValue([cancelled]);
    render(<WorkflowRunConsentNotice target={{ ...target, runId: "u1:run1", cancellationAcknowledged: true }} onMessage={observed} onDismiss={vi.fn()} />);
    await act(() => vi.advanceTimersByTimeAsync(120_000));
    expect(screen.getByRole("alert")).toHaveTextContent("Execution evidence monitoring paused");
    expect(screen.getByText(/Run auto-approval revoked; stop requested/)).toBeVisible();
    expect(screen.queryByText(/This run has finished/)).toBeNull();
    const readsAtPause = mocks.listMessages.mock.calls.length;
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    expect(mocks.listMessages).toHaveBeenCalledTimes(readsAtPause);

    const late = { ...cancelled, content: "Late evidence after cancellation" };
    mocks.listMessages.mockResolvedValue([late]);
    fireEvent.click(screen.getByRole("button", { name: "Refresh execution evidence" }));
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(observed).toHaveBeenLastCalledWith(late);
    expect(screen.getByText("Monitoring execution evidence after cancellation…")).toBeVisible();
    expect(screen.queryByText(/This run has finished/)).toBeNull();
  });

  it("retries an early by-key 404 while start is pending, without claiming cancellation", async () => {
    vi.useFakeTimers();
    mocks.listMessages.mockResolvedValue([]);
    let confirm!: (status: WorkflowRunStatus) => void;
    mocks.cancelWorkflowRunByKey
      .mockRejectedValueOnce(Object.assign(new Error("Run is not active yet"), { status: 404 }))
      .mockImplementationOnce(() => new Promise<WorkflowRunStatus>((resolve) => { confirm = resolve; }));
    render(<WorkflowRunConsentNotice target={{ ...target, mode: "direct", idempotencyKey: "direct-key", startPending: true }} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" })); });
    expect(screen.getByText(/cancellation is not confirmed yet/)).toBeVisible();
    expect(screen.queryByText(/Run cancellation acknowledged/)).toBeNull();
    expect(mocks.cancelWorkflowRunByKey).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(500));
    expect(mocks.cancelWorkflowRunByKey).toHaveBeenCalledTimes(2);
    expect(mocks.cancelWorkflowRunByKey.mock.calls).toEqual([
      ["research", "s1", "direct-key"], ["research", "s1", "direct-key"],
    ]);
    expect(mocks.cancelWorkflowRun).not.toHaveBeenCalled();
    expect(screen.queryByText(/Run cancellation acknowledged/)).toBeNull();
    await act(async () => confirm({ runId: "u1:direct", status: "TERMINATED", ok: false }));
    expect(screen.getByText(/Run cancellation acknowledged/)).toBeVisible();
    expect(screen.getByText("Monitoring execution evidence after cancellation…")).toBeVisible();
  });

  it("stops pre-claim cancellation retries when the matching start request settles", async () => {
    vi.useFakeTimers();
    mocks.listMessages.mockResolvedValue([]);
    mocks.cancelWorkflowRunByKey.mockRejectedValue(Object.assign(new Error("Run is not active yet"), { status: 404 }));
    const direct: WorkflowRunWatch = { ...target, mode: "direct", idempotencyKey: "direct-key", startPending: true };
    const { rerender } = render(<WorkflowRunConsentNotice target={direct} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" })); });
    rerender(<WorkflowRunConsentNotice target={{ ...direct, startPending: false }} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(mocks.cancelWorkflowRunByKey).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Consent has not been confirmed revoked");
    expect(screen.queryByText(/Run cancellation acknowledged/)).toBeNull();
  });

  it("cancels a pending retry timer on unmount without another cancellation request", async () => {
    vi.useFakeTimers();
    mocks.listMessages.mockResolvedValue([]);
    mocks.cancelWorkflowRunByKey.mockRejectedValue(Object.assign(new Error("Run is not active yet"), { status: 404 }));
    const { unmount } = render(<WorkflowRunConsentNotice target={{ ...target, mode: "direct", idempotencyKey: "direct-key", startPending: true }} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" })); });
    unmount();
    await act(() => vi.advanceTimersByTimeAsync(1500));
    expect(mocks.cancelWorkflowRunByKey).toHaveBeenCalledTimes(1);
  });

  it("does not automatically retry non-404 cancellation failures", async () => {
    vi.useFakeTimers();
    mocks.listMessages.mockResolvedValue([]);
    mocks.cancelWorkflowRunByKey.mockRejectedValue(Object.assign(new Error("Forbidden"), { status: 403 }));
    render(<WorkflowRunConsentNotice target={{ ...target, mode: "direct", idempotencyKey: "direct-key", startPending: true }} onMessage={vi.fn()} onDismiss={vi.fn()} />);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Revoke auto-approval & stop run" })); });
    await act(() => vi.advanceTimersByTimeAsync(1000));
    expect(mocks.cancelWorkflowRunByKey).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Forbidden");
    expect(screen.queryByText(/Run cancellation acknowledged/)).toBeNull();
  });

});
