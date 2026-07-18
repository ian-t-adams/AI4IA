// @vitest-environment jsdom
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { InspectorSnapshot } from "@/lib/inspector";
import type { ConversationDraftDefaults } from "@/lib/types";
import { ConversationInspector } from "./ConversationInspector";

const mocks = vi.hoisted(() => ({
  getInspector: vi.fn(),
  listTools: vi.fn(),
  getToolCatalog: vi.fn(),
  listMemories: vi.fn(),
  getLibrarySummary: vi.fn(),
  updateSession: vi.fn(),
  associateLibraryDocument: vi.fn(),
  disassociateLibraryDocument: vi.fn(),
  deleteMemory: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listTools: mocks.listTools,
  getToolCatalog: mocks.getToolCatalog,
  updateSession: mocks.updateSession,
  associateLibraryDocument: mocks.associateLibraryDocument,
  disassociateLibraryDocument: mocks.disassociateLibraryDocument,
}));

vi.mock("@/lib/inspector", () => ({
  getInspector: mocks.getInspector,
  listMemories: mocks.listMemories,
  getLibrarySummary: mocks.getLibrarySummary,
  deleteMemory: mocks.deleteMemory,
}));

function snapshot(id: string, prompt = `Prompt ${id}`): InspectorSnapshot {
  return {
    generatedAt: new Date().toISOString(),
    sessionId: id,
    title: id,
    model: {
      id: "gpt-5.2",
      displayName: "GPT-5.2",
      contextWindow: 128000,
      maxOutputTokens: 32000,
    },
    instructions: {
      source: "session",
      editable: true,
      value: prompt,
      agentName: null,
    },
    agent: { name: null, displayName: null, description: null, enabled: true },
    tools: {
      inherited: [],
      added: [],
      removed: [],
      effective: [],
      voiceEffective: [],
    },
    attachments: [],
    libraryDocuments: [],
    librarySelectionMode: "explicit",
    sessionUsage: {
      sessionId: id,
      totalRequests: 1,
      totalPromptTokens: 10,
      totalCompletionTokens: 20,
      totalTokens: 30,
      totalCostMicroUsd: 10,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
      latest: null,
      truncated: false,
      coveredRequests: 1,
      coverageStart: null,
      coverageEnd: null,
    },
    monthlyUsage: {
      totalRequests: 1,
      totalTokens: 30,
      totalCostMicroUsd: 10,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
    },
    voice: {
      defaultProviderId: "azure_openai",
      enabledProviderIds: ["azure_openai"],
      applies: "next_connection",
    },
  };
}

function props(sessionId: string | null = "s1") {
  const draftDefaults: ConversationDraftDefaults = {
    agentName: null,
    toolOverrides: { added: [], removed: [] },
    libraryDocumentIds: [],
  };
  return {
    sessionId,
    refreshKey: 0,
    models: [],
    agents: [],
    selectedModel: "gpt-5.2",
    onModelChange: vi.fn(),
    params: {},
    onParamsChange: vi.fn(),
    systemPrompt: "",
    onSystemPromptChange: vi.fn(),
    draftDefaults,
    onDraftDefaultsChange: vi.fn(),
    onSessionUpdated: vi.fn(),
    attachmentCapabilities: null,
    voiceLocked: false,
    collapsed: false,
    onToggle: vi.fn(),
  };
}

beforeEach(() => {
  mocks.getInspector.mockImplementation(async (id: string) => snapshot(id));
  mocks.listTools.mockResolvedValue([]);
  mocks.getToolCatalog.mockImplementation(
    async (_sessionId: string | null, agentName: string | null) => ({
      tools: await mocks.listTools(),
      inheritedTools: agentName === "analyst" ? ["calculator"] : [],
    }),
  );
  mocks.listMemories.mockResolvedValue({
    status: "ok",
    supportsDelete: false,
    items: [],
    detail: null,
  });
  mocks.getLibrarySummary.mockResolvedValue({
    generatedAt: new Date().toISOString(),
    status: "ok",
    total: 0,
    byStatus: {},
    byModality: {},
    recent: [],
    maxUploadBytes: 10,
    maxDocuments: 8,
    modalities: ["document"],
  });
  mocks.updateSession.mockResolvedValue({
    id: "s1",
    userId: "u1",
    title: "s1",
    titleSource: "auto" as const,
    model: "gpt-5.2",
    systemPrompt: "Prompt s1",
    agentName: null,
    toolOverrides: { added: [], removed: [] },
    libraryDocumentIds: [],
    createdAt: new Date().toISOString(),
    updatedAt: new Date().toISOString(),
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("ConversationInspector", () => {
  it("edits agent, tool, and library defaults without patching a missing session", async () => {
    const onDraftDefaultsChange = vi.fn();
    const draftDefaults: ConversationDraftDefaults = {
      agentName: null,
      toolOverrides: { added: [], removed: [] },
      libraryDocumentIds: [],
    };
    mocks.listTools.mockResolvedValue([
      {
        name: "calculator",
        label: "Calculator",
        description: "Calculate",
        source: "built-in",
        risk: "safe",
        requiresApproval: false,
        scopes: [],
        available: true,
        selectable: true,
        detail: null,
        ownership: "application",
        typed: true,
        voice: true,
      },
    ]);
    mocks.getLibrarySummary.mockResolvedValue({
      generatedAt: new Date().toISOString(),
      status: "ok",
      total: 1,
      byStatus: { ready: 1 },
      byModality: { document: 1 },
      recent: [
        {
          id: "doc-1",
          userId: "u1",
          filename: "brief.pdf",
          contentType: "application/pdf",
          size: 10,
          status: "ready",
          modality: "document",
          chunkCount: 1,
          citationReady: true,
          error: null,
          createdAt: "",
          updatedAt: "",
        },
      ],
      maxUploadBytes: 100,
      maxDocuments: 8,
      modalities: ["document"],
    });
    const user = userEvent.setup();
    const { rerender } = render(
      <ConversationInspector
        {...props(null)}
        agents={[
          {
            name: "analyst",
            displayName: "Data Analyst",
            description: "Analyzes",
            enabled: true,
          },
        ]}
        draftDefaults={draftDefaults}
        onDraftDefaultsChange={onDraftDefaultsChange}
      />,
    );

    expect(screen.getByText("New conversation defaults")).toBeInTheDocument();
    expect(screen.getByText(/Draft — applied when the conversation starts/)).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Agent & tools" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "Agent" }), "analyst");
    expect(onDraftDefaultsChange).toHaveBeenLastCalledWith({
      ...draftDefaults,
      agentName: "analyst",
    });

    const withAgent = {
      ...draftDefaults,
      agentName: "analyst",
      toolOverrides: { added: ["calculator"], removed: [] },
    };
    rerender(
      <ConversationInspector
        {...props(null)}
        agents={[
          {
            name: "analyst",
            displayName: "Data Analyst",
            description: "Analyzes",
            enabled: true,
          },
        ]}
        draftDefaults={withAgent}
        onDraftDefaultsChange={onDraftDefaultsChange}
      />,
    );
    await user.click(await screen.findByRole("checkbox", { name: /Calculator/ }));
    expect(onDraftDefaultsChange).toHaveBeenLastCalledWith({
      ...withAgent,
      toolOverrides: { added: [], removed: ["calculator"] },
    });

    await user.click(screen.getByRole("tab", { name: "Context" }));
    await user.click(await screen.findByRole("button", { name: "Add brief.pdf" }));
    expect(onDraftDefaultsChange).toHaveBeenLastCalledWith({
      ...withAgent,
      libraryDocumentIds: ["doc-1"],
    });
    expect(mocks.updateSession).not.toHaveBeenCalled();
    expect(mocks.associateLibraryDocument).not.toHaveBeenCalled();
  });

  it("supports roving keyboard tabs and one editable instruction source", async () => {
    const user = userEvent.setup();
    render(<ConversationInspector {...props()} />);
    const modelTab = screen.getByRole("tab", { name: "Model" });
    await user.click(modelTab);
    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Voice" })).toHaveFocus();
    await user.keyboard("{Home}");
    expect(modelTab).toHaveFocus();
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    expect(
      await screen.findByRole("textbox", { name: "System prompt" }),
    ).toHaveValue("Prompt s1");
  });

  it("discards a late A response after switching to B", async () => {
    let resolveA!: (value: ReturnType<typeof snapshot>) => void;
    const a = new Promise<ReturnType<typeof snapshot>>((resolve) => {
      resolveA = resolve;
    });
    mocks.getInspector.mockImplementation((id: string) =>
      id === "A" ? a : Promise.resolve(snapshot("B")),
    );
    const { rerender } = render(<ConversationInspector {...props("A")} />);
    rerender(<ConversationInspector {...props("B")} />);
    await userEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    expect(
      await screen.findByRole("textbox", { name: "System prompt" }),
    ).toHaveValue("Prompt B");
    resolveA(snapshot("A"));
    await Promise.resolve();
    expect(screen.getByRole("textbox", { name: "System prompt" })).toHaveValue(
      "Prompt B",
    );
  });

  it("blocks mutations until the current snapshot is loaded", async () => {
    mocks.getInspector.mockImplementation(() => new Promise(() => {}));
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Agent & tools" }));
    expect(screen.getByRole("combobox", { name: "Agent" })).toBeDisabled();
    expect(mocks.updateSession).not.toHaveBeenCalled();
  });

  it("uses dialog semantics and Escape close in drawer mode", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const onToggle = vi.fn();
    render(<ConversationInspector {...props()} onToggle={onToggle} />);
    const dialog = screen.getByRole("dialog", {
      name: "Conversation inspector",
    });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onToggle).toHaveBeenCalled();
  });

  it("returns focus to the explicit inspector opener after Escape", async () => {
    vi.stubGlobal(
      "matchMedia",
      vi.fn(() => ({
        matches: true,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    function Harness() {
      const [collapsed, setCollapsed] = useState(true);
      return (
        <>
          <ConversationInspector
            {...props()}
            collapsed={collapsed}
            onToggle={() => setCollapsed((value) => !value)}
          />
          {!collapsed ? (
            <button
              type="button"
              aria-label="Inspector backdrop"
              onClick={() => setCollapsed(true)}
            />
          ) : null}
        </>
      );
    }
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", {
      name: "Open conversation inspector",
    });
    await user.click(opener);
    const dialog = screen.getByRole("dialog", { name: "Conversation inspector" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    const restored = await screen.findByRole("button", {
      name: "Open conversation inspector",
    });
    expect(restored).toHaveFocus();
    await user.click(restored);
    await user.click(screen.getByRole("button", { name: "Inspector backdrop" }));
    expect(
      await screen.findByRole("button", { name: "Open conversation inspector" }),
    ).toHaveFocus();
  });

  it("shows independent unavailable and empty states", async () => {
    mocks.listMemories.mockRejectedValue(new Error("memory source offline"));
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Memory" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "memory source offline",
    );
  });

  it("renders snapshot sections while an unrelated tool catalog is still loading", async () => {
    mocks.listTools.mockImplementation(() => new Promise(() => {}));
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    expect(
      await screen.findByRole("textbox", { name: "System prompt" }),
    ).toHaveValue("Prompt s1");
    await userEvent.click(screen.getByRole("tab", { name: "Agent & tools" }));
    expect(screen.getByText("Loading tools…")).toBeInTheDocument();
  });

  it("keeps model and instructions disabled when the snapshot fails", async () => {
    mocks.getInspector.mockRejectedValue(new Error("snapshot offline"));
    render(<ConversationInspector {...props()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("snapshot offline");
    expect(screen.queryByRole("combobox", { name: "Model" })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    expect(screen.getByRole("alert")).toHaveTextContent("snapshot offline");
    expect(screen.queryByRole("textbox", { name: "System prompt" })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Usage" }));
    const retry = screen.getByRole("button", { name: "Retry" });
    await userEvent.click(retry);
    expect(mocks.getInspector).toHaveBeenCalledTimes(2);
  });

  it("discards a late prompt save after switching conversations", async () => {
    let resolveUpdate!: (value: ReturnType<typeof snapshot>) => void;
    mocks.updateSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );
    const onSessionUpdated = vi.fn();
    const { rerender } = render(
      <ConversationInspector {...props("A")} onSessionUpdated={onSessionUpdated} />,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    await userEvent.click(await screen.findByRole("button", { name: "Save" }));
    rerender(
      <ConversationInspector {...props("B")} onSessionUpdated={onSessionUpdated} />,
    );
    resolveUpdate(snapshot("A") as never);
    await act(async () => Promise.resolve());
    expect(onSessionUpdated).not.toHaveBeenCalled();
  });

  it("does not strand saving when a same-session refresh lands mid-mutation", async () => {
    let resolveUpdate!: (value: ReturnType<typeof snapshot>) => void;
    mocks.updateSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );
    const onSessionUpdated = vi.fn();
    const { rerender } = render(
      <ConversationInspector {...props()} onSessionUpdated={onSessionUpdated} />,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Instructions" }));
    await userEvent.click(await screen.findByRole("button", { name: "Save" }));
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    rerender(
      <ConversationInspector
        {...props()}
        refreshKey={1}
        onSessionUpdated={onSessionUpdated}
      />,
    );
    resolveUpdate(snapshot("s1") as never);
    await waitFor(() => expect(onSessionUpdated).toHaveBeenCalled());
    expect(screen.getByRole("button", { name: "Save" })).not.toBeDisabled();
  });

  it("discards a late tool override after switching conversations", async () => {
    const value = snapshot("A");
    value.tools.effective = [];
    mocks.getInspector.mockImplementation((id: string) =>
      Promise.resolve(id === "A" ? value : snapshot("B")),
    );
    mocks.listTools.mockResolvedValue([
      {
        name: "calculator",
        label: "Calculator",
        description: "Calculate",
        source: "built-in",
        risk: "safe",
        requiresApproval: false,
        scopes: [],
        available: true,
        selectable: true,
        detail: null,
        ownership: "application",
        typed: true,
        voice: true,
      },
    ]);
    let resolveUpdate!: (value: ReturnType<typeof snapshot>) => void;
    mocks.updateSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveUpdate = resolve;
        }),
    );
    const onSessionUpdated = vi.fn();
    const { rerender } = render(
      <ConversationInspector {...props("A")} onSessionUpdated={onSessionUpdated} />,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Agent & tools" }));
    await userEvent.click(await screen.findByRole("checkbox"));
    rerender(
      <ConversationInspector {...props("B")} onSessionUpdated={onSessionUpdated} />,
    );
    resolveUpdate(snapshot("A") as never);
    await act(async () => Promise.resolve());
    expect(onSessionUpdated).not.toHaveBeenCalled();
  });

  it("renders missing tool governance metadata as unknown, never safe defaults", async () => {
    const value = snapshot("s1");
    value.tools.effective = ["mystery"];
    mocks.getInspector.mockResolvedValue(value);
    mocks.listTools.mockResolvedValue([
      {
        name: "mystery",
        label: "mystery",
        description: "Metadata unavailable",
        source: "unknown",
        risk: null,
        requiresApproval: null,
        scopes: null,
        available: false,
        selectable: false,
        detail: "The server could not resolve authoritative tool metadata.",
        ownership: "unknown",
        typed: null,
        voice: null,
      },
    ]);
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Agent & tools" }));
    expect(await screen.findByText(/risk unknown/)).toHaveTextContent(
      "approval unknown",
    );
    expect(screen.getByText(/scopes unknown/)).toHaveTextContent("typed unknown");
  });

  it("surfaces a tool's description and an accessible reason it can't be toggled here", async () => {
    mocks.listTools.mockResolvedValue([
      {
        name: "shell",
        label: "Shell",
        description: "Runs a shell command on the host.",
        source: "built-in",
        risk: "destructive",
        requiresApproval: true,
        scopes: ["shell:exec"],
        available: true,
        selectable: false,
        detail: null,
        ownership: "application",
        typed: true,
        voice: false,
      },
    ]);
    const user = userEvent.setup();
    render(<ConversationInspector {...props()} />);
    await user.click(screen.getByRole("tab", { name: "Agent & tools" }));
    const row = (await screen.findByText("Shell")).closest("label") as HTMLElement;
    expect(row).not.toHaveAttribute("title");

    await user.click(within(row).getByRole("button", { name: "Help: Shell description" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(
      "Runs a shell command on the host.",
    );

    expect(
      within(row).getByText(/Can't enable from here/),
    ).toHaveTextContent(/only a pre-built agent can grant/);
  });

  it("explains the tool list's dot-joined columns via a glossary tooltip", async () => {
    mocks.listTools.mockResolvedValue([
      {
        name: "calculator",
        label: "Calculator",
        description: "Calculate",
        source: "built-in",
        risk: "safe",
        requiresApproval: false,
        scopes: [],
        available: true,
        selectable: true,
        detail: null,
        ownership: "application",
        typed: true,
        voice: true,
      },
    ]);
    const user = userEvent.setup();
    render(<ConversationInspector {...props()} />);
    await user.click(screen.getByRole("tab", { name: "Agent & tools" }));
    await screen.findByText("Calculator");
    await user.click(
      screen.getByRole("button", { name: "Help: How to read the tool list" }),
    );
    expect(screen.getByRole("tooltip")).toHaveTextContent(/schema-validated/);
  });

  it("shows the selected agent's description for both live sessions and new-conversation drafts", async () => {
    const value = snapshot("s1");
    value.agent = { name: "analyst", displayName: "Analyst", description: "Crunches numbers.", enabled: true };
    mocks.getInspector.mockResolvedValue(value);
    const user = userEvent.setup();
    const { rerender } = render(
      <ConversationInspector
        {...props()}
        agents={[
          { name: "analyst", displayName: "Analyst", description: "Crunches numbers.", enabled: true },
        ]}
      />,
    );
    await user.click(screen.getByRole("tab", { name: "Agent & tools" }));
    expect(await screen.findByText("Crunches numbers.")).toBeInTheDocument();
    expect(
      screen.getByRole("option", { name: "Analyst" }),
    ).toHaveAttribute("title", "Crunches numbers.");

    const draftDefaults: ConversationDraftDefaults = {
      agentName: "analyst",
      toolOverrides: { added: [], removed: [] },
      libraryDocumentIds: [],
    };
    rerender(
      <ConversationInspector
        {...props(null)}
        agents={[
          { name: "analyst", displayName: "Analyst", description: "Crunches numbers.", enabled: true },
        ]}
        draftDefaults={draftDefaults}
      />,
    );
    await user.click(screen.getByRole("tab", { name: "Agent & tools" }));
    expect(await screen.findByText("Crunches numbers.")).toBeInTheDocument();
  });

  it("discards a late document association after switching conversations", async () => {
    const document = {
      id: "doc-1",
      userId: "u1",
      filename: "shared.pdf",
      contentType: "application/pdf",
      size: 10,
      status: "ready",
      modality: "document",
      chunkCount: 1,
      citationReady: true,
      error: null,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
    };
    mocks.getLibrarySummary.mockResolvedValue({
      generatedAt: new Date().toISOString(),
      status: "ok",
      total: 1,
      byStatus: { ready: 1 },
      byModality: { document: 1 },
      recent: [document],
      maxUploadBytes: 100,
      maxDocuments: 20,
      modalities: ["document"],
    });
    let resolveAssociate!: (value: ReturnType<typeof snapshot>) => void;
    mocks.associateLibraryDocument.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveAssociate = resolve;
        }),
    );
    const onSessionUpdated = vi.fn();
    const { rerender } = render(
      <ConversationInspector {...props("A")} onSessionUpdated={onSessionUpdated} />,
    );
    await userEvent.click(screen.getByRole("tab", { name: "Context" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add shared.pdf" }));
    rerender(
      <ConversationInspector {...props("B")} onSessionUpdated={onSessionUpdated} />,
    );
    resolveAssociate(snapshot("A") as never);
    await act(async () => Promise.resolve());
    expect(onSessionUpdated).not.toHaveBeenCalled();
  });

  it("confirms item-specific memory deletion and exposes a pending-safe label", async () => {
    mocks.listMemories.mockResolvedValue({
      status: "ok",
      supportsDelete: true,
      items: [
        {
          id: "m1",
          text: "Prefers concise answers",
          source: "user_message",
          sessionId: "s1",
          documentId: null,
          createdAt: null,
        },
      ],
      detail: null,
    });
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Memory" }));
    const remove = await screen.findByRole("button", {
      name: "Delete memory: Prefers concise answers",
    });
    await userEvent.click(remove);
    expect(screen.getByText("Delete this memory?")).toBeInTheDocument();
    const confirm = screen.getByRole("button", {
      name: "Confirm deletion of memory: Prefers concise answers",
    });
    expect(confirm).toHaveFocus();
    await userEvent.click(
      screen.getByRole("button", {
        name: "Cancel deletion of memory: Prefers concise answers",
      }),
    );
    const restoredRemove = screen.getByRole("button", {
      name: "Delete memory: Prefers concise answers",
    });
    expect(restoredRemove).toHaveFocus();
    await userEvent.click(restoredRemove);
    const confirmAfterCancel = screen.getByRole("button", {
      name: "Confirm deletion of memory: Prefers concise answers",
    });
    expect(confirmAfterCancel).toHaveFocus();
    await userEvent.click(confirmAfterCancel);
    expect(mocks.deleteMemory).toHaveBeenCalledWith("m1");
  });

  it("shows and auto-clears saved feedback", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<ConversationInspector {...props()} />);
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    await user.click(await screen.findByRole("button", { name: "Save" }));
    expect(await screen.findByText("Saved")).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(2100);
    });
    expect(screen.queryByText("Saved")).toBeNull();
    vi.useRealTimers();
  });

  it("renders context pressure, latest completeness, and partial coverage honestly", async () => {
    const value = snapshot("s1");
    value.sessionUsage.latest = {
      provider: "azure_openai",
      model: "gpt-5.2",
      agent: null,
      usageKnown: true,
      usageComplete: false,
      promptTokens: 64000,
      completionTokens: 10,
      totalTokens: 64010,
      costKnown: false,
      estCostMicroUsd: null,
      createdAt: "2026-07-17T12:00:00Z",
    };
    value.sessionUsage.truncated = true;
    value.sessionUsage.coveredRequests = 1000;
    value.sessionUsage.coverageStart = "2026-01-01T00:00:00Z";
    value.sessionUsage.costUnknownRequests = 1;
    mocks.getInspector.mockResolvedValue(value);
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Usage" }));
    expect(await screen.findByText("50.0%")).toBeInTheDocument();
    expect(screen.getByText(/Partial coverage: newest 1000 requests/)).toBeInTheDocument();
    expect(screen.getByText(/partial usage/)).toBeInTheDocument();
  });

  it("renders unknown-only and mixed token coverage without fake zeros", async () => {
    const value = snapshot("s1");
    value.sessionUsage.totalRequests = 2;
    value.sessionUsage.totalTokens = 0;
    value.sessionUsage.unknownUsageRequests = 2;
    value.monthlyUsage.totalRequests = 4;
    value.monthlyUsage.totalTokens = 120;
    value.monthlyUsage.unknownUsageRequests = 1;
    mocks.getInspector.mockResolvedValue(value);
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Usage" }));
    expect(await screen.findByText("Unknown")).toBeInTheDocument();
    expect(
      screen.getByText("Known subtotal 120 (3/4 requests reported)"),
    ).toBeInTheDocument();
    expect(screen.getByText("Last 30 days tokens")).toBeInTheDocument();
  });
});
