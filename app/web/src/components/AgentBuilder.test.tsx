// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { AgentSummary, ModelEntry, UserAgent } from "@/lib/types";
import type { UserMcpServer } from "@/lib/customTools";
import { AgentBuilder } from "./AgentBuilder";

const mocks = vi.hoisted(() => ({
  listMyAgents: vi.fn(),
  listMcpServers: vi.fn(),
  listOfficialMcpServers: vi.fn(),
  deleteAgent: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listMyAgents: mocks.listMyAgents,
  listMcpServers: mocks.listMcpServers,
  listOfficialMcpServers: mocks.listOfficialMcpServers,
  deleteAgent: mocks.deleteAgent,
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

const MCP_SERVER: UserMcpServer = {
  id: "s1",
  userId: "u1",
  name: "weather",
  displayName: "Weather",
  description: "Forecasts",
  endpoint: "https://weather.example.com/mcp",
  host: "weather.example.com",
  transport: "streamable_http",
  authMode: "none",
  trusted: true,
  enabled: true,
  secretRef: null,
  discoveredTools: [
    { name: "forecast", description: "Get a weather forecast", inputSchema: {} },
  ],
  toolApprovals: {},
  createdAt: "2024-01-01T00:00:00Z",
  updatedAt: "2024-01-01T00:00:00Z",
  lastConnectedAt: "2024-01-01T00:00:00Z",
  lastError: null,
  consecutiveFailures: 0,
  quarantinedUntil: null,
  lastHealthCheck: "2024-01-01T00:00:00Z",
  lastHealthError: null,
};

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
  it("requires irreversible confirmation before deleting an agent", async () => {
    const confirmSpy = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    mocks.deleteAgent.mockResolvedValue(undefined);
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<AgentBuilder agents={AGENTS} models={[]} onChanged={onChanged} />);
    const remove = await screen.findByRole("button", { name: "Delete helper" });

    await user.click(remove);
    expect(mocks.deleteAgent).not.toHaveBeenCalled();
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringMatching(/permanently delete agent "Helper".*can't be undone/i),
    );

    mocks.listMyAgents.mockResolvedValueOnce([]);
    await user.click(remove);
    await waitFor(() => expect(mocks.deleteAgent).toHaveBeenCalledWith("helper"));
    expect(onChanged).toHaveBeenCalled();
  });

  it("explains what a built-in tool does, when to use it, and its risk level", async () => {
    const user = userEvent.setup();
    render(<AgentBuilder agents={[]} models={[]} onChanged={async () => {}} />);

    const toolsGroup = await screen.findByRole("group", { name: /Tools \(max/i });
    const checkbox = within(toolsGroup).getByRole("checkbox", { name: "Calculator" });
    expect(checkbox).not.toBeChecked();
    // The row must not be a <label> wrapping the checkbox, help button, and
    // text together — nesting a HelpTooltip inside a <label> lets its own
    // "Help: …" text (or other row content) leak into the checkbox's
    // accessible name. htmlFor/id must scope the label to just the visible
    // text, with the tooltip kept as a sibling.
    expect(checkbox.closest("label")).toBeNull();
    expect(checkbox).toHaveAccessibleName("Calculator");
    const calculatorRow = checkbox.parentElement as HTMLElement;

    await user.click(within(calculatorRow).getByRole("button", { name: "Help: Calculator" }));
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
    const helperCheckbox = within(delegateGroup).getByRole("checkbox", { name: "Helper" });
    // Regression test: the nested help button's aria-label must not leak
    // into this checkbox's accessible name either.
    expect(helperCheckbox.closest("label")).toBeNull();
    const helperRow = helperCheckbox.parentElement as HTMLElement;
    expect(within(helperRow).getByText("· yours")).toBeInTheDocument();

    const researchCheckbox = within(delegateGroup).getByRole("checkbox", {
      name: "Research Assistant",
    });
    const researchRow = researchCheckbox.parentElement as HTMLElement;
    expect(within(researchRow).getByText("· pre-created")).toBeInTheDocument();

    await user.click(
      within(researchRow).getByRole("button", { name: "Help: Research Assistant" }),
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent("Searches and cites sources.");
  });

  it("gives an MCP tool row a clean accessible name and surfaces its approval posture as a sibling, not a nested label", async () => {
    const user = userEvent.setup();
    mocks.listMcpServers.mockResolvedValue([MCP_SERVER]);
    render(
      <AgentBuilder agents={[]} models={[]} customToolsEnabled onChanged={async () => {}} />,
    );

    const mcpGroup = await screen.findByRole("group", { name: /MCP tools/i });
    const checkbox = within(mcpGroup).getByRole("checkbox", { name: "forecast" });
    expect(checkbox.closest("label")).toBeNull();
    expect(checkbox).toHaveAccessibleName("forecast");
    const row = checkbox.parentElement as HTMLElement;
    // Trusted server + no override -> auto-approved; shown alongside the
    // checkbox rather than folded into its name.
    expect(within(row).getByText("· auto")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Help: forecast" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("Get a weather forecast");
    expect(checkbox).not.toBeChecked();
  });

  it("labels a tool with an `always` approval override consistently as unavailable and explains the exact enabling path, even on a trusted server", async () => {
    const user = userEvent.setup();
    mocks.listMcpServers.mockResolvedValue([
      { ...MCP_SERVER, trusted: true, toolApprovals: { forecast: "always" } },
    ]);
    render(
      <AgentBuilder agents={[]} models={[]} customToolsEnabled onChanged={async () => {}} />,
    );

    const mcpGroup = await screen.findByRole("group", { name: /MCP tools/i });
    const checkbox = within(mcpGroup).getByRole("checkbox", { name: "forecast" });
    const row = checkbox.parentElement as HTMLElement;
    // Consistent "unavailable" wording — not "· approval (forced)" or
    // "· approval", which read as a live per-use prompt that doesn't exist.
    expect(within(row).getByText("· unavailable")).toBeInTheDocument();
    expect(within(row).queryByText(/approval \(forced\)/i)).not.toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Help: forecast approval" }));
    const tooltip = screen.getByRole("tooltip");
    // Must explain the actual enabling path, and must not imply trusting the
    // server alone would fix an `always` override.
    expect(tooltip).toHaveTextContent(/no live approval prompt/i);
    expect(tooltip).toHaveTextContent(/even on a trusted server/i);
    expect(tooltip).toHaveTextContent(/changed away from Always/i);
  });

  it("also labels a no-override tool on an untrusted server as unavailable, with a trust-the-server enabling path", async () => {
    const user = userEvent.setup();
    mocks.listMcpServers.mockResolvedValue([{ ...MCP_SERVER, trusted: false }]);
    render(
      <AgentBuilder agents={[]} models={[]} customToolsEnabled onChanged={async () => {}} />,
    );

    const mcpGroup = await screen.findByRole("group", { name: /MCP tools/i });
    const checkbox = within(mcpGroup).getByRole("checkbox", { name: "forecast" });
    const row = checkbox.parentElement as HTMLElement;
    // Same "unavailable" wording as the `always`-override case, so the badge
    // is consistent regardless of *why* the tool is unavailable.
    expect(within(row).getByText("· unavailable")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Help: forecast approval" }));
    const tooltip = screen.getByRole("tooltip");
    expect(tooltip).toHaveTextContent(/no live approval prompt/i);
    // Unlike `always`, trusting the server (or setting Never) does fix this one.
    expect(tooltip).toHaveTextContent(/unless the server is trusted/i);
  });

  it("shows a trusted, never-overridden tool as unavailable (not auto) when its server is disabled", async () => {
    // Regression test: the status word/tooltip used to be derived only from
    // the tool's own approval override, so a disabled server's trusted,
    // never-overridden tool incorrectly showed "· auto" — actively
    // misleading, since it can't actually be called.
    const user = userEvent.setup();
    mocks.listMcpServers.mockResolvedValue([
      { ...MCP_SERVER, trusted: true, enabled: false, toolApprovals: { forecast: "never" } },
    ]);
    render(
      <AgentBuilder agents={[]} models={[]} customToolsEnabled onChanged={async () => {}} />,
    );

    const mcpGroup = await screen.findByRole("group", { name: /MCP tools/i });
    const checkbox = within(mcpGroup).getByRole("checkbox", { name: "forecast" });
    const row = checkbox.parentElement as HTMLElement;
    expect(within(row).getByText("· unavailable")).toBeInTheDocument();
    expect(within(row).queryByText("· auto")).not.toBeInTheDocument();
    expect(within(row).queryByText("· pre-approved")).not.toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Help: forecast approval" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(/turned off/i);
  });

  it("shows a trusted tool as unavailable (not auto) when its server is quarantined", async () => {
    const user = userEvent.setup();
    mocks.listMcpServers.mockResolvedValue([
      {
        ...MCP_SERVER,
        trusted: true,
        consecutiveFailures: 5,
        quarantinedUntil: new Date(Date.now() + 60_000).toISOString(),
      },
    ]);
    render(
      <AgentBuilder agents={[]} models={[]} customToolsEnabled onChanged={async () => {}} />,
    );

    const mcpGroup = await screen.findByRole("group", { name: /MCP tools/i });
    const checkbox = within(mcpGroup).getByRole("checkbox", { name: "forecast" });
    const row = checkbox.parentElement as HTMLElement;
    expect(within(row).getByText("· unavailable")).toBeInTheDocument();

    await user.click(within(row).getByRole("button", { name: "Help: forecast approval" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(/quarantined/i);
  });

  it("keeps MCP checkbox ids unique and whitespace-safe even when discovered tool names collide and contain spaces", async () => {
    // Regression test: ids used to be built from the tool's namespaced name
    // (`ag-mcp-${namespacedName}`), so two tools sharing a name — or a name
    // containing whitespace — could collide or produce a broken id/label
    // association. Ids must now be index-based, independent of the name.
    mocks.listMcpServers.mockResolvedValue([
      {
        ...MCP_SERVER,
        discoveredTools: [
          { name: "foo bar", description: "First", inputSchema: {} },
          { name: "foo bar", description: "Second, same name", inputSchema: {} },
        ],
      },
    ]);
    render(
      <AgentBuilder agents={[]} models={[]} customToolsEnabled onChanged={async () => {}} />,
    );

    const mcpGroup = await screen.findByRole("group", { name: /MCP tools/i });
    const checkboxes = within(mcpGroup).getAllByRole("checkbox", { name: "foo bar" });
    expect(checkboxes).toHaveLength(2);
    const ids = checkboxes.map((c) => c.id);
    expect(new Set(ids).size).toBe(2);
    for (const id of ids) {
      expect(id).not.toMatch(/\s/);
    }
    for (const checkbox of checkboxes) {
      expect(checkbox).toHaveAccessibleName("foo bar");
    }
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
        supportsSampling: true,
        reasoningEffortOptions: [],
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
        supportsSampling: true,
        reasoningEffortOptions: [],
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
        supportsSampling: true,
        reasoningEffortOptions: [],
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
    expect(select).not.toHaveAttribute("aria-describedby");

    await user.selectOptions(select, "deep-thinker");
    expect(screen.getByText(/Reasoning\./)).toBeInTheDocument();
    expect(screen.getByText(/multi-step logic/)).toBeInTheDocument();

    // The note must be programmatically associated with the select (not just
    // visually adjacent), and the id it points to must resolve to a real,
    // rendered element rather than a dangling IDREF.
    const describedBy = select.getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    const note = document.getElementById(describedBy as string);
    expect(note).not.toBeNull();
    expect(note).toHaveTextContent(/Reasoning\./);
  });

  it("describes the preferred-model default as a fallback, not an override", async () => {
    const user = userEvent.setup();
    render(<AgentBuilder agents={[]} models={[]} onChanged={async () => {}} />);
    await user.click(screen.getByRole("button", { name: "Help: Preferred model" }));
    const tooltip = screen.getByRole("tooltip");
    // Backend precedence is body.model (chat header) > session.model >
    // agent.defaultModel -- so this setting must read as a fallback, never
    // as something that overrides an explicit header/session choice.
    expect(tooltip).toHaveTextContent(/fallback/i);
    expect(tooltip).toHaveTextContent(/take priority over this/i);
    expect(tooltip).not.toHaveTextContent(/overrides the model/i);
  });
});
