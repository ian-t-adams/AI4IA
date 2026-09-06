// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { AgentSummary, ExecutionReceipt, Message, Workflow, WorkflowRunOutcome } from "@/lib/types";
import { WorkflowBuilder } from "./WorkflowBuilder";

// The factory returns ONLY what is listed here, so every api.* the component
// reaches on mount must appear or the first render throws "is not a function"
// and every test in the file dies on the same line.
const mocks = vi.hoisted(() => ({
  listWorkflows: vi.fn(),
  createSession: vi.fn(),
  runWorkflow: vi.fn(),
  newWorkflowRunIdempotencyKey: vi.fn(),
  getWorkflowRun: vi.fn(),
  listMessages: vi.fn(),
  cancelWorkflowRun: vi.fn(),
  cancelWorkflowRunByKey: vi.fn(),
  getToolCatalog: vi.fn(),
  listLibraryDocuments: vi.fn(),
  createWorkflow: vi.fn(),
  updateWorkflow: vi.fn(),
  deleteWorkflow: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listWorkflows: mocks.listWorkflows,
  createSession: mocks.createSession,
  runWorkflow: mocks.runWorkflow,
  newWorkflowRunIdempotencyKey: mocks.newWorkflowRunIdempotencyKey,
  getWorkflowRun: mocks.getWorkflowRun,
  listMessages: mocks.listMessages,
  cancelWorkflowRun: mocks.cancelWorkflowRun,
  cancelWorkflowRunByKey: mocks.cancelWorkflowRunByKey,
  getToolCatalog: mocks.getToolCatalog,
  listLibraryDocuments: mocks.listLibraryDocuments,
  createWorkflow: mocks.createWorkflow,
  updateWorkflow: mocks.updateWorkflow,
  deleteWorkflow: mocks.deleteWorkflow,
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
  {
    id: "w2",
    userId: "u1",
    name: "research-then-write",
    displayName: "Research then write",
    description: "Two-step",
    steps: [
      { agent: "research", instruction: "Research: {input}" },
      { agent: "helper", instruction: "Write up: {previous}" },
    ],
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
    result: { sessionId: "s1", ok: true, message: { id: "m1", content: "the summary" } },
  });
  mocks.newWorkflowRunIdempotencyKey.mockReturnValue("durable-intent-key");
  // An agent with nothing attached: the default that produced the original bug
  // report, where a workflow was asked to remember something and silently could
  // not.
  mocks.getToolCatalog.mockResolvedValue({ tools: [], inheritedTools: [] });
  mocks.listLibraryDocuments.mockResolvedValue([]);
  mocks.listMessages.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/** Selects a saved workflow and switches to the run tab. */
async function openRunTab(
  user: ReturnType<typeof userEvent.setup>,
  name = "Summarize",
) {
  await user.click(await screen.findByRole("button", { name }));
  await user.click(screen.getByRole("tab", { name: "Run & test" }));
}

describe("WorkflowBuilder", () => {
  it("requires irreversible confirmation before deleting a workflow", async () => {
    const confirmSpy = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    mocks.deleteWorkflow.mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    const remove = await screen.findByRole("button", { name: "Delete summarize" });

    await user.click(remove);
    expect(mocks.deleteWorkflow).not.toHaveBeenCalled();
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringMatching(/permanently delete workflow "Summarize".*can't be undone/i),
    );

    mocks.listWorkflows.mockResolvedValueOnce({
      workflows: WORKFLOWS.filter((workflow) => workflow.name !== "summarize"),
      durableAvailable: false,
    });
    await user.click(remove);
    await waitFor(() =>
      expect(mocks.deleteWorkflow).toHaveBeenCalledWith("summarize"),
    );
  });

  it("loads a starter template into the form without saving it", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);

    const picker = await screen.findByLabelText("Start from a template");
    await user.selectOptions(picker, "mistral-ocr-extract");

    // The form is populated from the template...
    await waitFor(() =>
      expect(screen.getByLabelText("Name")).toHaveValue("ocr-extract"),
    );
    expect(screen.getByLabelText("Display name")).toHaveValue(
      "Extract from scans (Mistral OCR)",
    );
    // ...including its steps, which is the part that makes the template useful.
    const instructions = screen
      .getAllByRole("textbox")
      .map((el) => (el as HTMLInputElement | HTMLTextAreaElement).value);
    expect(instructions.some((v) => v.includes("fetch_document"))).toBe(true);
    expect(instructions.some((v) => v.includes("{previous}"))).toBe(true);

    // Nothing is persisted until the user explicitly saves. Loading a template
    // that silently created a workflow would be a surprising side effect from
    // what reads as a preview control.
    expect(mocks.createWorkflow).not.toHaveBeenCalled();
    expect(mocks.updateWorkflow).not.toHaveBeenCalled();
  });

  it("keeps the template picker out of edit mode", async () => {
    // Control for the test above: the picker must exist somewhere, so proving
    // it is absent while editing is only meaningful because it is present when
    // creating. Loading a template over an existing workflow would silently
    // rewrite that workflow's steps under its locked name.
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);

    expect(await screen.findByLabelText("Start from a template")).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: /^Summarize/ }));
    await waitFor(() =>
      expect(screen.queryByLabelText("Start from a template")).not.toBeInTheDocument(),
    );
  });
  it("shows the selected step agent's description and explains step chaining on demand", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);

    // The first step defaults to the first agent; its description should be
    // visible immediately (not hidden behind hover) so users can tell what
    // they're delegating to.
    expect(await screen.findByText("Helps with quick tasks.")).toBeInTheDocument();
    expect(
      screen.getByText(
        /Use \{input\} for the run input and \{previous\} for the prior step's output\./,
      ),
    ).toBeInTheDocument();

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
    await openRunTab(user);

    const runButton = await screen.findByRole("button", { name: "Run" });
    expect(runButton).toBeDisabled();
    // Native disabled-button titles aren't reliably exposed to keyboard/AT
    // users, so the reason must be visible text, not a hover-only attribute.
    expect(runButton).not.toHaveAttribute("title");
    expect(screen.getByText("Pick a model in the chat header first.")).toBeInTheDocument();
  });

  // --- Capability visibility -------------------------------------------------
  //
  // The bug this answers: a workflow step's tool surface is not its agent's tool
  // list, and nothing said so. A workflow told to "remember the decisions" ran,
  // replied that it could not save anything, and was recorded as a success.

  it("warns on the step itself that memory is off, and says document reading is ambient", async () => {
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);

    const strip = await screen.findByRole("group", { name: "Step 1 capabilities" });
    expect(strip).toHaveTextContent(/Save memory · off/i);
    expect(strip).toHaveTextContent(/Recall memory · off/i);
    // Web search and document reading are ambient/conditional, never attached —
    // stating that is the whole point, because the asymmetry is invisible.
    expect(strip).toHaveTextContent(/Web search/i);
    expect(strip).toHaveTextContent(/Read documents/i);
  });

  it("routes a missing tool to the step editor, not to Agents", async () => {
    // The old remedy opened the Agents panel. That was a dead end: the memory
    // tools are gated on the STEP's effective tool list, and a curated agent's
    // tools cannot be edited in Agents at all — which is precisely why a
    // workflow could claim to save memories while saving none.
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await screen.findByRole("group", { name: "Step 1 capabilities" });

    expect(screen.queryByRole("button", { name: /attach tools/i })).not.toBeInTheDocument();
    expect(
      await screen.findByRole("checkbox", { name: "Save memory" }),
    ).toBeInTheDocument();
  });

  it("grants a tool to the step and reports it as on, without touching the agent", async () => {
    // The default fixture has no memory service, which would render the chip as
    // "store disabled" no matter what is ticked — a pass/fail that says nothing
    // about the grant. Give it a working store so the chip reflects the tool.
    mocks.getToolCatalog.mockResolvedValue({
      tools: [
        {
          name: "remember_memory",
          label: "Save memory",
          description: "Saves a short durable fact.",
          source: "synthetic",
          risk: "safe",
          requiresApproval: false,
          scopes: [],
          available: true,
          selectable: true,
        },
      ],
      inheritedTools: [],
    });
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    const strip = await screen.findByRole("group", { name: "Step 1 capabilities" });
    // Control: the chip is off BEFORE the box is ticked, so a passing assertion
    // below cannot be the default state.
    expect(strip).toHaveTextContent(/Save memory · off/i);

    const user = userEvent.setup();
    await user.click(await screen.findByRole("checkbox", { name: "Save memory" }));

    await waitFor(() =>
      expect(
        screen.getByRole("group", { name: "Step 1 capabilities" }),
      ).toHaveTextContent(/Save memory · this step/i),
    );
  });

  it("never offers a tool that cannot work inside a workflow step", async () => {
    // A checkbox for `generate_image` would save and validate cleanly and then
    // do nothing: those tools deliver through a per-turn chat attachment sink a
    // step cannot drain. Offering one would be a brand-new inert control.
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await screen.findByRole("checkbox", { name: "Save memory" });

    for (const absent of ["Generate image", "Generate video", "Process document"]) {
      expect(screen.queryByRole("checkbox", { name: absent })).not.toBeInTheDocument();
    }
    // Non-vacuity: the picker really is rendered, so the absences above mean
    // "excluded", not "nothing rendered at all".
    expect(screen.getByRole("checkbox", { name: "Calculator" })).toBeInTheDocument();
  });

  it("sends the step's tools when saving", async () => {
    mocks.createWorkflow.mockResolvedValue(WORKFLOWS[0]);
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    const user = userEvent.setup();

    await user.type(await screen.findByLabelText(/^name/i), "notes");
    // `user.type` parses "{...}" as a key descriptor, so typing the {input}
    // token silently produces the wrong text and the first-step validation
    // rejects the save. `paste` inserts the string verbatim.
    await user.click(screen.getByLabelText("Step 1 instruction"));
    await user.paste("Record the decisions in {input}");
    await user.click(screen.getByRole("checkbox", { name: "Save memory" }));
    await user.click(screen.getByRole("button", { name: /create workflow/i }));

    await waitFor(() => expect(mocks.createWorkflow).toHaveBeenCalled());
    // createWorkflow takes ONE argument: the name is merged into the body.
    const [body] = mocks.createWorkflow.mock.calls[0];
    expect(body.name).toBe("notes");
    expect(body.steps[0].extraTools).toEqual(["remember_memory"]);
  });

  it("says so when a step's agent cannot be resolved, rather than rendering an empty strip", async () => {
    // An empty capability strip and an unresolvable agent look identical, and
    // the first reads as "this step has no tools" — a wrong answer, not a
    // missing one.
    mocks.getToolCatalog.mockRejectedValue(new Error("422"));
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    expect(
      await screen.findByText(/capabilities unavailable/i),
    ).toBeInTheDocument();
  });

  // --- Results stay in place -------------------------------------------------

  it("renders the result in the panel instead of navigating away, and hands off only on request", async () => {
    const onRun = vi.fn();
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={onRun} />);
    await openRunTab(user);
    await user.type(await screen.findByLabelText("Input"), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(await screen.findByText("the summary")).toBeInTheDocument();
    // Auto-navigating unmounts this panel and takes the output with it, which is
    // why a run appeared to produce nothing at all.
    expect(onRun).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Open in chat" }));
    expect(onRun).toHaveBeenCalledWith("s1");
  });

  it("attributes a failure to the step that raised it and marks later steps as skipped", async () => {
    mocks.runWorkflow.mockResolvedValue({
      scheduled: false,
      result: {
        sessionId: "s1",
        ok: false,
        message: { id: "m1", content: "Step 2: the model refused" },
      },
    });
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user, "Research then write");
    await user.type(await screen.findByLabelText("Input"), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));

    const trace = await screen.findByRole("list", { name: "Step results" });
    const items = within(trace).getAllByRole("listitem");
    // Step 1 ran before the failure; step 2 is the one that raised it; nothing
    // after a failure ever started.
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent(/succeeded/i);
    expect(items[1]).toHaveTextContent(/failed/i);
    expect(await screen.findByText(/the model refused/i)).toBeInTheDocument();
  });

  // --- Durable execution opt-in ---------------------------------------------
  //
  // These exist because the backend capability shipped with no way to reach it:
  // the run payload simply never carried `durable`, so a provisioned scheduler
  // sat idle while the feature read as "enabled" everywhere except where it is
  // actually used. A capability with no caller is indistinguishable from a
  // broken one, and nothing failed to say so.

  it("hides the durable option and never sends the flag when the server cannot honour it", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(await screen.findByLabelText("Input"), "hello");

    // Hidden rather than disabled: a visible-but-dead control invites the user
    // to ask for durability the deployment would answer with a 422.
    expect(
      screen.queryByLabelText(/keep running if the app restarts/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(mocks.runWorkflow).toHaveBeenCalled());
    expect(mocks.runWorkflow.mock.calls[0][1]).not.toHaveProperty("durable");
    expect(mocks.runWorkflow.mock.calls[0][1]).toHaveProperty("idempotencyKey", "durable-intent-key");
  });

  it("leaves the synchronous path untouched when durable is available but not chosen", async () => {
    mocks.listWorkflows.mockResolvedValue({
      workflows: WORKFLOWS,
      durableAvailable: true,
    });
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(await screen.findByLabelText("Input"), "hello");

    expect(
      await screen.findByLabelText(/keep running if the app restarts/i),
    ).not.toBeChecked();

    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("the summary")).toBeInTheDocument();
    // Durability defaults off: the synchronous response path is preserved,
    // without scheduler polling. The invocation key still enables cancellation.
    expect(mocks.runWorkflow.mock.calls[0][1]).not.toHaveProperty("durable");
    expect(mocks.getWorkflowRun).not.toHaveBeenCalled();
  });

  it("polls a scheduled run to completion and reports it in place", async () => {
    mocks.listWorkflows.mockResolvedValue({
      workflows: WORKFLOWS,
      durableAvailable: true,
    });
    mocks.runWorkflow.mockResolvedValue({
      scheduled: true,
      run: {
        sessionId: "s1",
        runId: "u1:abc",
        status: "accepted",
        idempotencyKey: "durable-intent-key",
      },
    });
    mocks.getWorkflowRun
      .mockResolvedValueOnce({ runId: "u1:abc", status: "RUNNING" })
      .mockResolvedValueOnce({
        runId: "u1:abc",
        status: "COMPLETED",
        ok: true,
        text: "durable output",
      });

    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(await screen.findByLabelText("Input"), "hello");
    await user.click(await screen.findByLabelText(/keep running if the app restarts/i));
    await user.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(mocks.runWorkflow).toHaveBeenCalled());
    expect(mocks.runWorkflow.mock.calls[0][1]).toMatchObject({
      durable: true,
      idempotencyKey: "durable-intent-key",
    });
    expect(
      await screen.findByText("durable output", {}, { timeout: 10000 }),
    ).toBeInTheDocument();
    expect(mocks.getWorkflowRun).toHaveBeenCalledTimes(2);
  }, 15000);

  it("reports a failed durable run instead of presenting it as a success", async () => {
    mocks.listWorkflows.mockResolvedValue({
      workflows: WORKFLOWS,
      durableAvailable: true,
    });
    mocks.runWorkflow.mockResolvedValue({
      scheduled: true,
      run: {
        sessionId: "s1",
        runId: "u1:abc",
        status: "accepted",
        idempotencyKey: "durable-intent-key",
      },
    });
    mocks.getWorkflowRun.mockResolvedValue({
      runId: "u1:abc",
      status: "FAILED",
      error: "step 2 timed out",
    });

    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(await screen.findByLabelText("Input"), "hello");
    await user.click(await screen.findByLabelText(/keep running if the app restarts/i));
    await user.click(screen.getByRole("button", { name: "Run" }));

    expect(
      await screen.findByText(/step 2 timed out/i, {}, { timeout: 10000 }),
    ).toBeInTheDocument();
  }, 15000);

  it("scopes a run to the selected documents, and omits the key entirely when none are chosen", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(await screen.findByLabelText("Input"), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));

    await waitFor(() => expect(mocks.createSession).toHaveBeenCalled());
    // `[]` is NOT "no preference": the API reads `allowed_document_ids is None
    // or bool(...)`, so sending an empty array switches document reading OFF
    // for the whole run.
    expect(mocks.createSession.mock.calls[0][0]).not.toHaveProperty("libraryDocumentIds");
  });

  // --- Save & run runs what was just saved -----------------------------------

  it("runs the definition it just saved, not the copy it loaded", async () => {
    // The bug: `doRun` looked the workflow up by name in `mine`, which its
    // closure captured BEFORE the save. Editing a workflow and hitting
    // "Save & run" therefore executed the PREVIOUS definition while the result
    // card described the new one — a silently wrong run reported as a success.
    const saved: Workflow = {
      ...WORKFLOWS[0],
      steps: [
        { agent: "helper", instruction: "Summarize: {input}" },
        { agent: "research", instruction: "Fact-check: {previous}" },
      ],
    };
    mocks.updateWorkflow.mockResolvedValue(saved);
    // The server list deliberately keeps returning the ONE-step version, which
    // is what a save followed by a lagging refresh looks like. Without it the
    // assertion could pass on a re-render rather than on the fix.
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(await screen.findByLabelText("Input"), "hello");
    await user.click(screen.getByRole("button", { name: "Save & run" }));

    await waitFor(() => expect(mocks.updateWorkflow).toHaveBeenCalled());
    const trace = await screen.findByRole("list", { name: "Step results" });
    const items = within(trace).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[1]).toHaveTextContent("Research Assistant");
  });
});


describe("per-invocation workflow auto-approval", () => {
  it("hides the opt-in when unavailable and sends no true consent", async () => {
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    expect(screen.queryByRole("checkbox", { name: "Auto-approve enabled tools for this run" })).toBeNull();
    await user.type(screen.getByRole("textbox", { name: /^Input/ }), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(mocks.runWorkflow).toHaveBeenCalled());
    expect(mocks.runWorkflow.mock.calls[0][1].autoApproveTools).toBe(false);
  });

  it("starts unchecked, scopes a direct run to its own key, and resets Run again", async () => {
    mocks.listWorkflows.mockResolvedValue({ workflows: WORKFLOWS, durableAvailable: false, toolAutoApproveAvailable: true });
    mocks.newWorkflowRunIdempotencyKey.mockReturnValueOnce("first-run-key").mockReturnValueOnce("second-run-key");
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    const checkbox = screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" });
    expect(checkbox).not.toBeChecked();
    await user.click(checkbox);
    await user.type(screen.getByRole("textbox", { name: /^Input/ }), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));
    await screen.findByText("the summary");
    expect(mocks.runWorkflow.mock.calls[0][1]).toMatchObject({ autoApproveTools: true, idempotencyKey: "first-run-key" });
    expect(checkbox).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Run again" }));
    await waitFor(() => expect(mocks.runWorkflow).toHaveBeenCalledTimes(2));
    expect(mocks.runWorkflow.mock.calls[1][1].autoApproveTools).toBe(false);
    expect(mocks.runWorkflow.mock.calls[1][1]).toHaveProperty("idempotencyKey", "second-run-key");
  });

  it("clears opt-in when selecting a different workflow and when returning", async () => {
    mocks.listWorkflows.mockResolvedValue({ workflows: WORKFLOWS, durableAvailable: true, toolAutoApproveAvailable: true });
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.click(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" }));
    await user.click(screen.getByRole("button", { name: "Research then write" }));
    expect(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Summarize" }));
    expect(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" })).not.toBeChecked();
  });

  it("keeps a post-dispatch error as an unconfirmed run, never an empty idle panel", async () => {
    mocks.listWorkflows.mockResolvedValue({ workflows: WORKFLOWS, durableAvailable: false, toolAutoApproveAvailable: true });
    mocks.runWorkflow.mockRejectedValue(new Error("Connection lost"));
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.click(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" }));
    await user.type(screen.getByRole("textbox", { name: /^Input/ }), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("outcome unknown")).toBeVisible();
    expect(screen.getByRole("list", { name: "Step results" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("The run may still be executing");
    expect(screen.getByRole("button", { name: "Open in chat" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" })).not.toBeChecked();
  });

  it("does not dispatch a run when session creation completes after unmount", async () => {
    let finish!: (session: { id: string }) => void;
    mocks.createSession.mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    const user = userEvent.setup();
    const { unmount } = render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(screen.getByRole("textbox", { name: /^Input/ }), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));
    unmount();
    finish({ id: "late-session" });
    await Promise.resolve();
    expect(mocks.runWorkflow).not.toHaveBeenCalled();
  });
  it("surfaces an operator preflight rejection without leaving a phantom active consent", async () => {
    mocks.listWorkflows.mockResolvedValue({ workflows: WORKFLOWS, durableAvailable: false, toolAutoApproveAvailable: true });
    mocks.runWorkflow.mockRejectedValue(Object.assign(new Error("Tool auto-approval is disabled by the operator."), { status: 409 }));
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.click(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" }));
    await user.type(screen.getByRole("textbox", { name: /^Input/ }), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("disabled by the operator");
    expect(screen.queryByRole("complementary", { name: "Run auto-approval: Summarize" })).toBeNull();
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
    expect(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" })).not.toBeChecked();
  });

  it("does not get stuck busy if creating the invocation key fails", async () => {
    mocks.listWorkflows.mockResolvedValue({ workflows: WORKFLOWS, durableAvailable: false, toolAutoApproveAvailable: true });
    mocks.newWorkflowRunIdempotencyKey.mockImplementation(() => { throw new Error("Unavailable"); });
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.click(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" }));
    await user.type(screen.getByRole("textbox", { name: /^Input/ }), "hello");
    await user.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("secure run identity");
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Run" })).toBeEnabled();
  });

});


describe("durable cancellation evidence continuation", () => {
  it.each(["streaming", "cancelled"] as const)(
    "retains durable cancellation evidence monitoring after a %s acknowledgement record",
    async (acknowledgedMessageStatus) => {
      const consent = { id: "late-consent", scope: "run" as const, toolCount: 11,
        grantedAt: "2026-09-06T00:00:00Z", expiresAt: "2099-09-06T08:00:00Z" };
      const firstReceipt: ExecutionReceipt = {
        version: 1, runtime: { agent: "research" }, prompt: [], promptMessageCount: 0, promptBytes: 0,
        contextBlocks: [], droppedHistoryMessages: 0, droppedContextBlocks: [], toolsOffered: [], toolsOfferedCount: 0,
        toolCalls: [{ tool: "web_search", outcome: "result", approval: "run", consentId: consent.id,
          result: { text: "Initial completed tool result", sha256: "a".repeat(64), bytes: 29, truncated: false } }],
        toolCallCount: 1, approvalsRequested: 0, approvalsGranted: 0, autoApprovedToolCalls: 1,
        iterations: 1, status: "complete", partial: false, truncated: false, notes: [],
      };
      const lateReceipt: ExecutionReceipt = {
        ...firstReceipt, runtime: { agent: "helper" }, status: "cancelled", partial: true,
        toolCalls: [{ tool: "finance_search", outcome: "result", approval: "run", consentId: consent.id,
          result: { text: "Late completed tool result", sha256: "b".repeat(64), bytes: 26, truncated: false } }],
      };
      let record: Message = {
        id: "late-message", sessionId: "s1", userId: "u1", role: "assistant", status: "streaming",
        content: "", model: "gpt-4", agent: "workflow:research-then-write", createdAt: "2026-09-06T00:00:00Z",
        workflowRunId: "u1:late-run", workflowToolConsent: consent,
        executionReceipt: { ...firstReceipt, status: "incomplete", partial: true },
        workflowStepReceipts: [firstReceipt],
        steps: [{ kind: "workflow_step", label: "Step 1: research", detail: "completed" }],
      };
      let cancellationAcknowledged = false;
      mocks.listWorkflows.mockResolvedValue({ workflows: WORKFLOWS, durableAvailable: true, toolAutoApproveAvailable: true });
      mocks.runWorkflow.mockResolvedValue({ scheduled: true, run: { sessionId: "s1", runId: "u1:late-run",
        status: "accepted", idempotencyKey: "durable-intent-key", autoApproveTools: true, toolConsent: consent } });
      mocks.listMessages.mockImplementation(async () => [record]);
      mocks.getWorkflowRun.mockImplementation(async () => ({ runId: "u1:late-run",
        status: cancellationAcknowledged ? "TERMINATED" : "RUNNING", ok: cancellationAcknowledged ? false : null }));
      mocks.cancelWorkflowRun.mockImplementation(async () => {
        cancellationAcknowledged = true;
        record = { ...record, status: acknowledgedMessageStatus, workflowConsentRevoked: true,
          content: "Cancellation acknowledged; a tool is still in flight." };
        return { runId: "u1:late-run", status: "TERMINATED", ok: false, text: record.content };
      });
      const user = userEvent.setup();
      render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
      await openRunTab(user, "Research then write");
      await user.type(screen.getByLabelText("Input"), "Research current markets");
      await user.click(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" }));
      await user.click(screen.getByRole("checkbox", { name: "Keep running if the app restarts" }));
      await user.click(screen.getByRole("button", { name: "Run" }));
      await user.click(await screen.findByRole("button", { name: "Revoke auto-approval & stop run" }));
      await screen.findByText("cancelled", { selector: ".workflow-result-verdict" }, { timeout: 10000 });
      expect(mocks.getWorkflowRun).toHaveBeenCalledWith("u1:late-run", "s1");
      expect(screen.queryByRole("complementary", { name: "Run auto-approval: Research then write" })).not.toBeNull();
      expect(screen.getByText("Step execution receipts · 1")).toBeVisible();
      const readsAtAcknowledgement = mocks.listMessages.mock.calls.length;

      // The tool finishes after cancellation was acknowledged. Cancellation is
      // still final as an authorization decision, but receipt data is not frozen.
      record = { ...record, status: "cancelled", workflowConsentRevoked: true,
        content: "Cancelled; the in-flight tool completed and its result was retained.",
        workflowStepReceipts: [firstReceipt, lateReceipt],
        executionReceipt: { ...firstReceipt, status: "cancelled", partial: true, toolCallCount: 2,
          autoApprovedToolCalls: 2, toolCalls: [...firstReceipt.toolCalls, ...lateReceipt.toolCalls] },
        steps: [...record.steps!, { kind: "tool_result", tool: "finance_search", label: "Late tool completed" },
          { kind: "workflow_error", label: "Step 2: helper", detail: "cancelled" }],
      };
      await screen.findByText("Step execution receipts · 2", {}, { timeout: 10000 });
      expect(mocks.listMessages.mock.calls.length).toBeGreaterThan(readsAtAcknowledgement);
      const report = screen.getByRole("region", { name: "Result" });
      expect(within(report).getByText("cancelled", { selector: ".workflow-result-verdict" })).toBeVisible();
      expect(within(report).queryByText(/succeeded in/)).toBeNull();
      expect(within(report).getAllByText("Initial completed tool result").length).toBeGreaterThan(0);
      expect(within(report).getAllByText("Late completed tool result").length).toBeGreaterThan(0);
      const aggregateSummary = within(report).getByText(/^Execution receipt ·/);
      await user.click(aggregateSummary);
      const aggregate = aggregateSummary.closest("details")!;
      await user.click(within(aggregate).getByText("Runtime"));
      expect(within(aggregate).getByText("2 reported by the server")).toBeVisible();
      expect(screen.getByText("Monitoring execution evidence after cancellation…")).toBeVisible();

      // An ongoing watch must not resurrect the old report after the user
      // selects another workflow while that evidence is still being refreshed.
      if (acknowledgedMessageStatus === "streaming") {
        await user.click(screen.getByRole("button", { name: "Summarize" }));
        const readsAtSwitch = mocks.listMessages.mock.calls.length;
        await waitFor(() => expect(mocks.listMessages.mock.calls.length).toBeGreaterThan(readsAtSwitch), { timeout: 10000 });
        expect(screen.queryByRole("region", { name: "Result" })).toBeNull();
      }
    }, 15000,
  );
});


describe("direct cancellation handle", () => {
  it.each([false, true])("can cancel a pending direct invocation before its run id is discoverable (auto-approve=%s)", async (autoApproveTools) => {
    mocks.listWorkflows.mockResolvedValue({ workflows: WORKFLOWS, durableAvailable: false, toolAutoApproveAvailable: true });
    mocks.newWorkflowRunIdempotencyKey.mockReturnValue("direct-invocation-key");
    mocks.listMessages.mockResolvedValue([]);
    let finish!: (result: WorkflowRunOutcome) => void;
    mocks.runWorkflow.mockImplementation(() => new Promise<WorkflowRunOutcome>((resolve) => { finish = resolve; }));
    mocks.cancelWorkflowRunByKey.mockResolvedValue({ runId: "u1:direct", status: "TERMINATED", ok: false });
    const user = userEvent.setup();
    render(<WorkflowBuilder agents={AGENTS} runModel="gpt-4" onRun={() => {}} />);
    await openRunTab(user);
    await user.type(screen.getByLabelText("Input"), "hello");
    if (autoApproveTools) await user.click(screen.getByRole("checkbox", { name: "Auto-approve enabled tools for this run" }));
    await user.click(screen.getByRole("button", { name: "Run" }));
    await waitFor(() => expect(mocks.runWorkflow).toHaveBeenCalled());
    expect(mocks.runWorkflow.mock.calls[0][1]).toMatchObject({ idempotencyKey: "direct-invocation-key", autoApproveTools });
    expect(mocks.newWorkflowRunIdempotencyKey.mock.invocationCallOrder[0]).toBeLessThan(mocks.createSession.mock.invocationCallOrder[0]);
    const stop = screen.queryByRole("button", { name: autoApproveTools ? "Revoke auto-approval & stop run" : "Stop run" });
    expect(stop).not.toBeNull();
    expect(stop).toBeEnabled();
    await user.click(stop!);
    await waitFor(() => expect(mocks.cancelWorkflowRunByKey).toHaveBeenCalledWith("summarize", "s1", "direct-invocation-key"));
    expect(mocks.cancelWorkflowRun).not.toHaveBeenCalled();
    expect(mocks.runWorkflow).toHaveBeenCalledTimes(1);
    await screen.findByText(/Run cancellation acknowledged/);
    await act(async () => finish({ scheduled: false, result: {
      sessionId: "s1", ok: false, message: { id: "direct-result", sessionId: "s1", userId: "u1", role: "assistant",
        status: "cancelled", content: "Cancelled before tool dispatch", model: "gpt-4", agent: "workflow:summarize",
        createdAt: "2026-09-06T00:00:00Z", workflowRunId: "u1:direct", workflowConsentRevoked: true },
    } }));
    expect(screen.getByText("cancelled", { selector: ".workflow-result-verdict" })).toBeVisible();
  });
});
