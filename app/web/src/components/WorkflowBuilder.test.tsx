// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { AgentSummary, Workflow } from "@/lib/types";
import { WorkflowBuilder } from "./WorkflowBuilder";

const mocks = vi.hoisted(() => ({
  listWorkflows: vi.fn(),
  createSession: vi.fn(),
  runWorkflow: vi.fn(),
  getWorkflowRun: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listWorkflows: mocks.listWorkflows,
  createSession: mocks.createSession,
  runWorkflow: mocks.runWorkflow,
  getWorkflowRun: mocks.getWorkflowRun,
  // Not mocked: the real terminal-state predicate is the contract under test.
  // Stubbing it would let the component "poll to completion" against a fake
  // rule and pass while disagreeing with the API's actual statuses.
  isTerminalRunStatus: (status: string) =>
    ["COMPLETED", "FAILED", "TERMINATED"].includes(status.trim().toUpperCase()),
}));

const AGENTS: AgentSummary[] = [
  { name: "helper", displayName: "Helper", description: "Helps with quick tasks.", enabled: true },
  {
    name: "research",
    displayName: "Research Assistant",
    description: "Searches and cites sources.",
    enabled: true,
  },
];

const WORKFLOWS: Workflow[] = [
  {
    id: "w1",
    userId: "u1",
    name: "summarize",
    displayName: "Summarize",
    description: "Summarizes the input",
    steps: [{ agent: "helper", instruction: "Summarize: {input}" }],
    enabled: true,
    createdAt: "2024-01-01T00:00:00Z",
    updatedAt: "2024-01-01T00:00:00Z",
  },
];

beforeEach(() => {
  mocks.listWorkflows.mockResolvedValue({
    workflows: WORKFLOWS,
    durableAvailable: false,
  });
  mocks.createSession.mockResolvedValue({ id: "s1" });
  mocks.runWorkflow.mockResolvedValue({
    scheduled: false,
    result: { sessionId: "s1", ok: true, message: { id: "m1" } },
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("WorkflowBuilder", () => {
  it("shows the selected step agent's description and explains step chaining on demand", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);

    // The first step defaults to the first agent; its description should be
    // visible immediately (not hidden behind hover) so users can tell what
    // they're delegating to.
    expect(await screen.findByText("Helps with quick tasks.")).toBeInTheDocument();

    const select = screen.getByLabelText("Step 1 agent");
    await user.selectOptions(select, "research");
    expect(await screen.findByText("Searches and cites sources.")).toBeInTheDocument();
    expect(screen.queryByText("Helps with quick tasks.")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Help: About workflow steps" }));
    const stepsTooltip = screen.getByRole("tooltip");
    expect(stepsTooltip).toHaveTextContent(/up to three/i);
    expect(stepsTooltip).toHaveTextContent(/truncated to 8,000 characters/i);
  });

  it("explains why Run is disabled when no model is picked, without relying on a disabled-button title", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel={null} onRun={() => {}} />);
    await waitFor(() => expect(mocks.listWorkflows).toHaveBeenCalled());

    await user.click(await screen.findByRole("button", { name: "▶ Run" }));

    const runButton = await screen.findByRole("button", { name: "Run in new chat" });
    expect(runButton).toBeDisabled();
    // Native disabled-button titles aren't reliably exposed to keyboard/AT
    // users, so the reason must be visible text, not a hover-only attribute.
    expect(runButton).not.toHaveAttribute("title");
    expect(screen.getByText("Pick a model in the chat header first.")).toBeInTheDocument();
  });

  // --- Durable execution opt-in ---------------------------------------------
  //
  // These exist because the backend capability shipped with no way to reach it:
  // the run payload simply never carried `durable`, so a provisioned scheduler
  // sat idle while the feature read as "enabled" everywhere except where it is
  // actually used. A capability with no caller is indistinguishable from a
  // broken one, and nothing failed to say so.

  // Selects by accessible name, not placeholder: the builder's step-1 field
  // prompts with the {input} token, so a /input/i placeholder match is ambiguous
  // across the two columns that render side by side.
  async function openRunForm(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole("button", { name: "▶ Run" }));
    await user.type(await screen.findByLabelText(`Input for ${WORKFLOWS[0].name}`), "hello");
  }

  it("hides the durable option and never sends the flag when the server cannot honour it", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunForm(user);

    // Hidden rather than disabled: a visible-but-dead control invites the user
    // to ask for durability the deployment would answer with a 422.
    expect(
      screen.queryByLabelText(/keep running if the app restarts/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run in new chat" }));
    await waitFor(() => expect(mocks.runWorkflow).toHaveBeenCalled());
    expect(mocks.runWorkflow.mock.calls[0][1]).not.toHaveProperty("durable");
  });

  it("leaves the synchronous path untouched when durable is available but not chosen", async () => {
    mocks.listWorkflows.mockResolvedValue({
      workflows: WORKFLOWS,
      durableAvailable: true,
    });
    const onRun = vi.fn();
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={onRun} />);
    await openRunForm(user);

    expect(
      await screen.findByLabelText(/keep running if the app restarts/i),
    ).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: "Run in new chat" }));
    await waitFor(() => expect(onRun).toHaveBeenCalledWith("s1"));
    // Default OFF must mean byte-identical behaviour to before the feature
    // existed -- no flag, and no polling round-trip.
    expect(mocks.runWorkflow.mock.calls[0][1]).not.toHaveProperty("durable");
    expect(mocks.getWorkflowRun).not.toHaveBeenCalled();
  });

  it("polls a scheduled run to completion before handing off to the chat view", async () => {
    mocks.listWorkflows.mockResolvedValue({
      workflows: WORKFLOWS,
      durableAvailable: true,
    });
    mocks.runWorkflow.mockResolvedValue({
      scheduled: true,
      run: { sessionId: "s1", runId: "u1:abc", status: "accepted" },
    });
    mocks.getWorkflowRun
      .mockResolvedValueOnce({ runId: "u1:abc", status: "RUNNING" })
      .mockResolvedValueOnce({ runId: "u1:abc", status: "COMPLETED", ok: true });

    const onRun = vi.fn();
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={onRun} />);
    await openRunForm(user);
    await user.click(await screen.findByLabelText(/keep running if the app restarts/i));
    await user.click(screen.getByRole("button", { name: "Run in new chat" }));

    await waitFor(() => expect(mocks.runWorkflow).toHaveBeenCalled());
    expect(mocks.runWorkflow.mock.calls[0][1]).toMatchObject({ durable: true });
    // The chat view loads a session's messages once and does not watch for
    // later arrivals, so handing off before the run finishes would show the
    // user their own input and nothing else.
    await waitFor(() => expect(onRun).toHaveBeenCalledWith("s1"), { timeout: 10000 });
    expect(mocks.getWorkflowRun).toHaveBeenCalledTimes(2);
  }, 15000);

  it("reports a failed durable run instead of handing off as if it succeeded", async () => {
    mocks.listWorkflows.mockResolvedValue({
      workflows: WORKFLOWS,
      durableAvailable: true,
    });
    mocks.runWorkflow.mockResolvedValue({
      scheduled: true,
      run: { sessionId: "s1", runId: "u1:abc", status: "accepted" },
    });
    mocks.getWorkflowRun.mockResolvedValue({
      runId: "u1:abc",
      status: "FAILED",
      error: "step 2 timed out",
    });

    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunForm(user);
    await user.click(await screen.findByLabelText(/keep running if the app restarts/i));
    await user.click(screen.getByRole("button", { name: "Run in new chat" }));

    expect(await screen.findByText(/step 2 timed out/i, {}, { timeout: 10000 })).toBeInTheDocument();
  }, 15000);
});
