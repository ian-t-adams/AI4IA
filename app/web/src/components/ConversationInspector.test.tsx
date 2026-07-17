// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { InspectorSnapshot } from "@/lib/inspector";
import { ConversationInspector } from "./ConversationInspector";

const mocks = vi.hoisted(() => ({
  getInspector: vi.fn(),
  listTools: vi.fn(),
  listMemories: vi.fn(),
  getLibrarySummary: vi.fn(),
  updateSession: vi.fn(),
  associateLibraryDocument: vi.fn(),
  disassociateLibraryDocument: vi.fn(),
  deleteMemory: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listTools: mocks.listTools,
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

function props(sessionId = "s1") {
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
    onSessionUpdated: vi.fn(),
    voiceLocked: false,
    collapsed: false,
    onToggle: vi.fn(),
  };
}

beforeEach(() => {
  mocks.getInspector.mockImplementation(async (id: string) => snapshot(id));
  mocks.listTools.mockResolvedValue([]);
  mocks.listMemories.mockResolvedValue({
    status: "ok",
    supportsDelete: false,
    items: [],
    detail: null,
  });
  mocks.getLibrarySummary.mockResolvedValue({
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

  it("shows independent unavailable and empty states", async () => {
    mocks.listMemories.mockRejectedValue(new Error("memory source offline"));
    render(<ConversationInspector {...props()} />);
    await userEvent.click(screen.getByRole("tab", { name: "Memory" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "memory source offline",
    );
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
});
