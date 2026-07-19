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

  // Regression (independent re-review, HIGH 2): creatingRef only ever
  // deduplicated the underlying network call by presence -- any caller that
  // reused an in-flight creatingRef promise got a session built from
  // whatever model/systemPrompt/agent/tools/docs the FIRST caller's settings
  // happened to be at the time, even if a LATER caller's own current
  // settings had since diverged (e.g. after Stop waiting + New chat resets
  // them, or the user just edits the draft again). This test drives the
  // *real* ensureSession callback ChatApp passes into the (mocked)
  // useInlineVoiceLive hook, changing the real system prompt via the real
  // (unmocked) ConversationInspector between two calls, to prove a caller
  // whose settings have diverged from an in-flight creation fires its own
  // request instead of silently inheriting the stale one.
  it("fires its own session creation instead of reusing an in-flight one when the caller's settings have since diverged", async () => {
    const resolvers: Array<() => void> = [];
    mocks.createSession.mockImplementation(
      (value: object) =>
        new Promise((resolve) => {
          resolvers.push(() => resolve({ ...session("C"), ...value }));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // Switch the real inspector to its Instructions tab and set a draft
    // system prompt -- while still in "new chat" (no session yet), this
    // writes straight through to ChatApp's systemPrompt state.
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    const promptBox = screen.getByLabelText("System prompt");
    await user.type(promptBox, "Prompt A");

    const firstCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let firstResult: Promise<string> | undefined;
    act(() => {
      firstResult = firstCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    expect(mocks.createSession).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ systemPrompt: "Prompt A" }),
      expect.any(AbortSignal),
    );

    // Before that creation resolves, the caller's settings diverge -- change
    // the draft system prompt to a different value while still in "new
    // chat", exactly as would happen after Stop waiting + New chat, or a
    // plain draft edit.
    await user.clear(promptBox);
    await user.type(promptBox, "Prompt B");

    const secondCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let secondResult: Promise<string> | undefined;
    act(() => {
      secondResult = secondCall.ensureSession(() => true);
    });

    // The second, differently-configured caller must fire its own request
    // rather than silently reusing the first's in-flight (and now stale)
    // creation.
    expect(mocks.createSession).toHaveBeenCalledTimes(2);
    expect(mocks.createSession).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ systemPrompt: "Prompt B" }),
      expect.any(AbortSignal),
    );

    await act(async () => {
      resolvers.forEach((resolve) => resolve());
      await Promise.all([firstResult, secondResult]);
    });
  });

  // Regression (voice acceptance round 11, HIGH): creatingRef's intent
  // fingerprint used to key only on settings, never on selection
  // generation. New chat resets settings to a fixed, deterministic default
  // (same model/systemPrompt/agent/tools/docs every time) but never clears
  // creatingRef, so an abandoned voice creation started while already on
  // default settings produced the exact same fingerprint as a later
  // send/upload's creation after Stop waiting + New chat -- even though the
  // two belong to entirely different selection generations. This drives the
  // *real* ensureSession callback and the *real* New chat button to prove a
  // later caller in a new generation always fires (and activates) its own
  // request instead of silently inheriting an earlier, already-abandoned
  // generation's in-flight creation just because both generations'
  // settings happen to coincide.
  it("does not let an abandoned voice session creation from an earlier generation be reused after New chat resets back to the same default settings", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("OLD"), session("NEW")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // Voice Live begins creating a session under the current (default)
    // settings, then is abandoned before that creation resolves.
    const firstCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let firstResult: Promise<string> | undefined;
    act(() => {
      firstResult = firstCall.ensureSession(() => false);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // "New chat" bumps the selection generation and resets settings back to
    // the exact same defaults they already were -- no visible settings
    // change, but a genuinely different generation.
    await user.click(screen.getByRole("button", { name: "+ New chat" }));

    // A fresh caller (e.g. a text send) in the new generation asks for a
    // session under those (coincidentally identical) default settings.
    const secondCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let secondResult: Promise<string> | undefined;
    act(() => {
      secondResult = secondCall.ensureSession(() => true);
    });

    // Even though the settings fingerprint alone would match the still-
    // in-flight, already-abandoned first generation's creation, the new
    // generation must fire its own independent request.
    expect(mocks.createSession).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolvers.forEach((resolve) => resolve());
      await Promise.all([firstResult, secondResult]);
    });

    // The still-current (second) generation's own, freshly created session
    // must be what actually gets activated -- not the abandoned first
    // generation's.
    expect(
      await screen.findByText("Session NEW", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  // Regression (voice acceptance round 11, HIGH -- literal scenario):
  // proves the same New-chat-then-diverge flow using the real "+ New chat"
  // button (rather than editing the draft directly, as the round-10 test
  // above does) followed by genuinely different settings, end to end.
  it("fires its own session creation for a send after New chat resets settings differently than an earlier abandoned voice creation", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("OLD"), session("NEW")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    const promptBox = screen.getByLabelText("System prompt");
    await user.type(promptBox, "Voice prompt");

    const firstCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let firstResult: Promise<string> | undefined;
    act(() => {
      firstResult = firstCall.ensureSession(() => false);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    expect(mocks.createSession).toHaveBeenNthCalledWith(
      1,
      expect.objectContaining({ systemPrompt: "Voice prompt" }),
      expect.any(AbortSignal),
    );

    // Stop waiting + New chat: bumps generation and resets the system
    // prompt back to blank.
    await user.click(screen.getByRole("button", { name: "+ New chat" }));

    // The user then types a different prompt before sending.
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");

    const secondCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let secondResult: Promise<string> | undefined;
    act(() => {
      secondResult = secondCall.ensureSession(() => true);
    });

    expect(mocks.createSession).toHaveBeenCalledTimes(2);
    expect(mocks.createSession).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({ systemPrompt: "Send prompt" }),
      expect.any(AbortSignal),
    );

    await act(async () => {
      resolvers.forEach((resolve) => resolve());
      await Promise.all([firstResult, secondResult]);
    });

    expect(
      await screen.findByText("Session NEW", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  // Regression (voice acceptance round 12 test scaffolding, extended in
  // round 13): two callers with genuinely different settings (and so their
  // own creatingRef entries, per the round-10/11 fixes above) can each have
  // their own api.createSession() network call in flight at the same time.
  // These two tests drive two differently-configured concurrent
  // ensureSession() calls -- standing in for Voice Live and a typed send
  // racing each other -- and resolve their underlying creations in each
  // possible order.
  //
  // Regression (voice acceptance round 13, MEDIUM): whichever differently-
  // configured intent resolves LAST supersedes an already-active mismatched
  // one, so the conversation actually on screen always reflects the most
  // recent settings rather than whichever request happened to win the race
  // to resolve first. A Promise's own resolved value is fixed the instant
  // it settles, so the EARLIER-resolving caller's own already-returned id
  // never changes after the fact -- only the LATER caller's own return
  // value, and the active session the UI shows, end up reflecting the
  // supersession.
  it("lets the later-resolving, differently-configured (send-shaped) creation supersede the earlier-issued (voice-shaped) one that already activated", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("VOICE"), session("SEND")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // Voice Live's ensureSession call, under the current (blank) settings.
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // A concurrent, differently-configured intent (e.g. a typed send after
    // editing the draft system prompt) fires its OWN creation rather than
    // sharing voice's in-flight one -- proven by the round-10 fix above.
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    const sendCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let sendResult: Promise<string> | undefined;
    act(() => {
      sendResult = sendCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(2);

    // The earlier-issued (voice) creation resolves first. Nothing else is
    // active yet, so it activates and its OWN ensureSession() call resolves
    // to "VOICE" -- a value that, once settled, can never change.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });
    expect(
      await screen.findByText("Session VOICE", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // The later-issued (send) intent's own creation resolves *after* voice
    // already activated, under genuinely different settings. Rather than
    // silently inheriting voice's already-active session, it supersedes:
    // its own ensureSession() call resolves to its own "SEND" id, and the
    // conversation actually on screen switches to match.
    await act(async () => {
      resolvers[1]();
      await sendResult;
    });
    expect(await voiceResult).toBe("VOICE");
    expect(await sendResult).toBe("SEND");
    expect(
      await screen.findByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  it("lets the later-resolving, differently-configured (voice-shaped) creation supersede the earlier-issued (send-shaped) one that already activated", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("VOICE"), session("SEND")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    const sendCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let sendResult: Promise<string> | undefined;
    act(() => {
      sendResult = sendCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(2);

    // This time the later-issued (send) creation resolves *first* and
    // activates -- the opposite resolution order from the sibling test
    // above. Nothing else is active yet, so its own ensureSession() call
    // resolves to its own "SEND" id -- fixed the instant it settles.
    await act(async () => {
      resolvers[1]();
      await sendResult;
    });
    expect(
      await screen.findByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // Voice's own creation resolves *after* the send-shaped intent already
    // activated, under genuinely different settings. It supersedes: its
    // own ensureSession() call resolves to its own "VOICE" id, and the
    // conversation actually on screen switches to match -- the symmetric
    // mirror of the sibling test above.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });
    expect(await sendResult).toBe("SEND");
    expect(await voiceResult).toBe("VOICE");
    expect(
      await screen.findByText("Session VOICE", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  // Regression (voice acceptance round 13, MEDIUM -- real send() integration):
  // proves the later-resolving, differently-configured intent supersedes
  // through the *real* send() path, not just ensureSession's own return
  // value in isolation. A real "Send" click, under different settings than
  // a concurrent voice creation that resolves and activates first, must
  // still stream into its OWN session once its own creation resolves --
  // never silently inherit voice's already-active but differently
  // configured one, since that would run the user's message with stale
  // model/prompt/tools/docs it never asked for.
  it("streams a real send() into its own session once its creation resolves, superseding a concurrent voice-shaped session that activated first under different settings", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("VOICE"), session("SEND")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // Voice Live starts creating a session under the current (blank)
    // settings.
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // The user edits the draft system prompt and sends a real message
    // before voice's creation resolves -- send()'s own ensureSession()
    // call, with different settings, fires its own concurrent creation.
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));

    // Voice's creation resolves first. Nothing else is active yet, so it
    // activates -- but this is only a transient intermediate state here,
    // not the final outcome, since send's own differently-configured
    // creation is still in flight.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });
    expect(
      await screen.findByText("Session VOICE", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // The send's own creation resolves after voice already activated,
    // under genuinely different settings -- it supersedes.
    await act(async () => {
      resolvers[1]();
    });

    // The real send() must have streamed into its own session ("SEND")
    // once its own ensureSession() call resolved and superseded, never
    // into voice's differently-configured "VOICE" session.
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SEND" }),
      expect.anything(),
    );
    expect(
      await screen.findByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  // Regression (voice acceptance round 13 background review, HIGH): once a
  // real send() has already activated its own session AND begun actively
  // streaming into it, a later-resolving, differently-keyed intent (e.g.
  // Voice Live, racing its own creation under different settings) must
  // never be allowed to supersede it. send()/runUpload() capture their own
  // session id ONCE and never re-read activeId/sessionIdRef afterward, so
  // a superseding activation wouldn't confuse send()'s own closure (it
  // would keep correctly streaming into and reconciling its original
  // session) -- but the visible header/sidebar would flip to a different,
  // unrelated session while the message pane kept showing/updating the
  // original one, and any OTHER caller reading activeId (a second send, a
  // voice turn) would then target the wrong conversation entirely.
  it("keeps a real, already-streaming send()'s session active against a later-resolving, differently-keyed voice intent", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("VOICE"), session("SEND")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // Voice Live starts creating a session under the current (blank)
    // settings.
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // The user then edits the draft system prompt and sends a real message
    // -- send()'s own ensureSession() call, under different settings,
    // fires its own concurrent creation rather than sharing voice's.
    await user.click(screen.getByRole("tab", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));

    // send()'s OWN creation resolves FIRST this time: nothing is active
    // yet, so it activates AND immediately begins real, visible streaming
    // (the mock never calls onDone, so it stays actively "in flight" just
    // like a real in-progress response).
    await act(async () => {
      resolvers[1]();
    });
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SEND" }),
      expect.anything(),
    );
    expect(
      await screen.findByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // Voice's differently-keyed creation resolves SECOND, after send() has
    // already activated and started actively streaming into "SEND". Even
    // though voice's settings genuinely differ from what's active (a
    // mismatch that would otherwise win per the "latest-intent-wins"
    // design), it must NOT be allowed to supersede a session with a real,
    // in-flight consumer.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });

    // Voice's own call falls back to the session that's actually current
    // and in use, rather than being hijacked into its own orphaned
    // "VOICE" session that nothing is showing.
    expect(await voiceResult).toBe("SEND");
    // The header must still show "SEND" -- never flip to "VOICE" out from
    // under the actively-streaming conversation.
    expect(
      screen.getByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Session VOICE", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).not.toBeInTheDocument();
    // Only ONE stream was ever started, and only into "SEND".
    expect(mocks.streamChat).toHaveBeenCalledTimes(1);
  });

  // Regression (voice acceptance round 13, HIGH): a hung createSession()
  // network call (dropped connection, backend stall) previously stayed
  // cached in creatingRef forever -- a later Retry (after voice's own
  // PERSIST_TIMEOUT_MS fired) or a plain text send would join the exact
  // same doomed promise and hang right along with it, with no way out short
  // of a page reload. The bounded SESSION_CREATION_TIMEOUT_MS race must
  // reject the first caller with a diagnosable error AND evict the entry so
  // a subsequent attempt fires a genuinely fresh createSession() call
  // instead of re-joining the same hung promise.
  it("evicts a permanently hung session creation once the bounded timeout trips, so a retry fires a fresh request instead of hanging forever", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.createSession.mockImplementationOnce(() => new Promise(() => {}));
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    const call = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let firstResult: Promise<string> | undefined;
    act(() => {
      firstResult = call.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // Deliberately the synchronous advanceTimersByTime, not the *Async
    // variant: the async variant fires each due timer via a real
    // setImmediate so the microtask queue can drain in between, but that
    // extra hop is exactly what makes Node observe this rejection as
    // "unhandled" for one tick before our own await below attaches its
    // handler (a PromiseRejectionHandledWarning, confirmed to reproduce
    // even in a minimal Promise.race+hung-sibling repro with no relation
    // to this file's production code). The synchronous variant fires the
    // timer and lets our pre-existing `.catch` on the race/timeout chain
    // observe the rejection within the same synchronous flush.
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    await expect(firstResult).rejects.toThrow(
      "Creating the conversation is taking too long. Please try again.",
    );

    // A retry (e.g. voice's own Retry button, or a plain text send) must
    // fire a genuinely fresh createSession() call -- not silently re-join
    // the same hung promise -- and succeed normally this time.
    let secondResult: Promise<string> | undefined;
    act(() => {
      secondResult = call.ensureSession(() => true);
    });
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));
    expect(await secondResult).toBe("C");
    expect(
      await screen.findByText("Session C", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    vi.useRealTimers();
  });

  // Regression (voice acceptance round 13, HIGH): voice's "Stop waiting"
  // control calls abandonPendingSessionCreation() so a hung creation can be
  // released right away -- without this, the only way out of a hung
  // createSession() call was to wait out the full
  // SESSION_CREATION_TIMEOUT_MS bound even after the user had already asked
  // to stop waiting.
  it("lets abandonPendingSessionCreation evict a hung creation immediately, without waiting out the bounded timeout", async () => {
    // Fake timers here even though nothing is ever advanced: ensureSession
    // still schedules its own real SESSION_CREATION_TIMEOUT_MS deadline
    // internally, and this test never lets that entry settle one way or
    // the other before abandoning it. Under real timers that dangling
    // 20s timeout would still be armed (harmlessly, but noisily) well
    // after this test body returns; parking it on the fake clock instead
    // means it simply never fires, since nothing advances that clock.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    mocks.createSession.mockImplementationOnce(() => new Promise(() => {}));
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    const call = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
      abandonPendingSessionCreation: () => void;
    };
    act(() => {
      void call.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    const [, firstSignal] = mocks.createSession.mock.calls[0] as [
      unknown,
      AbortSignal,
    ];
    expect(firstSignal.aborted).toBe(false);

    act(() => {
      call.abandonPendingSessionCreation();
    });
    expect(firstSignal.aborted).toBe(true);

    // No timer advancement at all: the slot must already be free, and a
    // subsequent call must fire a genuinely fresh request rather than
    // rejoining the abandoned one.
    let secondResult: Promise<string> | undefined;
    act(() => {
      secondResult = call.ensureSession(() => true);
    });
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));
    expect(await secondResult).toBe("C");
    expect(
      await screen.findByText("Session C", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    vi.useRealTimers();
  });

  // Regression (voice acceptance round 13 background review, HIGH):
  // abandonPendingSessionCreation ("Stop waiting") must never abort a
  // creation that's still genuinely relied upon by ANOTHER concurrent
  // caller sharing the exact same in-flight request -- the intentional
  // dedup design lets identical settings + selection generation join one
  // network call. The "Stop waiting" control is visible/clickable as soon
  // as voice.saving is true, with no gating on any timeout and no
  // disabling of the Composer while voice is saving, so a user could
  // previously click it while an unrelated, healthy plain send() was
  // sharing that exact same pending creation -- aborting it out from
  // under that unrelated send, which would then fail with a raw abort
  // error instead of ever getting its session.
  it("does not abort a shared pending session creation still relied upon by another caller when only one side abandons it", async () => {
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

    const call = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
      abandonPendingSessionCreation: () => void;
    };

    // Voice starts creating a session under the current (blank) settings.
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = call.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    const [, signal] = mocks.createSession.mock.calls[0] as [
      unknown,
      AbortSignal,
    ];
    expect(signal.aborted).toBe(false);

    // A second, unrelated caller under the IDENTICAL settings/selection
    // generation (e.g. a plain text send racing the same lazy-creation
    // path) joins the SAME in-flight request rather than firing its own --
    // the existing, intentional dedup design.
    let otherResult: Promise<string> | undefined;
    act(() => {
      otherResult = call.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // Voice abandons its OWN wait ("Stop waiting"). The other caller is
    // still relying on this exact same request, so the underlying network
    // call must be left running rather than aborted out from under it.
    act(() => {
      call.abandonPendingSessionCreation();
    });
    expect(signal.aborted).toBe(false);

    // The underlying request now resolves normally -- BOTH callers' own
    // ensureSession() invocations race the same entry.promise regardless
    // of abandon (which never rejects/settles either specific call, only
    // the shared cache slot and the network controller), so both must
    // still succeed and converge on the same activated session.
    await act(async () => {
      resolveCreate(session("C"));
      await Promise.all([voiceResult, otherResult]);
    });
    expect(await voiceResult).toBe("C");
    expect(await otherResult).toBe("C");
    expect(
      await screen.findByText("Session C", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  // Regression (voice acceptance round 13, MEDIUM): the intent key must
  // incorporate tool overrides and library document selections -- not just
  // the system prompt -- so a later-resolving intent that differs *only* by
  // those fields still correctly supersedes an already-active, differently
  // (but blank-)configured one.
  it("supersedes on a later-resolving intent that differs only by tool overrides and library documents, not the system prompt", async () => {
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
    const resolvers: Array<() => void> = [];
    const created = [session("VOICE"), session("SEND")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // Voice Live's ensureSession call, under the current (blank) settings.
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // A concurrent intent enables a tool override and attaches a library
    // document -- no system prompt change at all -- and fires its own
    // creation rather than sharing voice's in-flight one.
    await user.click(screen.getByRole("tab", { name: "Agent & tools" }));
    await user.click(await screen.findByRole("checkbox", { name: /Calculator/ }));
    await user.click(screen.getByRole("tab", { name: "Context" }));
    await user.click(await screen.findByRole("button", { name: "Add brief.pdf" }));
    const sendCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let sendResult: Promise<string> | undefined;
    act(() => {
      sendResult = sendCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(2);

    // The earlier-issued (voice) creation resolves first and activates.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });
    expect(
      await screen.findByText("Session VOICE", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // The later-issued (send) intent's own creation -- differing only by
    // tool overrides and library documents -- resolves after voice already
    // activated, and correctly supersedes it.
    await act(async () => {
      resolvers[1]();
      await sendResult;
    });
    expect(await voiceResult).toBe("VOICE");
    expect(await sendResult).toBe("SEND");
    expect(mocks.createSession).toHaveBeenNthCalledWith(
      2,
      expect.objectContaining({
        toolOverrides: { added: ["calculator"], removed: [] },
        libraryDocumentIds: ["doc-1"],
      }),
      expect.any(AbortSignal),
    );
    expect(
      await screen.findByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });
});
