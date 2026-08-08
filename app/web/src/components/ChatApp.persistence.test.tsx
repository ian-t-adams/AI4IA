// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatApp } from "./ChatApp";
import type { StreamHandlers } from "@/lib/api";
import type { ToolCatalogItem } from "@/lib/types";

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(),
  listSessions: vi.fn(),
  listAgents: vi.fn(),
  getAttachmentCapabilities: vi.fn(),
  listMessages: vi.fn(),
  listDocuments: vi.fn(),
  listLibraryDocuments: vi.fn(),
  listSharedWithMe: vi.fn(),
  createSession: vi.fn(),
  streamChat: vi.fn(),
  uploadLibraryDocument: vi.fn(),
  uploadDocument: vi.fn(),
  associateLibraryDocument: vi.fn(),
  deleteSession: vi.fn(),
  toolCatalog: [] as ToolCatalogItem[],
  getToolCatalog: vi.fn(),
  updateSession: vi.fn(),
  getInspector: vi.fn(),
  listMemories: vi.fn(),
  getLibrarySummary: vi.fn(),
  createMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  appendVoiceTurns: vi.fn(),
  voiceOptions: null as null | {
    persistConversation: (
      sessionId: string,
      conversationId: string,
      turns: { role: "user" | "assistant"; text: string }[],
      isStillValid: () => boolean,
    ) => Promise<void>;
  },
}));

vi.mock("@/lib/api", () => mocks);

function mockToolCatalog(tools: ToolCatalogItem[]): void {
  mocks.toolCatalog = tools;
}
vi.mock("@/lib/inspector", () => ({
  getInspector: mocks.getInspector,
  listMemories: mocks.listMemories,
  getLibrarySummary: mocks.getLibrarySummary,
  createMemory: mocks.createMemory,
  updateMemory: mocks.updateMemory,
  deleteMemory: mocks.deleteMemory,
}));
vi.mock("./VoiceLiveProvider", () => ({
  useVoiceLiveConfig: () => ({ enabled: false, toolsAvailable: false }),
}));
vi.mock("./LibraryProvider", () => ({
  useLibraryConfig: () => ({ enabled: true }),
}));
vi.mock("./CustomToolsProvider", () => ({
  useCustomToolsConfig: () => ({ enabled: false }),
}));
vi.mock("./AdminLink", () => ({ AdminLink: () => null }));
vi.mock("./UserMenu", () => ({ UserMenu: () => null }));
vi.mock("./Composer", () => ({
  Composer: ({
    onSend,
    onUpload,
    uploads,
    uploading,
    streaming,
    onRetryUpload,
    onDismissUpload,
  }: {
    onSend: (text: string) => void;
    onUpload: (file: File) => Promise<void>;
    uploads: { id: string; filename: string; status: string }[];
    uploading: boolean;
    streaming: boolean;
    onRetryUpload: (id: string) => void;
    onDismissUpload: (id: string) => void;
  }) => (
    <>
      <button
        type="button"
        disabled={streaming}
        onClick={() => onSend("hello from draft")}
      >
        Send draft message
      </button>
      <button
        type="button"
        onClick={() => {
          void onUpload(new File(["a"], "a.pdf", { type: "application/pdf" }));
          void onUpload(new File(["b"], "b.pdf", { type: "application/pdf" }));
        }}
      >
        Queue two uploads
      </button>
      <div aria-label="Upload status" aria-busy={uploading}>
        {uploads.map((upload) => `${upload.filename}:${upload.status}`).join(",")}
      </div>
      {uploads
        .filter((upload) => upload.status === "failed")
        .map((upload) => (
          <span key={upload.id}>
            <button type="button" onClick={() => onRetryUpload(upload.id)}>
              Retry {upload.filename}
            </button>
            <button type="button" onClick={() => onDismissUpload(upload.id)}>
              Dismiss {upload.filename}
            </button>
          </span>
        ))}
    </>
  ),
}));
vi.mock("./MessageList", () => ({
  MessageList: ({
    messages,
  }: {
    messages: {
      id: string;
      content: string;
      steps?: { kind: string; label: string }[] | null;
      source?: string;
      agent?: string | null;
      attachments?: unknown[];
    }[];
  }) => (
    <div aria-label="Conversation">
      {messages.map((message) => (
        <div
          key={message.id}
          data-message-id={message.id}
          data-source={message.source}
          data-agent={message.agent ?? undefined}
          data-attachment-count={message.attachments?.length ?? 0}
        >
          {message.content}
          {(message.steps ?? []).map((step) => (
            <span key={`${step.kind}-${step.label}`}>{step.label}</span>
          ))}
        </div>
      ))}
    </div>
  ),
}));
vi.mock("./InlineVoiceLive", () => ({
  InlineVoiceLiveStatus: () => null,
  mergeDisplayMessages: (messages: unknown[]) => messages,
  voiceMessagesForSession: () => [],
  useInlineVoiceLive: (options: typeof mocks.voiceOptions) => {
    mocks.voiceOptions = options;
    return {
      active: false,
      supported: false,
      phase: "idle",
      saving: false,
      persistenceError: null,
      error: null,
      start: vi.fn(),
      stop: vi.fn(),
      exitLocked: false,
      messages: [],
      boundSessionId: null,
    };
  },
}));

const session = (id: string) => ({
  id,
  userId: "u1",
  title: `Session ${id}`,
  titleSource: "auto" as const,
  model: "gpt-5.2",
  systemPrompt: null,
  agentName: null,
  toolOverrides: { added: [], removed: [] },
  libraryDocumentIds: [],
  createdAt: "",
  updatedAt: "",
});

const libraryDocument = (id: string, filename: string) => ({
  id,
  userId: "u1",
  filename,
  contentType: "application/pdf",
  size: 1,
  status: "ready",
  modality: "document",
  chunkCount: 1,
  citationReady: true,
  error: null,
  createdAt: "",
  updatedAt: "",
});

const chatMessage = (
  id: string,
  role: "user" | "assistant",
  content: string,
  status: "complete" | "streaming" | "cancelled" | "error" = "complete",
) => ({
  id,
  sessionId: "A",
  userId: "u1",
  role,
  content,
  status,
  model: "gpt-5.2",
  agent: null,
  createdAt: role === "user" ? "2026-07-19T08:00:00Z" : "2026-07-19T08:00:01Z",
});

function captureStreamHandlers(): () => StreamHandlers {
  let handlers: StreamHandlers | null = null;
  mocks.streamChat.mockImplementation(
    (_input: unknown, nextHandlers: StreamHandlers) => {
      handlers = nextHandlers;
      return vi.fn();
    },
  );
  return () => {
    if (!handlers) throw new Error("stream handlers were not registered");
    return handlers;
  };
}

beforeEach(() => {
  const sessions = [session("A"), session("B")];
  mocks.listModels.mockResolvedValue({
    models: [
      {
        id: "gpt-5.2",
        displayName: "GPT-5.2",
        category: "chat",
        format: "openai",
        conversational: true,
        contextWindow: 128000,
        maxOutputTokens: 32000,
        options: [],
      },
    ],
  });
  mocks.listSessions.mockResolvedValue(sessions);
  mocks.listAgents.mockResolvedValue([]);
  mocks.getAttachmentCapabilities.mockResolvedValue({
    ingestPath: "library",
    maxBytes: 1_000_000,
    maxPerUserDocuments: 100,
    maxPerSessionDocuments: 20,
    extensions: [".pdf"],
    mimeTypes: ["application/pdf"],
    modalities: ["document"],
  });
  mocks.listMessages.mockResolvedValue([]);
  mocks.listDocuments.mockResolvedValue([]);
  mocks.listLibraryDocuments.mockResolvedValue([]);
  mocks.listSharedWithMe.mockResolvedValue([]);
  mocks.createSession.mockImplementation(async (value: object) => ({
    ...session("C"),
    ...value,
  }));
  mocks.streamChat.mockReturnValue(vi.fn());
  mocks.appendVoiceTurns.mockResolvedValue([]);
  mocks.voiceOptions = null;
  mocks.associateLibraryDocument.mockImplementation(
    async (sessionId: string, documentId: string) => ({
      ...session(sessionId),
      libraryDocumentIds: [documentId],
    }),
  );
  mocks.deleteSession.mockResolvedValue(undefined);
  mockToolCatalog([]);
  mocks.getToolCatalog.mockImplementation(async () => ({
    tools: mocks.toolCatalog,
    inheritedTools: [],
  }));
  mocks.updateSession.mockImplementation(async (id: string, value: object) => ({
    ...session(id),
    ...value,
  }));
  mocks.getInspector.mockImplementation(async (id: string) => ({
    generatedAt: new Date().toISOString(),
    sessionId: id,
    title: `Session ${id}`,
    model: {
      id: "gpt-5.2",
      displayName: "GPT-5.2",
      contextWindow: 128000,
      maxOutputTokens: 32000,
    },
    instructions: { source: "session", editable: true, value: "", agentName: null, agentSource: null },
    agent: { name: null, displayName: null, description: null },
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
      totalRequests: 0,
      totalPromptTokens: 0,
      totalCompletionTokens: 0,
      totalTokens: 0,
      totalCostMicroUsd: 0,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
      latest: null,
      truncated: false,
      coveredRequests: 0,
      coverageStart: null,
      coverageEnd: null,
    },
    monthlyUsage: {
      totalRequests: 0,
      totalTokens: 0,
      totalCostMicroUsd: 0,
      unknownUsageRequests: 0,
      costUnknownRequests: 0,
    },
    voice: {
      defaultProviderId: null,
      enabledProviderIds: [],
      applies: "next_connection",
    },
  }));
  mocks.listMemories.mockResolvedValue({
    status: "disabled",
    supportsCreate: false,
    supportsEdit: false,
    supportsDelete: false,
    items: [],
    detail: "Memory disabled",
  });
  mocks.getLibrarySummary.mockResolvedValue({
    generatedAt: new Date().toISOString(),
    status: "ok",
    total: 0,
    byStatus: {},
    byModality: {},
    recent: [],
    maxUploadBytes: 100,
    maxDocuments: 100,
    modalities: ["document"],
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("--font-scale");
});

describe("ChatApp uploads", () => {
  it("creates the first session with complete draft defaults and never PATCHes null", async () => {
    mocks.listAgents.mockResolvedValue([
      {
        name: "researcher",
        displayName: "Researcher",
        description: "Researches",
        enabled: true,
      },
    ]);
    mockToolCatalog([
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
      recent: [libraryDocument("doc-1", "brief.pdf")],
      maxUploadBytes: 100,
      maxDocuments: 100,
      modalities: ["document"],
    });

    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(
      screen.getByRole("textbox", { name: "System prompt" }),
      "Draft prompt",
    );
    await user.click(screen.getByRole("button", { name: "Agent & tools" }));
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Agent" }),
      "researcher",
    );
    await user.click(await screen.findByRole("checkbox", { name: /Calculator/ }));
    await user.click(screen.getByRole("tab", { name: "Context" }));
    await user.click(await screen.findByRole("button", { name: "Add brief.pdf" }));

    expect(mocks.updateSession).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() =>
      expect(mocks.createSession).toHaveBeenCalledWith(
        {
          model: "gpt-5.2",
          systemPrompt: "Draft prompt",
          agentName: "researcher",
          toolOverrides: { added: ["calculator"], removed: [] },
          libraryDocumentIds: ["doc-1"],
        },
        expect.any(AbortSignal),
      ),
    );
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
  });

  it("blocks navigation and runs multi-file uploads sequentially for the captured session", async () => {
    let resolveFirst!: (value: ReturnType<typeof libraryDocument>) => void;
    let resolveSecond!: (value: ReturnType<typeof libraryDocument>) => void;
    mocks.uploadLibraryDocument
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveFirst = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve;
          }),
      );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    expect(
      await screen.findByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Queue two uploads" }));
    await waitFor(() => expect(mocks.uploadLibraryDocument).toHaveBeenCalledTimes(1));
    await user.click(screen.getByRole("button", { name: "Delete Session A" }));
    expect(mocks.deleteSession).not.toHaveBeenCalled();
    expect(
      await screen.findByText(/finish before deleting this conversation/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Session B" }));
    expect(
      await screen.findByText(/Wait for active attachments to finish/),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    resolveFirst(libraryDocument("d1", "a.pdf"));
    await waitFor(() =>
      expect(mocks.associateLibraryDocument).toHaveBeenCalledWith("A", "d1"),
    );
    await waitFor(() => expect(mocks.uploadLibraryDocument).toHaveBeenCalledTimes(2));
    expect(screen.getByLabelText("Upload status")).toHaveAttribute("aria-busy", "true");
    resolveSecond(libraryDocument("d2", "b.pdf"));
    await waitFor(() =>
      expect(mocks.associateLibraryDocument).toHaveBeenCalledWith("A", "d2"),
    );
    expect(mocks.uploadLibraryDocument.mock.calls[0][0].name).toBe("a.pdf");
    expect(mocks.uploadLibraryDocument.mock.calls[1][0].name).toBe("b.pdf");
    await waitFor(() =>
      expect(screen.getByLabelText("Upload status")).toHaveAttribute(
        "aria-busy",
        "false",
      ),
    );
  });

  it("releases queue accounting when a queued retry is dismissed before running", async () => {
    let resolveSecond!: (value: ReturnType<typeof libraryDocument>) => void;
    mocks.uploadLibraryDocument
      .mockRejectedValueOnce(new Error("temporary"))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve;
          }),
      );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Queue two uploads" }));
    await user.click(await screen.findByRole("button", { name: "Retry a.pdf" }));
    await user.click(screen.getByRole("button", { name: "Dismiss a.pdf" }));
    resolveSecond(libraryDocument("d2", "b.pdf"));
    await waitFor(() =>
      expect(screen.getByLabelText("Upload status")).toHaveAttribute(
        "aria-busy",
        "false",
      ),
    );
    await user.click(screen.getByRole("button", { name: "+ New chat" }));
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();
  });

  it("uses actual narrow ChatApp drawers with mutual exclusion and inert background", async () => {
    Object.defineProperty(window, "innerWidth", { value: 320, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 240, configurable: true });
    document.documentElement.style.setProperty("--font-scale", "2");
    vi.stubGlobal(
      "matchMedia",
      vi.fn((query: string) => ({
        matches:
          query === "(max-width: 720px)" || query === "(max-width: 1050px)",
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      })),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    const sidebarOpener = await screen.findByRole("button", {
      name: "Open conversation sidebar",
    });
    const inspectorOpener = screen.getByRole("button", {
      name: "Open conversation inspector",
    });

    await user.click(sidebarOpener);
    expect(screen.getByRole("dialog", { name: "Chat sessions" })).toBeInTheDocument();
    expect(screen.getByTestId("sidebar-scroll")).toHaveStyle({
      minHeight: "0",
      overflowY: "auto",
      overflowX: "hidden",
    });
    const statusLink = screen.getByRole("link", {
      name: "Status (opens in new tab)",
    });
    statusLink.focus();
    expect(statusLink).toHaveFocus();
    expect(document.querySelector("main")).toHaveAttribute("inert");
    expect(screen.queryByRole("dialog", { name: "Conversation inspector" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Close conversation sidebar" }));
    expect(
      await screen.findByRole("button", { name: "Open conversation sidebar" }),
    ).toHaveFocus();

    await user.click(inspectorOpener);
    expect(
      screen.getByRole("dialog", { name: "Conversation inspector" }),
    ).toHaveAttribute("aria-modal", "true");
    expect(document.querySelector("main")).toHaveAttribute("inert");
    expect(screen.queryByRole("dialog", { name: "Chat sessions" })).toBeNull();
    const main = document.querySelector("main") as HTMLElement;
    expect(main).toHaveStyle({ minWidth: "0px" });
    expect(screen.getByRole("button", { name: "Collapse conversation inspector" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Close conversation inspector" }));
    expect(
      await screen.findByRole("button", { name: "Open conversation inspector" }),
    ).toHaveFocus();
  });
});

describe("ChatApp stream reconciliation", () => {
  it("removes the optimistic user row after a pre-acceptance HTTP error", async () => {
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onError("429: rate limited", {
        accepted: false,
        persistenceFailed: false,
        definitePreAcceptance: true,
      });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    expect(screen.queryByText("hello from draft")).toBeNull();
    expect(screen.getByText("429: rate limited")).toBeInTheDocument();
  });

  it("retains local state without querying history after an ambiguous 5xx", async () => {
    mocks.listMessages.mockResolvedValue([]);
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    const view = render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onError("503: upstream reset", {
        accepted: false,
        persistenceFailed: false,
        definitePreAcceptance: false,
      });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    expect(screen.getByText("hello from draft")).toBeInTheDocument();
    expect(
      screen.getByText(/503: upstream reset.*Outcome unknown/),
    ).toBeInTheDocument();
    expect(mocks.streamChat).toHaveBeenCalledTimes(1);
    await new Promise((resolve) => window.setTimeout(resolve, 300));
    expect(mocks.listMessages).toHaveBeenCalledTimes(1);
    view.unmount();
  });

  it("materializes the exact-id fallback before unlocking and keeps it through a stale snapshot", async () => {
    let resolveHistory!: (messages: ReturnType<typeof chatMessage>[]) => void;
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveHistory = resolve;
          }),
      );
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));

    act(() => {
      handlers().onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers().onDelta("Durable fallback");
      handlers().onDone();
    });

    expect(await screen.findByText("Durable fallback")).toHaveAttribute(
      "data-message-id",
      "assistant-1",
    );
    expect(screen.getByRole("button", { name: "Send draft message" })).toBeDisabled();
    resolveHistory([
      chatMessage("user-1", "user", "hello from draft"),
      chatMessage("assistant-1", "assistant", "", "streaming"),
    ]);
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    expect(screen.getAllByText("Durable fallback")).toHaveLength(1);

    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    expect(mocks.streamChat).toHaveBeenCalledTimes(2);
  });

  it("preserves buffered text on failed fetch and missing metadata", async () => {
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockRejectedValueOnce(new Error("history unavailable"));
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers().onDelta("Keep after fetch failure");
      handlers().onError("save failed", {
        accepted: true,
        persistenceFailed: true,
      });
    });
    expect(await screen.findByText("Keep after fetch failure")).toHaveAttribute(
      "data-message-id",
      "assistant-1",
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );

    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onDelta("Truncated but visible");
      handlers().onError("Stream completed without message metadata.", {
        accepted: false,
        persistenceFailed: false,
      });
    });
    expect(await screen.findByText("Truncated but visible")).toBeInTheDocument();
    expect(
      await screen.findByText(
        /Stream completed without message metadata.*Outcome unknown/,
      ),
    ).toBeInTheDocument();
    expect(mocks.streamChat).toHaveBeenCalledTimes(2);
  });

  it("keeps interleaved same-source turns unbound until navigation reload", async () => {
    const authoritative = [
      chatMessage("turn-a-user", "user", "hello from draft"),
      chatMessage("turn-a-assistant", "assistant", "Possible answer A"),
      chatMessage("turn-b-user", "user", "hello from draft"),
      chatMessage("turn-b-assistant", "assistant", "Possible answer B"),
    ];
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce(authoritative);
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onDelta("Outcome-unknown fallback");
      handlers().onError("Stream ended unexpectedly.", {
        accepted: false,
        persistenceFailed: false,
        definitePreAcceptance: false,
      });
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    expect(
      screen.getByText("Outcome-unknown fallback").getAttribute("data-message-id"),
    ).toMatch(/^tmp-assistant-/);
    expect(screen.getAllByText("hello from draft")).toHaveLength(1);
    expect(screen.getByText(/Outcome unknown/)).toBeInTheDocument();
    await new Promise((resolve) => window.setTimeout(resolve, 300));
    expect(mocks.listMessages).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Possible answer A")).toBeNull();
    expect(screen.queryByText("Possible answer B")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Session B" }));
    await screen.findByText("Session B", {
      selector: ".chat-header .editable-session-title-text",
    });
    await user.click(screen.getByRole("button", { name: "Session A" }));
    expect(await screen.findByText("Possible answer A")).toHaveAttribute(
      "data-message-id",
      "turn-a-assistant",
    );
    expect(screen.getByText("Possible answer B")).toHaveAttribute(
      "data-message-id",
      "turn-b-assistant",
    );
    expect(screen.queryByText("Outcome-unknown fallback")).toBeNull();
    expect(document.querySelector('[data-message-id^="tmp-"]')).toBeNull();
  });

  it("keeps a suppressed command honest without inventing assistant metadata", async () => {
    mocks.listMessages.mockResolvedValueOnce([]);
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    const view = render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onError(
        "The command result was superseded before it could be saved.",
        {
          accepted: false,
          persistenceFailed: false,
          definitePreAcceptance: false,
        },
      );
    });

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    expect(screen.getAllByText("hello from draft")).toHaveLength(1);
    expect(screen.getByText("hello from draft").getAttribute("data-message-id")).toMatch(
      /^tmp-/,
    );
    expect(
      screen.getByText(/superseded before it could be saved.*Outcome unknown/),
    ).toBeInTheDocument();
    expect(
      document.querySelector('[data-message-id^="tmp-assistant-"]'),
    ).toBeNull();
    await new Promise((resolve) => window.setTimeout(resolve, 300));
    expect(mocks.listMessages).toHaveBeenCalledTimes(1);
    view.unmount();
  });

  it("deduplicates same-id rows and retains only finalized activity", async () => {
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        chatMessage("user-1", "user", "hello from draft"),
        {
          ...chatMessage("assistant-1", "assistant", "Authoritative answer"),
          steps: [{ kind: "tool_result", label: "Searched the web" }],
        },
      ]);
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers().onStep?.({ kind: "tool_start", label: "Searching the web" });
      handlers().onStep?.({ kind: "tool_result", label: "Searched the web" });
      handlers().onDelta("Buffered answer");
      handlers().onDone();
    });
    expect(await screen.findByText("Authoritative answer")).toHaveAttribute(
      "data-message-id",
      "assistant-1",
    );
    expect(screen.getAllByText("Authoritative answer")).toHaveLength(1);
    expect(screen.getByText("Searched the web")).toBeInTheDocument();
    expect(screen.queryByText("Searching the web")).toBeNull();
  });

  it("does not overlay fallback onto a concurrent same-id terminal row", async () => {
    mocks.listMessages.mockResolvedValueOnce([]).mockResolvedValue([
      chatMessage("user-1", "user", "hello from draft"),
      chatMessage("assistant-1", "assistant", "", "streaming"),
    ]);
    mocks.appendVoiceTurns.mockResolvedValueOnce([
      {
        ...chatMessage(
          "assistant-1",
          "assistant",
          "Authoritative concurrent answer",
        ),
        source: "voice",
        agent: "voice-agent",
        attachments: [
          {
            id: "artifact-1",
            kind: "image",
            mimeType: "image/png",
            prompt: null,
            model: "image-model",
            size: "1024x1024",
          },
        ],
      },
    ]);
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers().onDelta("Short buffered fallback");
    });
    await act(async () => {
      await mocks.voiceOptions!.persistConversation(
        "A",
        "conversation",
        [{ role: "assistant", text: "Authoritative concurrent answer" }],
        () => true,
      );
    });
    const authoritative = await screen.findByText(
      "Authoritative concurrent answer",
    );
    act(() => handlers().onDone());

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    expect(authoritative).toHaveAttribute("data-message-id", "assistant-1");
    expect(authoritative).toHaveAttribute("data-source", "voice");
    expect(authoritative).toHaveAttribute("data-agent", "voice-agent");
    expect(authoritative).toHaveAttribute("data-attachment-count", "1");
    expect(screen.queryByText("Short buffered fallback")).toBeNull();
  });

  it("keeps Voice history monotonic when a stale refresh regresses the same id", async () => {
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        chatMessage("voice-assistant", "assistant", "", "streaming"),
      ]);
    mocks.appendVoiceTurns.mockResolvedValueOnce([
      {
        ...chatMessage("voice-assistant", "assistant", "Finished voice answer"),
        source: "voice",
      },
    ]);
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await act(async () => {
      await mocks.voiceOptions!.persistConversation(
        "A",
        "conversation",
        [{ role: "assistant", text: "Finished voice answer" }],
        () => true,
      );
    });
    expect(await screen.findByText("Finished voice answer")).toHaveAttribute(
      "data-message-id",
      "voice-assistant",
    );
  });

  it("reconciles a known accepted assistant when returning after navigation", async () => {
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        chatMessage("user-1", "user", "hello from draft"),
        chatMessage("assistant-1", "assistant", "", "streaming"),
      ])
      .mockResolvedValueOnce([
        chatMessage("user-1", "user", "hello from draft"),
        chatMessage("assistant-1", "assistant", "Reconciled after return"),
      ]);
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers().onDelta("Local fallback");
      handlers().onDone();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );

    await user.click(screen.getByRole("button", { name: "Session B" }));
    await screen.findByText("Session B", {
      selector: ".chat-header .editable-session-title-text",
    });
    await user.click(screen.getByRole("button", { name: "Session A" }));
    expect(await screen.findByText("Reconciled after return")).toHaveAttribute(
      "data-message-id",
      "assistant-1",
    );
  });

  it("ignores an in-flight old-turn poll after the next same-session turn starts", async () => {
    let resolveOldPoll!: (messages: ReturnType<typeof chatMessage>[]) => void;
    const handlers: StreamHandlers[] = [];
    mocks.streamChat.mockImplementation(
      (_input: unknown, nextHandlers: StreamHandlers) => {
        handlers.push(nextHandlers);
        return vi.fn();
      },
    );
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([
        chatMessage("user-1", "user", "hello from draft"),
        chatMessage("assistant-1", "assistant", "", "streaming"),
      ])
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveOldPoll = resolve;
          }),
      );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers[0].onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers[0].onDelta("First fallback");
      handlers[0].onDone();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    await waitFor(
      () => expect(mocks.listMessages).toHaveBeenCalledTimes(3),
      { timeout: 1000 },
    );

    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    expect(handlers).toHaveLength(2);
    await act(async () => {
      resolveOldPoll([
        chatMessage("user-1", "user", "hello from draft"),
        chatMessage("assistant-1", "assistant", "Old poll must be ignored"),
      ]);
      await Promise.resolve();
    });
    expect(screen.queryByText("Old poll must be ignored")).toBeNull();
    expect(screen.getByText("First fallback")).toBeInTheDocument();
    expect(mocks.streamChat).toHaveBeenCalledTimes(2);
  });

  it("ignores an in-flight reconciliation result after unmount", async () => {
    let resolvePoll!: (messages: ReturnType<typeof chatMessage>[]) => void;
    mocks.listMessages
      .mockResolvedValueOnce([])
      .mockResolvedValueOnce([])
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePoll = resolve;
          }),
      );
    const handlers = captureStreamHandlers();
    const user = userEvent.setup();
    const view = render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    act(() => {
      handlers().onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers().onDelta("Awaiting persistence");
      handlers().onDone();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );
    await waitFor(
      () => expect(mocks.listMessages).toHaveBeenCalledTimes(3),
      { timeout: 1000 },
    );
    view.unmount();
    await act(async () => {
      resolvePoll([
        chatMessage("assistant-1", "assistant", "Must not merge after unmount"),
      ]);
      await new Promise((resolve) => window.setTimeout(resolve, 300));
    });
    expect(mocks.listMessages).toHaveBeenCalledTimes(3);
  });
});
