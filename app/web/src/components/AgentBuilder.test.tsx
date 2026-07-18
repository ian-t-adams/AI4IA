// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { AgentSummary, ModelEntry, UserAgent } from "@/lib/types";
import { AgentBuilder } from "./AgentBuilder";

const mocks = vi.hoisted(() => ({
  listMyAgents: vi.fn(),
  listMcpServers: vi.fn(),
  listOfficialMcpServers: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listMyAgents: mocks.listMyAgents,
  listMcpServers: mocks.listMcpServers,
  listOfficialMcpServers: mocks.listOfficialMcpServers,
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

const MINE: UserAgent[] = [
  {
    id: "helper",
    userId: "u1",
    name: "helper",
    displayName: "Helper",
    description: "Helps with quick tasks.",
    systemPrompt: "Be helpful.",
    defaultModel: null,
    tools: [],
    links: [],
    enabled: true,
    createdAt: "2024-01-01T00:00:00Z",
    updatedAt: "2024-01-01T00:00:00Z",
  },
];

beforeEach(() => {
  mocks.listMyAgents.mockResolvedValue(MINE);
  mocks.listMcpServers.mockResolvedValue([]);
  mocks.listOfficialMcpServers.mockResolvedValue([]);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AgentBuilder", () => {
  it("explains what a built-in tool does, when to use it, and its risk level", async () => {
    const user = userEvent.setup();
    render(<AgentBuilder agents={[]} models={[]} onChanged={async () => {}} />);

    const toolsGroup = await screen.findByRole("group", { name: /Tools \(max/i });
    const calculatorRow = within(toolsGroup).getByText("Calculator").closest("label");
    expect(calculatorRow).not.toBeNull();
    const checkbox = within(calculatorRow!).getByRole("checkbox");
    expect(checkbox).not.toBeChecked();
    // The help button lives inside the same <label>; its own aria-label must
    // not leak into the checkbox's accessible name (regression test for a
    // label/HelpTooltip nesting bug).
    expect(screen.getByRole("checkbox", { name: "Calculator" })).toBe(checkbox);

    await user.click(within(calculatorRow!).getByRole("button", { name: "Help: Calculator" }));
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(/arithmetic/i);
    expect(tooltip).toHaveTextContent(/Safe:/);
    // Opening help must never attach the tool it explains.
    expect(checkbox).not.toBeChecked();
  });

  it("distinguishes the user's own agents from pre-created ones and surfaces descriptions on demand", async () => {
    const user = userEvent.setup();
    render(<AgentBuilder agents={AGENTS} models={[]} onChanged={async () => {}} />);
    await waitFor(() => expect(mocks.listMyAgents).toHaveBeenCalled());

    const delegateGroup = await screen.findByRole("group", { name: /Delegate to/i });
    const helperRow = within(delegateGroup).getByText("Helper").closest("label");
    expect(helperRow).not.toBeNull();
    expect(within(helperRow!).getByText("· yours")).toBeInTheDocument();
    // Regression test: the nested help button's aria-label must not leak
    // into this checkbox's accessible name either.
    expect(screen.getByRole("checkbox", { name: "Helper" })).toBe(
      within(helperRow!).getByRole("checkbox"),
    );

    const researchRow = within(delegateGroup).getByText("Research Assistant").closest("label");
    expect(researchRow).not.toBeNull();
    expect(within(researchRow!).getByText("· pre-created")).toBeInTheDocument();

    await user.click(
      within(researchRow!).getByRole("button", { name: "Help: Research Assistant" }),
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent("Searches and cites sources.");
  });

  it("groups the preferred-model picker by category and explains the selected one", async () => {
    const user = userEvent.setup();
    const models: ModelEntry[] = [
      {
        id: "gpt-fast",
        displayName: "GPT Fast",
        category: "chat-fast",
        format: "OpenAI",
        conversational: true,
        contextWindow: 128_000,
        maxOutputTokens: null,
        options: [],
      },
      {
        id: "deep-thinker",
        displayName: "Deep Thinker",
        category: "reasoning",
        format: "OpenAI",
        conversational: true,
        contextWindow: 200_000,
        maxOutputTokens: null,
        options: [],
      },
      {
        id: "dalle",
        displayName: "DALL-E",
        category: "image",
        format: "OpenAI",
        conversational: false,
        contextWindow: null,
        maxOutputTokens: null,
        options: [],
      },
    ];
    render(<AgentBuilder agents={[]} models={models} onChanged={async () => {}} />);

    const select = screen.getByRole("combobox", { name: "Preferred model" });
    // Grouped by category; capability (non-conversational) models excluded.
    expect(select.querySelector('optgroup[label="chat-fast"]')).not.toBeNull();
    expect(select.querySelector('optgroup[label="reasoning"]')).not.toBeNull();
    expect(screen.queryByRole("option", { name: /DALL-E/ })).toBeNull();

    // No category note until a real model is picked.
    expect(screen.queryByText(/Reasoning\./)).toBeNull();

    await user.selectOptions(select, "deep-thinker");
    expect(screen.getByText(/Reasoning\./)).toBeInTheDocument();
    expect(screen.getByText(/multi-step logic/)).toBeInTheDocument();
  });
});
