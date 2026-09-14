// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { Workflow } from "@/lib/types";
import { WorkflowAutomationPanel } from "./WorkflowAutomationPanel";

const mocks = vi.hoisted(() => ({
  config: vi.fn(), schedules: vi.fn(), start: vi.fn(), run: vi.fn(), save: vi.fn(), disable: vi.fn(),
}));
vi.mock("@/lib/api", () => ({
  getWorkflowAutomationConfig: mocks.config, listWorkflowSchedules: mocks.schedules,
  startResumableWorkflow: mocks.start, getAutomationRun: mocks.run,
  saveWorkflowSchedule: mocks.save, disableWorkflowSchedule: mocks.disable,
  apiErrorDetail: (reason: unknown) => reason instanceof Error ? reason.message : "Unavailable",
}));

const workflow: Workflow = {
  id: "flow", userId: "owner", name: "flow", displayName: "Report", description: "",
  enabled: true, steps: [{ agent: "general", instruction: "{input}" }],
  createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z",
};
const run = {
  runId: "owner:run", sessionId: "run-session", workflow: "Report", status: "completed",
  reason: null, revision: 2, step: 1, approval: null, deadline: "2099-01-01T01:00:00Z",
};

beforeEach(() => {
  mocks.config.mockResolvedValue({
    approvalsAvailable: true, schedulesAvailable: true, maxRuntimeSeconds: 1800, maxSchedules: 10,
  });
  mocks.schedules.mockResolvedValue({ schedules: [] });
  mocks.start.mockResolvedValue(run);
  mocks.run.mockResolvedValue(run);
  mocks.save.mockImplementation(async (body) => ({
    id: "schedule", revision: 1, generation: 1, enabled: true, status: "active",
    workflow: "Report", workflowName: "flow", model: "model", input: body.input,
    limits: body.limits, rule: body.rule, next: null, consumed: 0, tools: ["calculator"],
    history: [], approvedDigest: "a".repeat(64), effectiveDigest: "b".repeat(64),
  }));
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

async function fill() {
  const user = userEvent.setup();
  render(<WorkflowAutomationPanel workflow={workflow} model="model" documentIds={["document"]} onOpenChat={() => {}} />);
  await user.type(await screen.findByRole("textbox", { name: "Automation input" }), "Prepare the report.");
  return user;
}

it("requires explicit no-hard-dollar-cap choice and retains the original request across an ambiguous retry", async () => {
  const user = await fill();
  expect(screen.getByRole("button", { name: "Start resumable run" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /without a hard dollar cap/ }));
  mocks.start.mockRejectedValueOnce(new Error("Response lost"));
  await user.click(screen.getByRole("button", { name: "Start resumable run" }));
  await user.click(await screen.findByRole("button", { name: "Retry original run start" }));
  await waitFor(() => expect(mocks.start).toHaveBeenCalledTimes(2));
  expect(mocks.start.mock.calls[0][0]).toEqual(mocks.start.mock.calls[1][0]);
  expect(mocks.start.mock.calls[0][0]).toMatchObject({
    selection: { name: "flow", model: "model", documentIds: ["document"] },
    limits: { spendMode: "no_hard_dollar_cap", maxApplicationDispatches: 64 },
  });
});

it("writes explicit calendar semantics and lets a definite validation failure be edited", async () => {
  const user = await fill();
  await user.click(screen.getByRole("checkbox", { name: /without a hard dollar cap/ }));
  const invalid = Object.assign(new Error("Invalid local time"), { status: 422 });
  mocks.save.mockRejectedValueOnce(invalid);
  await user.click(screen.getByRole("button", { name: "Save safe schedule" }));
  await waitFor(() => expect(screen.getByRole("textbox", { name: "Automation input" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Save safe schedule" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledTimes(2));
  expect(mocks.save.mock.calls[1][0].rule).toMatchObject({
    frequency: "daily", gapPolicy: "skip", foldPolicy: "first", missedPolicy: "skip",
    overlapPolicy: "deny", maxOccurrences: 10,
  });
  expect(mocks.save.mock.calls[0][0].idempotencyKey).not.toBe(mocks.save.mock.calls[1][0].idempotencyKey);
});

it("does not offer automation when the server has disabled it", async () => {
  mocks.config.mockResolvedValueOnce({ approvalsAvailable: false, schedulesAvailable: false });
  render(<WorkflowAutomationPanel workflow={workflow} model="model" documentIds={[]} onOpenChat={() => {}} />);
  await waitFor(() => expect(mocks.config).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "Start resumable run" })).not.toBeInTheDocument();
  expect(mocks.schedules).not.toHaveBeenCalled();
});

it("edits a saved schedule with its revision and requires renewed confirmation", async () => {
  const user = await fill();
  await user.click(screen.getByRole("checkbox", { name: /without a hard dollar cap/ }));
  await user.click(screen.getByRole("button", { name: "Save safe schedule" }));
  await user.click(await screen.findByRole("button", { name: "Edit schedule" }));
  expect(screen.getByRole("checkbox", { name: /without a hard dollar cap/ })).not.toBeChecked();
  expect(screen.getByRole("button", { name: "Save revised safe schedule" })).toBeDisabled();
  await user.click(screen.getByRole("checkbox", { name: /without a hard dollar cap/ }));
  await user.click(screen.getByRole("button", { name: "Save revised safe schedule" }));
  await waitFor(() => expect(mocks.save).toHaveBeenCalledTimes(2));
  expect(mocks.save.mock.calls[1][0].expectedRevision).toBe(1);
  expect(mocks.save.mock.calls[1][1]).toBe("schedule");
});
