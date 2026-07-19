// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatApp } from "./ChatApp";

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
  listTools: vi.fn(),
  getToolCatalog: vi.fn(),
  updateSession: vi.fn(),
  getInspector: vi.fn(),
  listMemories: vi.fn(),
  getLibrarySummary: vi.fn(),
  deleteMemory: vi.fn(),
  useInlineVoiceLive: vi.fn(),
  appendVoiceTurns: vi.fn(),
}));

vi.mock("@/lib/api", () => mocks);
vi.mock("@/lib/inspector", () => ({
  getInspector: mocks.getInspector,
  listMemories: mocks.listMemories,
  getLibrarySummary: mocks.getLibrarySummary,
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
    onRetryUpload,
    onDismissUpload,
  }: {
    onSend: (text: string) => void;
    onUpload: (file: File) => Promise<void>;
    uploads: { id: string; filename: string; status: string }[];
    uploading: boolean;
    onRetryUpload: (id: string) => void;
    onDismissUpload: (id: string) => void;
  }) => (
    <>
      <button
        type="button"
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
    messages: { id: string; content: string }[];
  }) => (
    <div aria-label="Conversation">
      {messages.map((message) => (
        <div key={message.id}>{message.content}</div>
      ))}
    </div>
  ),
}));
vi.mock("./InlineVoiceLive", () => ({
  InlineVoiceLiveStatus: () => null,
  mergeDisplayMessages: (messages: unknown[]) => messages,
  voiceMessagesForSession: () => [],
  useInlineVoiceLive: mocks.useInlineVoiceLive,
}));
vi.mock("./StudioPanel", () => ({
  // Minimal stand-in exposing only the "run a workflow" callback, which is
  // the one call site of selectSession() outside the sidebar.
  StudioPanel: ({ onRun }: { onRun: (sessionId: string) => void }) => (
    <button type="button" onClick={() => onRun("A")}>
      Run workflow for Session A
    </button>
  ),
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
  mocks.appendVoiceTurns.mockResolvedValue([]);
  mocks.listDocuments.mockResolvedValue([]);
  mocks.listLibraryDocuments.mockResolvedValue([]);
  mocks.listSharedWithMe.mockResolvedValue([]);
  mocks.createSession.mockImplementation(async (value: object) => ({
    ...session("C"),
    ...value,
  }));
  mocks.streamChat.mockResolvedValue(undefined);
  mocks.associateLibraryDocument.mockImplementation(
    async (sessionId: string, documentId: string) => ({
      ...session(sessionId),
      libraryDocumentIds: [documentId],
    }),
  );
  mocks.deleteSession.mockResolvedValue(undefined);
  mocks.listTools.mockResolvedValue([]);
  mocks.getToolCatalog.mockImplementation(async () => ({
    tools: await mocks.listTools(),
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
    instructions: { source: "session", editable: true, value: "", agentName: null },
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
  mocks.useInlineVoiceLive.mockReturnValue({
    messages: [],
    enabled: false,
    supported: false,
    active: false,
    saving: false,
    phase: "idle",
    statusLabel: "",
    agentLabel: "",
    error: null,
    persistenceError: null,
    hasUnsavedTurns: false,
    exitLocked: false,
    boundSessionId: null,
    start: vi.fn(),
    stop: vi.fn(),
    retryPersistence: vi.fn(),
    discardPersistence: vi.fn(),
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
      recent: [libraryDocument("doc-1", "brief.pdf")],
      maxUploadBytes: 100,
      maxDocuments: 100,
      modalities: ["document"],
    });
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    await user.type(
      screen.getByRole("textbox", { name: "System prompt" }),
      "Draft prompt",
    );
    await user.click(screen.getByRole("tab", { name: "Agent & tools" }));
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
      expect(mocks.createSession).toHaveBeenCalledWith({
        model: "gpt-5.2",
        systemPrompt: "Draft prompt",
        agentName: "researcher",
        toolOverrides: { added: ["calculator"], removed: [] },
        libraryDocumentIds: ["doc-1"],
      }),
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

  it("hard-disables sidebar navigation with an explanatory, recoverable tooltip while a voice transcript is stuck saving, then re-enables once it resolves", async () => {
    const recoveryTooltip =
      "Finish saving the voice transcript before switching conversations. Use \u201cRetry saving\u201d or \u201cStop waiting\u201d in the voice status bar below.";
    const user = userEvent.setup();
    const { rerender } = render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    expect(
      await screen.findByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // Voice now has an unsaved transcript stuck saving for the active session.
    mocks.useInlineVoiceLive.mockReturnValue({
      messages: [],
      enabled: true,
      supported: true,
      active: false,
      saving: true,
      phase: "idle",
      statusLabel: "Saving voice transcript…",
      agentLabel: "",
      error: null,
      persistenceError: null,
      hasUnsavedTurns: true,
      exitLocked: true,
      boundSessionId: "A",
      start: vi.fn(),
      stop: vi.fn(),
      retryPersistence: vi.fn(),
      discardPersistence: vi.fn(),
    });
    rerender(<ChatApp />);

    const sessionBButton = screen.getByRole("button", { name: "Session B" });
    const newChatButton = screen.getByRole("button", { name: "+ New chat" });
    const deleteButton = screen.getByRole("button", { name: "Delete Session A" });
    const headerRename = document.querySelector(
      ".chat-header .editable-session-title-trigger",
    );
    for (const control of [sessionBButton, newChatButton, deleteButton, headerRename]) {
      expect(control).not.toBeDisabled();
      expect(control).toHaveAttribute("aria-disabled", "true");
      const describedById = control?.getAttribute("aria-describedby");
      expect(describedById).toBeTruthy();
      expect(document.getElementById(describedById!)).toHaveTextContent(recoveryTooltip);
    }

    // Clicking disabled controls is inert: no navigation, no deletion.
    await user.click(sessionBButton);
    await user.click(newChatButton);
    await user.click(deleteButton);
    expect(mocks.deleteSession).not.toHaveBeenCalled();
    expect(
      screen.getByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("New conversation", { selector: "strong" })).toBeNull();

    // Once the transcript is saved (or discarded), navigation recovers.
    mocks.useInlineVoiceLive.mockReturnValue({
      messages: [],
      enabled: true,
      supported: true,
      active: false,
      saving: false,
      phase: "idle",
      statusLabel: "",
      agentLabel: "",
      error: null,
      persistenceError: null,
      hasUnsavedTurns: false,
      exitLocked: false,
      boundSessionId: "A",
      start: vi.fn(),
      stop: vi.fn(),
      retryPersistence: vi.fn(),
      discardPersistence: vi.fn(),
    });
    rerender(<ChatApp />);
    expect(screen.getByRole("button", { name: "Session B" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Session B" })).not.toHaveAttribute("title");
    expect(screen.getByRole("button", { name: "Session B" })).not.toHaveAttribute(
      "aria-disabled",
    );
    expect(screen.getByRole("button", { name: "Session B" })).not.toHaveAttribute(
      "aria-describedby",
    );
    await user.click(screen.getByRole("button", { name: "Session B" }));
    expect(
      await screen.findByText("Session B", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  it("still surfaces the navigation-lock recovery message for callers outside the sidebar, such as Studio's run-workflow action", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    // Open Studio while everything is still unlocked, so its "run workflow"
    // action (a non-sidebar caller of selectSession) is available.
    await user.click(screen.getByRole("button", { name: /Agents & workflows/ }));
    await screen.findByRole("button", { name: "Run workflow for Session A" });

    mocks.useInlineVoiceLive.mockReturnValue({
      messages: [],
      enabled: true,
      supported: true,
      active: false,
      saving: true,
      phase: "idle",
      statusLabel: "Saving voice transcript…",
      agentLabel: "",
      error: null,
      persistenceError: null,
      hasUnsavedTurns: true,
      exitLocked: true,
      boundSessionId: "A",
      start: vi.fn(),
      stop: vi.fn(),
      retryPersistence: vi.fn(),
      discardPersistence: vi.fn(),
    });
    // Commit the locked state (and its layout-effect-synced ref) before the
    // next interaction, mirroring how a real state transition would already
    // be committed by the time a user's next click occurs.
    rerender(<ChatApp />);
    const runWorkflow = screen.getByRole("button", {
      name: "Run workflow for Session A",
    });

    // Studio isn't gated by the sidebar's disabled prop, so this reaches
    // selectSession() directly and exercises its guard clause. Its banner
    // ends in "...to continue.", distinct from the Sidebar's/header's
    // "...below." hint text, so this also confirms selectSession's own
    // error banner (not one of the lock hints) is what actually rendered.
    await user.click(runWorkflow);
    expect(
      await screen.findByText(/in the voice status bar to continue\./),
    ).toBeInTheDocument();
  });

  // Regression (independent re-review, HIGH): ensureSession() mutates
  // sessionIdRef/activeId unconditionally as soon as its internal
  // api.createSession() call resolves. A caller-side check on the returned
  // promise (like InlineVoiceLive's old shared abandonedRef) is powerless to
  // stop that -- the mutation already happened. These two tests drive the
  // *real* ensureSession callback ChatApp passes into the (mocked)
  // useInlineVoiceLive hook -- not a test double -- to prove the fix's two
  // independent gates (selection generation, and the caller's own
  // isStillWanted predicate) each independently stop a stale creation from
  // clobbering navigation, while the session itself still always joins the
  // sidebar since it's real and already persisted either way.
  it("does not let a voice session creation that is still in flight when the user navigates away drag navigation back once it resolves", async () => {
    let resolveCreate!: (value: ReturnType<typeof session>) => void;
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    const { ensureSession } = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };

    // Voice Live starts its very first turn and begins creating a session.
    // The predicate reports "still wanted" for as long as this call stays
    // outstanding -- this particular attempt is never itself discarded.
    let creationResult: Promise<string> | undefined;
    act(() => {
      creationResult = ensureSession(() => true);
    });

    // Before that creation resolves, the user navigates to an existing
    // session -- a real navigation, so it bumps selectionGenerationRef.
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    expect(
      await screen.findByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // The stale creation now finally resolves. It must still join the
    // sidebar (it's a real, already-persisted session on the backend either
    // way) but must not drag navigation back to it out from under the user
    // who already moved on.
    await act(async () => {
      resolveCreate(session("C"));
      await creationResult;
    });

    expect(
      screen.getByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Session C" }),
    ).toBeInTheDocument();
  });

  it("does not let a discarded voice session creation switch navigation away from the still-blank view once it resolves", async () => {
    let resolveCreate!: (value: ReturnType<typeof session>) => void;
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve;
        }),
    );
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    const { ensureSession } = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };

    // This time the user discards the voice transcript before creation
    // resolves, without navigating anywhere else -- selectionGenerationRef
    // never changes, so only the caller's isStillWanted predicate can catch
    // this.
    let creationResult: Promise<string> | undefined;
    act(() => {
      creationResult = ensureSession(() => false);
    });

    await act(async () => {
      resolveCreate(session("C"));
      await creationResult;
    });

    // Still on the blank view: the discarded attempt must not have
    // force-navigated the user to the session it created behind the scenes.
    expect(
      screen.getByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();
    // The session is still real and persisted, so it must still show up in
    // the sidebar for the user to open manually if they want it.
    expect(
      await screen.findByRole("button", { name: "Session C" }),
    ).toBeInTheDocument();
  });

  // Regression (final acceptance review, Finding 1): creatingRef only ever
  // deduplicated the underlying network call -- the activation gate itself
  // used to run ONCE, inside that shared promise, evaluating only the FIRST
  // caller's isStillWanted. A later, still-valid caller (e.g. a text send
  // that starts moments after Voice Live kicks off a creation it then
  // abandons) shared that same in-flight promise and had its own "yes"
  // silently discarded. Each caller must now get its own independent
  // activation check after the shared network call resolves.
  it("lets a later still-wanted caller activate a session even though an earlier caller sharing its in-flight creation was discarded", async () => {
    let resolveCreate!: (value: ReturnType<typeof session>) => void;
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve;
        }),
    );
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    const { ensureSession } = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };

    // Two callers race for the exact same in-flight creation, back-to-back
    // with no await between them so both see creatingRef already set and
    // share it. The first (Voice Live) is already abandoned by the time it
    // asks; the second (e.g. a text send/upload moments later) still wants
    // whatever session that shared creation produces.
    let firstResult: Promise<string> | undefined;
    let secondResult: Promise<string> | undefined;
    act(() => {
      firstResult = ensureSession(() => false);
      secondResult = ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolveCreate(session("C"));
      await Promise.all([firstResult, secondResult]);
    });

    // The second caller's "yes" must win: sharing an in-flight creation with
    // an already-discarded caller must never silently veto a later caller's
    // own, still-valid activation.
    expect(
      await screen.findByText("Session C", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
  });

  // Regression (final acceptance review, Finding 2): persistVoiceConversation
  // used to gate its client-side commits only on the session-generation
  // check -- it never re-checked the caller's (InlineVoiceLive's) own
  // attempt-validity signal, so a save already in flight when the user
  // discarded that exact voice attempt would still blindly apply its result
  // once appendVoiceTurns finally resolved. These two tests drive the *real*
  // persistVoiceConversation callback ChatApp passes into the (mocked)
  // useInlineVoiceLive hook -- not a test double -- to prove each of its two
  // independent gates (the caller's isStillValid predicate, and the
  // pre-existing session-generation check) can, on its own, keep a stale
  // voice save's content out of the transcript.
  it("keeps a discarded voice save's content out of the transcript even when the active session never changes", async () => {
    let resolveAppend!: (
      value: Awaited<ReturnType<typeof mocks.appendVoiceTurns>>,
    ) => void;
    mocks.appendVoiceTurns.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveAppend = resolve;
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

    const { persistConversation } = mocks.useInlineVoiceLive.mock.calls.at(
      -1,
    )![0] as {
      persistConversation: (
        sessionId: string,
        conversationId: string,
        turns: { role: "user" | "assistant"; text: string }[],
        isStillValid: () => boolean,
      ) => Promise<void>;
    };

    // A voice save for the currently active session is still in flight when
    // the exact attempt that started it gets discarded (Discard, or a newer
    // voice cycle beginning) -- nothing about the active session changes.
    let stillValid = true;
    let persistResult: Promise<void> | undefined;
    act(() => {
      persistResult = persistConversation(
        "A",
        "conversation-1",
        [{ role: "user", text: "Stale voice turn" }],
        () => stillValid,
      );
    });
    stillValid = false;

    await act(async () => {
      resolveAppend([
        {
          id: "m1",
          sessionId: "A",
          userId: "u1",
          role: "user",
          content: "Stale voice turn",
          status: "complete",
          model: null,
          agent: null,
          createdAt: "",
          source: "voice",
        },
      ]);
      await persistResult;
      // Flush the fire-and-forget reconcile/refresh chain
      // persistVoiceConversation kicks off after the append settles, so the
      // assertion below covers that later work too, not just the immediate
      // append result.
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.queryByText("Stale voice turn")).not.toBeInTheDocument();
  });

  it("keeps a voice save's content out of the transcript when the user navigates away and back before it resolves, even though the attempt itself was never invalidated", async () => {
    let resolveAppend!: (
      value: Awaited<ReturnType<typeof mocks.appendVoiceTurns>>,
    ) => void;
    mocks.appendVoiceTurns.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveAppend = resolve;
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

    const { persistConversation } = mocks.useInlineVoiceLive.mock.calls.at(
      -1,
    )![0] as {
      persistConversation: (
        sessionId: string,
        conversationId: string,
        turns: { role: "user" | "assistant"; text: string }[],
        isStillValid: () => boolean,
      ) => Promise<void>;
    };

    // The attempt itself is never invalidated -- isStillValid stays true for
    // the whole test -- but the user navigates A -> B -> A before the save
    // resolves. Only the session-generation half of the gate (not
    // isStillValid) can catch this: the session id matches again ("A"), but
    // its generation has moved on.
    let persistResult: Promise<void> | undefined;
    act(() => {
      persistResult = persistConversation(
        "A",
        "conversation-1",
        [{ role: "user", text: "Stale voice turn" }],
        () => true,
      );
    });

    await user.click(await screen.findByRole("button", { name: "Session B" }));
    expect(
      await screen.findByText("Session B", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    expect(
      await screen.findByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    await act(async () => {
      resolveAppend([
        {
          id: "m1",
          sessionId: "A",
          userId: "u1",
          role: "user",
          content: "Stale voice turn",
          status: "complete",
          model: null,
          agent: null,
          createdAt: "",
          source: "voice",
        },
      ]);
      await persistResult;
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    expect(screen.queryByText("Stale voice turn")).not.toBeInTheDocument();
  });
});
