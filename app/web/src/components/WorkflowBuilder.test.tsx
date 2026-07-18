// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { AgentSummary, Workflow } from "@/lib/types";
import { WorkflowBuilder } from "./WorkflowBuilder";

const mocks = vi.hoisted(() => ({
  listWorkflows: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listWorkflows: mocks.listWorkflows,
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
  mocks.listWorkflows.mockResolvedValue(WORKFLOWS);
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
    expect(screen.getByRole("tooltip")).toHaveTextContent(/its own model call/i);
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
});
