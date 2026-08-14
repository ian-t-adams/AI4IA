// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatApp } from "./ChatApp";
import type { Session, ToolCatalogItem } from "@/lib/types";
import {
  CHAT_ATTACHMENT_CAPABILITIES,
  CHAT_MODEL_CATALOG,
  DISABLED_MEMORY,
  emptyLibrarySummary,
  makeChatSession,
  makeInspectorSnapshot,
} from "./chatTestFixtures";

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(),
  listSessions: vi.fn(),
  listAgents: vi.fn(),
  getAttachmentCapabilities: vi.fn(),
  listMessages: vi.fn(),
  listDocuments: vi.fn(),
  listLibraryDocuments: vi.fn(),
  getLibraryDocument: vi.fn(),
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
  useInlineVoiceLive: vi.fn(),
  appendVoiceTurns: vi.fn(),
  logout: vi.fn(),
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
vi.mock("./UserMenu", () => ({
  UserMenu: ({
    onBeforeSignOut,
  }: {
    onBeforeSignOut?: () => boolean | void;
  }) => (
    <button
      type="button"
      onClick={() => {
        if (onBeforeSignOut?.() !== false) mocks.logout();
      }}
    >
      Sign out
    </button>
  ),
}));
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
      <button type="button" onClick={() => onSend("/settings")}>
        Send slash command
      </button>
      <button
        type="button"
        onClick={() =>
          void onUpload(new File(["a"], "a.pdf", { type: "application/pdf" }))
        }
      >
        Queue one upload
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
    conversationId,
    onCitation,
  }: {
    messages: { id: string; content: string }[];
    conversationId?: string | null;
    onCitation?: (target: {
      documentId: string;
      filename: string;
      ms: number;
    }) => void;
  }) => (
    <div aria-label="Conversation" data-conversation-id={conversationId ?? "draft"}>
      {messages.map((message) => (
        <div key={message.id}>{message.content}</div>
      ))}
      {onCitation && (
        <button
          type="button"
          onClick={() =>
            onCitation({
              documentId: "shared-media",
              filename: "shared.mp4",
              ms: 42_000,
            })
          }
        >
          Open shared citation
        </button>
      )}
    </div>
  ),
}));
vi.mock("./MediaPlayer", () => ({
  MediaPlayer: ({
    doc,
    seekToMs,
  }: {
    doc: { id: string };
    seekToMs?: number;
  }) => <div>{`Playing ${doc.id} at ${seekToMs}`}</div>,
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

const session = (id: string): Session => makeChatSession(id);

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
  mocks.listModels.mockResolvedValue(CHAT_MODEL_CATALOG);
  mocks.listSessions.mockResolvedValue(sessions);
  mocks.listAgents.mockResolvedValue([]);
  mocks.getAttachmentCapabilities.mockResolvedValue(
    CHAT_ATTACHMENT_CAPABILITIES,
  );
  mocks.listMessages.mockResolvedValue([]);
  mocks.appendVoiceTurns.mockResolvedValue([]);
  mocks.listDocuments.mockResolvedValue([]);
  mocks.listLibraryDocuments.mockResolvedValue([]);
  mocks.getLibraryDocument.mockRejectedValue(new Error("not configured"));
  mocks.listSharedWithMe.mockResolvedValue([]);
  mocks.createSession.mockImplementation(async (value: object) => ({
    ...session("C"),
    ...value,
  }));
  mocks.streamChat.mockReturnValue(vi.fn());
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
  mocks.getInspector.mockImplementation(async (id: string) =>
    makeInspectorSnapshot(id),
  );
  mocks.listMemories.mockResolvedValue(DISABLED_MEMORY);
  mocks.getLibrarySummary.mockResolvedValue(emptyLibrarySummary());
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
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("--font-scale");
});

describe("ChatApp landmarks", () => {
  it("owns exactly one main landmark and the skip-link target", () => {
    render(<ChatApp />);

    const mainLandmarks = screen.getAllByRole("main");
    expect(mainLandmarks).toHaveLength(1);
    expect(mainLandmarks[0]).toHaveAttribute("id", "main");
    expect(screen.getByLabelText("Conversation")).not.toHaveAttribute("id", "main");
  });

  it("passes session identity to the mounted conversation viewport", async () => {
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Conversation")).toHaveAttribute(
        "data-conversation-id",
        "A",
      ),
    );

    await user.click(screen.getByRole("button", { name: "Session B" }));
    await waitFor(() =>
      expect(screen.getByLabelText("Conversation")).toHaveAttribute(
        "data-conversation-id",
        "B",
      ),
    );
  });
});

describe("ChatApp session state reliability", () => {
  it("signs out normally without warning when no local work is in flight", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(confirmSpy).not.toHaveBeenCalled();
    expect(mocks.logout).toHaveBeenCalledTimes(1);
  });

  it("keeps sign-out available while streaming and aborts before logout", async () => {
    const abort = vi.fn();
    mocks.streamChat.mockReturnValue(abort);
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    const signOut = screen.getByRole("button", { name: "Sign out" });
    expect(signOut).toBeEnabled();
    await user.click(signOut);

    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringMatching(/sign out now.*stop the current response/i),
    );
    expect(abort).toHaveBeenCalledTimes(1);
    expect(mocks.logout).toHaveBeenCalledTimes(1);
  });

  it("prevents a pending text-session creation from streaming after sign-out", async () => {
    let resolveCreate!: (value: Session) => void;
    mocks.listSessions.mockResolvedValue([]);
    mocks.createSession.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(mocks.logout).toHaveBeenCalledTimes(1);
    await act(async () => {
      resolveCreate(session("LATE"));
      await Promise.resolve();
    });

    expect(mocks.streamChat).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Session LATE" })).not.toBeInTheDocument();
  });

  it("normally starts streaming when pending text-session creation resolves", async () => {
    let resolveCreate!: (value: Session) => void;
    mocks.listSessions.mockResolvedValue([]);
    mocks.createSession.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveCreate = resolve;
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(1));

    await act(async () => {
      resolveCreate(session("LIVE"));
      await Promise.resolve();
    });

    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
  });

  it("keeps sign-out available while voice persistence is locked and discards before logout", async () => {
    const discardPersistence = vi.fn();
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
      discardPersistence,
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ChatApp />);

    const signOut = await screen.findByRole("button", { name: "Sign out" });
    expect(signOut).toBeEnabled();
    await user.click(signOut);

    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringMatching(/sign out now.*unsaved voice transcript/i),
    );
    expect(discardPersistence).toHaveBeenCalledTimes(1);
    expect(mocks.logout).toHaveBeenCalledTimes(1);
  });

  it("requires confirmation before deleting a conversation", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(await screen.findByRole("button", { name: "Delete Session A" }));

    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringMatching(/permanently delete "Session A".*can't be undone/i),
    );
    expect(mocks.deleteSession).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Session A" })).toBeInTheDocument();
  });

  it("removes a deleted active row locally and reports a separate refresh failure", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await screen.findByText("Session A", {
      selector: ".chat-header .editable-session-title-text",
    });
    mocks.listSessions.mockRejectedValueOnce(new Error("refresh unavailable"));

    await user.click(screen.getByRole("button", { name: "Delete Session A" }));

    await waitFor(() => expect(mocks.deleteSession).toHaveBeenCalledWith("A"));
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Session A" })).not.toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /conversation deleted.*couldn't refresh.*refresh unavailable/i,
    );
  });

  it("restores the previous model and surfaces a failed persistence PATCH", async () => {
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
        {
          id: "gpt-alt",
          displayName: "GPT Alt",
          category: "chat",
          format: "openai",
          conversational: true,
          contextWindow: 64000,
          maxOutputTokens: 16000,
          options: [],
        },
      ],
    });
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    const model = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(model).toBeEnabled());
    mocks.updateSession.mockRejectedValueOnce(new Error("patch unavailable"));

    await user.selectOptions(model, "gpt-alt");

    await waitFor(() =>
      expect(mocks.updateSession).toHaveBeenCalledWith("A", {
        model: "gpt-alt",
      }),
    );
    await waitFor(() => expect(model).toHaveValue("gpt-5.2"));
    expect(screen.getByRole("alert")).toHaveTextContent(
      /couldn't save the model change.*patch unavailable/i,
    );
  });

  it("rolls rapid failed model changes back to the last persisted model", async () => {
    const model = (id: string, displayName: string) => ({
      id,
      displayName,
      category: "chat",
      format: "openai",
      conversational: true,
      contextWindow: 128000,
      maxOutputTokens: 32000,
      options: [],
    });

    mocks.listModels.mockResolvedValue({
      models: [
        model("gpt-5.2", "GPT-5.2"),
        model("gpt-b", "GPT B"),
        model("gpt-c", "GPT C"),
      ],
    });
    let rejectB!: (reason: Error) => void;
    let rejectC!: (reason: Error) => void;
    mocks.updateSession
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectB = reject;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise((_, reject) => {
            rejectC = reject;
          }),
      );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    const picker = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(picker).toBeEnabled());

    await user.selectOptions(picker, "gpt-b");
    await waitFor(() => expect(mocks.updateSession).toHaveBeenCalledTimes(1));
    await user.selectOptions(picker, "gpt-c");
    expect(mocks.updateSession).toHaveBeenCalledTimes(1);

    await act(async () => {
      rejectB(new Error("B failed"));
      await Promise.resolve();
    });
    await waitFor(() => expect(mocks.updateSession).toHaveBeenCalledTimes(2));
    await act(async () => {
      rejectC(new Error("C failed"));
      await Promise.resolve();
    });

    await waitFor(() => expect(picker).toHaveValue("gpt-5.2"));
    expect(screen.getByRole("alert")).toHaveTextContent(/C failed/);
  });

  it("does not let a hung model PATCH in one session block another session", async () => {
    const model = (id: string, displayName: string) => ({
      id,
      displayName,
      category: "chat",
      format: "openai",
      conversational: true,
      contextWindow: 128000,
      maxOutputTokens: 32000,
      options: [],
    });
    mocks.listModels.mockResolvedValue({
      models: [model("gpt-5.2", "GPT-5.2"), model("gpt-b", "GPT B")],
    });
    mocks.updateSession
      .mockImplementationOnce(() => new Promise(() => {}))
      .mockResolvedValueOnce({ ...session("B"), model: "gpt-b" });
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    let picker = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(picker, "gpt-b");
    await waitFor(() => expect(mocks.updateSession).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Session B" }));
    picker = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(picker, "gpt-b");

    await waitFor(() => expect(mocks.updateSession).toHaveBeenCalledTimes(2));
    expect(mocks.updateSession).toHaveBeenLastCalledWith("B", { model: "gpt-b" });
    await waitFor(() => expect(picker).toHaveValue("gpt-b"));
  });

  it("shows a persisted model when its PATCH completes after navigating away and back", async () => {
    const model = (id: string, displayName: string) => ({
      id,
      displayName,
      category: "chat",
      format: "openai",
      conversational: true,
      contextWindow: 128000,
      maxOutputTokens: 32000,
      options: [],
    });
    mocks.listModels.mockResolvedValue({
      models: [
        model("gpt-5.2", "GPT-5.2"),
        model("gpt-b", "GPT B"),
      ],
    });
    let resolvePatch!: (value: Session) => void;
    let resolveStaleHydration!: (value: Session[]) => void;
    mocks.updateSession.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePatch = resolve;
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    let picker = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(picker).toBeEnabled());
    await user.selectOptions(picker, "gpt-b");
    await waitFor(() => expect(mocks.updateSession).toHaveBeenCalledTimes(1));

    await user.click(screen.getByRole("button", { name: "Session B" }));
    await screen.findByText("Session B", {
      selector: ".chat-header .editable-session-title-text",
    });
    mocks.listSessions.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveStaleHydration = resolve;
        }),
    );
    await user.click(screen.getByRole("button", { name: "Session A" }));
    await screen.findByText("Session A", {
      selector: ".chat-header .editable-session-title-text",
    });

    await act(async () => {
      resolvePatch({ ...session("A"), model: "gpt-b" });
      await Promise.resolve();
    });
    await act(async () => {
      resolveStaleHydration([session("A"), session("B")]);
      await Promise.resolve();
    });

    picker = await screen.findByRole("combobox", { name: "Model" });
    await waitFor(() => expect(picker).toHaveValue("gpt-b"));
  });

});
describe("ChatApp citations", () => {
  it("opens shared media directly by the attested document id", async () => {
    mocks.getLibraryDocument.mockResolvedValue({
      ...libraryDocument("shared-media", "shared.mp4"),
      contentType: "video/mp4",
      modality: "video",
      analyzerId: null,
      summary: "",
      visibility: "shared",
    });
    const user = userEvent.setup();

    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await user.click(
      await screen.findByRole("button", { name: "Open shared citation" }),
    );

    expect(mocks.getLibraryDocument).toHaveBeenCalledWith("shared-media");
    expect(await screen.findByText("Playing shared-media at 42000")).toBeInTheDocument();
  });
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

  // Regression (acceptance review of 7dabeda, HIGH): entry.mismatchClaimed
  // used to be claimed unconditionally by whichever caller's continuation
  // happened to resume first once a SHARED in-flight creation settled --
  // even an already-ineligible one (stillCurrentSelection/stillWanted
  // false). Unlike the test above (which starts from a blank view, so
  // noSessionActiveYet alone grants activation regardless of
  // mismatchClaimed), this scenario needs a DIFFERENT, already-active
  // session in place first so only the settingsMismatch branch -- the one
  // mismatchClaimed actually gates -- can satisfy activation. A discarded
  // caller's continuation running first must never be able to burn the
  // entry's one-shot mismatch claim ahead of a second, genuinely eligible
  // caller sharing that identical entry.
  it("lets a still-wanted caller supersede an already-active, differently-configured session even though a discarded caller sharing its entry's mismatch check ran first", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("INITIAL"), session("LATER")];
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

    // An initial creation fires under the current (blank) settings. It must
    // stay unresolved for now: ensureSession() short-circuits to whatever
    // is already active (sessionIdRef.current) before it ever looks at
    // settings, so the entry the next two callers join below has to be
    // created while NOTHING is active yet -- exactly as it would be if this
    // and the shared entry were two genuinely concurrent intents.
    const initialCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let initialResult: Promise<string> | undefined;
    act(() => {
      initialResult = initialCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // Before it resolves, settings change (a genuinely different intent
    // key from the initial call's), and two callers share THAT entry --
    // back-to-back, no await between them, so both see the same still-empty
    // creatingRef slot for the new key and join the SAME in-flight
    // creation. The first is already discarded by the time it asks (e.g.
    // Voice Live's "Stop waiting"); the second still wants whatever session
    // that shared creation produces (e.g. a plain send/upload).
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Later prompt");
    const laterCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let discardedResult: Promise<string> | undefined;
    let stillWantedResult: Promise<string> | undefined;
    act(() => {
      discardedResult = laterCall.ensureSession(() => false);
      stillWantedResult = laterCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(2);

    // The initial creation resolves first -- nothing else is active yet, so
    // it goes through the noSessionActiveYet path, untouched by
    // mismatchClaimed, and activates.
    await act(async () => {
      resolvers[0]();
      await initialResult;
    });
    expect(
      await screen.findByText("Session INITIAL", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // The shared, differently-configured entry resolves next, with
    // "INITIAL" already active. Both waiters' continuations now compete for
    // the entry's one-shot mismatch claim -- the discarded one first, since
    // it raced the shared promise first.
    await act(async () => {
      resolvers[1]();
      await Promise.all([discardedResult, stillWantedResult]);
    });

    // The discarded caller's continuation running first must not have spent
    // the entry's one-shot mismatch claim: the still-wanted second caller
    // must still be able to supersede the already-active "INITIAL" session
    // with the newly created, correctly-configured "LATER" one.
    expect(await stillWantedResult).toBe("LATER");
    expect(
      await screen.findByText("Session LATER", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(mocks.createSession).toHaveBeenCalledTimes(2);
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

    // Switch the real inspector to its Instructions section and set a draft
    // system prompt -- while still in "new chat" (no session yet), this
    // writes straight through to ChatApp's systemPrompt state.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
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

    await user.click(screen.getByRole("button", { name: "Instructions" }));
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

    // The user then types a different prompt before sending. Instructions is
    // still the inspector's open section, so there is nothing to re-navigate.
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
  // ensureSession() calls -- standing in for Voice Live (issued first) and
  // a typed send after editing settings (issued second, hence the
  // genuinely LATER intent by sequence) -- and resolve their underlying
  // creations in each possible order.
  //
  // Regression (voice acceptance round 13, MEDIUM; corrected in round 15,
  // HIGH, after discovering resolution order alone was an unreliable proxy
  // for "later intent" -- see the sequence comment on creatingRef): only
  // the genuinely LATER-ISSUED intent may ever supersede an already-active
  // mismatched one, regardless of which one's underlying network call
  // happens to resolve first. When the later-issued (send) intent resolves
  // after the earlier one (voice) already activated, it correctly
  // supersedes -- the conversation on screen reflects the most recent
  // settings. A Promise's own resolved value is fixed the instant it
  // settles, so the earlier-resolving caller's own already-returned id
  // never changes after the fact -- only which caller "already activated"
  // matters, and that is decided by *issuance* order, never resolution
  // order (see the sibling test below for the mirror image).
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
    await user.click(screen.getByRole("button", { name: "Instructions" }));
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

  // Regression (voice acceptance round 15, HIGH): the mirror image of the
  // sibling test above, and the exact defect this round fixes. Voice is
  // issued FIRST (lower sequence) but its underlying network call happens
  // to settle SECOND, after the genuinely later-issued (send) intent has
  // already activated. Resolution order alone must never let the older,
  // staler intent win -- see "older voice intent resolving after newer
  // send can replace active session while send continues hidden" and the
  // sequence comment on creatingRef/activeActivationSequenceRef.
  it("does not let the earlier-issued (voice-shaped) creation supersede the later-issued (send-shaped) one that already activated, even though voice resolves after it", async () => {
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

    // Voice Live's ensureSession call fires FIRST, under the current
    // (blank) settings.
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // A concurrent, differently-configured intent (e.g. a typed send after
    // editing the draft system prompt) fires its OWN, genuinely LATER (by
    // sequence) creation.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    const sendCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let sendResult: Promise<string> | undefined;
    act(() => {
      sendResult = sendCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(2);

    // The later-issued (send) intent resolves FIRST this time. Nothing is
    // active yet, so it activates normally.
    await act(async () => {
      resolvers[1]();
      await sendResult;
    });
    expect(
      await screen.findByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // Voice's own creation resolves SECOND, under its own (now stale)
    // settings. Even though it resolves AFTER send already activated --
    // the exact shape that used to let resolution order win -- voice is
    // the EARLIER-issued intent (lower sequence) and must not supersede.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });
    expect(await sendResult).toBe("SEND");
    // Voice's own call falls back to the session that's actually current,
    // never its own now-orphaned "VOICE" creation.
    expect(await voiceResult).toBe("SEND");
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
  });

  // Regression (voice acceptance round 15, HIGH): proves the narrower,
  // previously-unclosed race one hop earlier than the "already-streaming"
  // test above closes too. Resolving both underlying creations back-to-back
  // (rather than waiting for streamChat before resolving voice, as that
  // test deliberately does) lets the two calls' own multi-hop promise
  // chains genuinely interleave hop-by-hop: send()'s own ensureSession()
  // call activates internally (sessionIdRef.current is set) one microtask
  // before send()'s CALLER code -- the `streamingSessionIdRef.current =
  // sessionId;` line, one further `await` hop later -- actually runs, so
  // currentSessionInUse alone still reads false at the exact instant
  // voice's own activation-decision code executes. Only the sequence-based
  // guard (activeActivationSequenceRef) closes this exact window.
  it("keeps active/send coherent when a genuinely later send() and an earlier-issued voice intent resolve in the same interleaved microtask batch", async () => {
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

    // Voice starts creating a session FIRST, under the current (blank)
    // settings.
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // The user edits settings and sends a REAL message -- a genuinely
    // LATER intent (by sequence) that fires its own concurrent creation.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));

    // Resolve BOTH underlying creations back-to-back -- no artificial
    // synchronization point in between -- so the two calls' own multi-hop
    // promise chains genuinely interleave.
    await act(async () => {
      resolvers[1]();
      resolvers[0]();
      await voiceResult;
      await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    });

    // send() must have streamed into its own, genuinely-later "SEND"
    // session -- never voice's stale "VOICE" one.
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SEND" }),
      expect.anything(),
    );
    expect(mocks.streamChat).toHaveBeenCalledTimes(1);
    // Voice's own call must fall back to the session that's actually
    // current and being streamed into, never its own now-orphaned "VOICE"
    // creation.
    expect(await voiceResult).toBe("SEND");
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
    await user.click(screen.getByRole("button", { name: "Instructions" }));
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
    await user.click(screen.getByRole("button", { name: "Instructions" }));
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

  // Regression (voice acceptance round 17, HIGH): currentSessionInUse only
  // ever protects a session for as long as something is LITERALLY still
  // streaming/uploading into it -- it goes false again the instant that
  // finishes, even though the session's real, already-committed content
  // stays fully displayed. A completed voice persist never touches
  // streamingSessionIdRef/uploadTargetsRef at all, so before this round's
  // fix a later-resolving, differently-keyed creation could still supersede
  // the session hours after voice's real content had already been saved and
  // shown, flipping the header/sidebar to a new, unrelated session while
  // the just-persisted transcript stayed on screen underneath it.
  // consumedSessionIdRef/activeSessionConsumed close that gap by staying
  // sticky once real content has ever been committed, independent of
  // whether anything is actively in flight at the moment a competitor
  // resolves.
  it("keeps a session active after voice has fully persisted real content to it, against a later-resolving, differently-keyed send() creation", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("A"), session("B")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    mocks.appendVoiceTurns.mockResolvedValueOnce([
      {
        id: "m1",
        sessionId: "A",
        userId: "u1",
        role: "user",
        content: "Hello from voice",
        status: "complete",
        model: null,
        agent: null,
        createdAt: "",
        source: "voice",
      },
    ]);
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // Voice starts creating a session under the current (blank) settings.
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
      persistConversation: (
        sessionId: string,
        conversationId: string,
        turns: { role: "user" | "assistant"; text: string }[],
        isStillValid: () => boolean,
      ) => Promise<void>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // The user edits the draft system prompt and sends a real message --
    // send()'s own ensureSession() call, under genuinely different
    // settings, fires its own concurrent creation rather than sharing
    // voice's.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));

    // Voice's own creation resolves first -- nothing is active yet, so
    // this is a trivial noSessionActiveYet activation.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });
    expect(await voiceResult).toBe("A");
    expect(
      await screen.findByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // Voice's turn is now FULLY persisted -- real, saved backend content --
    // well before send's differently-keyed creation resolves. Nothing is
    // currently streaming or uploading at this instant, so only the new,
    // sticky protection can save this case.
    await act(async () => {
      await voiceCall.persistConversation(
        "A",
        "conversation-1",
        [{ role: "user", text: "Hello from voice" }],
        () => true,
      );
    });
    expect(await screen.findByText("Hello from voice")).toBeInTheDocument();

    // send()'s differently-keyed creation resolves NOW, long after voice's
    // persist has already fully landed.
    await act(async () => {
      resolvers[1]();
    });

    // send() must have proceeded into "A" -- the session that's genuinely
    // already established and holds voice's saved turn -- never into an
    // orphaned "B" that nothing shows.
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "A" }),
      expect.anything(),
    );
    expect(
      screen.getByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Session B", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).not.toBeInTheDocument();
    // Voice's real, previously-saved content must still be visible.
    expect(screen.getByText("Hello from voice")).toBeInTheDocument();
  });

  // Regression (voice acceptance round 17, HIGH): finalize() clears
  // streamingSessionIdRef.current -- the ONLY thing currentSessionInUse
  // depended on -- essentially at its very start, well BEFORE awaiting
  // api.listMessages() and committing the final reconciled transcript.
  // Before this round's fix, a differently-keyed creation resolving inside
  // that exact async gap would see currentSessionInUse already false (and
  // nothing else protecting the session), win the mismatch, and silently
  // flip activeId to the new, unrelated session while send()'s own
  // finalize() was still mid-flight -- landing its reconciled content under
  // a UI nominally showing something else. consumedSessionIdRef is set
  // synchronously alongside streamingSessionIdRef, before any await, so it
  // keeps protecting the session through this entire window.
  it("keeps a real send()'s session active against a later, differently-keyed creation resolving inside finalize()'s async gap", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("SEND"), session("VOICE")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    let resolveListMessages!: (
      value: Awaited<ReturnType<typeof mocks.listMessages>>,
    ) => void;
    mocks.listMessages.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveListMessages = resolve;
        }),
    );
    // finalize()'s trailing refreshSessions() re-fetches the session list
    // for its title lookup; include the newly-created sessions so the
    // header's title doesn't fall back to "Untitled" once that lands.
    mocks.listSessions.mockResolvedValue(created);
    const user = userEvent.setup();
    render(<ChatApp />);
    expect(
      await screen.findByText("New conversation", { selector: "strong" }),
    ).toBeInTheDocument();

    // send() starts creating a session FIRST, under the current (blank)
    // settings -- this is entry #1, the lower activation sequence number.
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(1));

    // Only AFTER send()'s own creation is already in flight does the user
    // edit settings and voice starts its own, later, differently-keyed
    // creation -- entry #2, a HIGHER activation sequence number than
    // send's. This ordering matters: the pre-existing round-15 guard
    // (`entry.sequence > activeActivationSequenceRef.current`) alone
    // already blocks any LOWER-sequence (earlier-created) challenger from
    // ever superseding a HIGHER-sequence active session, regardless of
    // consumedSessionIdRef. To actually exercise this round's new sticky
    // protection in isolation, the challenger must be the one created
    // later (and thus hold the higher sequence) while still losing the
    // race to be the one that actually activates first.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Voice prompt");
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));

    // send()'s own creation (entry #1, lower sequence) resolves first,
    // activates, and begins streaming.
    await act(async () => {
      resolvers[0]();
    });
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SEND" }),
      expect.anything(),
    );

    // The assistant's response completes: onDone fires finalize(), which
    // synchronously clears streamingSessionIdRef.current before awaiting
    // listMessages (held open by resolveListMessages above).
    const handlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onMetadata: (value: {
        userMessageId: string | null;
        assistantMessageId: string;
      }) => void;
      onDelta: (text: string) => void;
      onDone: () => void;
    };
    act(() => {
      handlers.onMetadata({
        userMessageId: "user-1",
        assistantMessageId: "assistant-1",
      });
      handlers.onDelta("Final assistant reply");
      handlers.onDone();
    });

    // Voice's differently-keyed creation (entry #2, HIGHER sequence than
    // send's already-active entry) resolves NOW, inside finalize()'s async
    // gap -- after streamingSessionIdRef.current was cleared but before
    // listMessages resolves and the final content commits. Without this
    // round's fix, nothing else would stop it: currentSessionInUse is
    // already false and the sequence check passes (2 > 1).
    await act(async () => {
      resolvers[1]();
      await voiceResult;
    });

    // Voice's own call must fall back to "SEND" -- the session that's
    // genuinely already established and mid-finalize -- never its own
    // orphaned "VOICE" session.
    expect(await voiceResult).toBe("SEND");
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

    // Let finalize()'s listMessages resolve and its reconciled content
    // land -- it must land under "SEND", the session actually still shown.
    await act(async () => {
      resolveListMessages([
        {
          id: "assistant-1",
          sessionId: "SEND",
          userId: "assistant",
          role: "assistant",
          content: "Final assistant reply",
          status: "complete",
          model: "gpt-5.2",
          agent: null,
          createdAt: "",
        },
      ]);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });
    expect(await screen.findByText("Final assistant reply")).toBeInTheDocument();
    expect(
      screen.getByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  // Regression (voice acceptance round 18, HIGH): ensureSession()'s own
  // activation block used to set only sessionIdRef.current synchronously;
  // consumedSessionIdRef.current was marked externally by send()/
  // runUpload() AFTER their own `await ensureSession()` call returned -- an
  // additional microtask hop later than ensureSession()'s own internal
  // activation (an `await` always yields at least once, even for an
  // already-resolved promise). A differently-keyed, higher-sequence
  // competitor's own ensureSession() continuation could land in exactly
  // that gap: consumedSessionIdRef.current still read as unset, so it
  // would legitimately (per the existing sequence-based supersession rule)
  // win and switch sessionIdRef.current to its own session -- even though
  // the original caller had already captured its own id and was about to
  // stream into it. The UI would then show the new session while streaming
  // silently continued, unseen, into the old one.
  it("marks a send()'s session consumed atomically inside ensureSession(), before a later, differently-keyed voice creation resolving back-to-back can supersede it", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("SEND"), session("VOICE")];
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

    // send() starts creating a session FIRST, under the current (blank)
    // settings -- this is entry #1, the lower activation sequence number.
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(1));

    // Only AFTER send()'s own creation is already in flight does the user
    // edit settings and voice starts its own, later, differently-keyed
    // creation -- entry #2, a HIGHER activation sequence number than
    // send's.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Voice prompt");
    const voiceCall = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = voiceCall.ensureSession(() => true);
    });
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));

    // Resolve BOTH underlying creations back-to-back -- no artificial
    // synchronization point in between -- so send()'s own ensureSession()
    // activation (entry #1) and voice's (entry #2) genuinely interleave hop
    // by hop, with no external code running between them. send's own
    // internal continuation (which, with this round's fix, now marks
    // consumedSessionIdRef.current in the SAME synchronous block as
    // activation) always completes one hop ahead of voice's corresponding
    // hop, since it was resolved first.
    await act(async () => {
      resolvers[0]();
      resolvers[1]();
      await voiceResult;
      await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    });

    // send() must have streamed into its own "SEND" session.
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SEND" }),
      expect.anything(),
    );
    // Voice's own call must fall back to "SEND" -- never its own "VOICE".
    expect(await voiceResult).toBe("SEND");
    // Critically, the UI itself must still show "SEND" as active -- not
    // silently flip to "VOICE" while streaming continues, unseen, into
    // "SEND".
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
  });

  // Regression (voice acceptance round 18, HIGH): finalize()'s very first
  // lines clear streamingRef.current and call setStreaming(false)
  // synchronously -- unlocking navigation and new sends -- well before its
  // own `await api.listMessages()` resolves. Before this round's fix, once
  // the user had since navigated away, this now-stale finalize's eventual,
  // unconditional setMessages(reconcileMessages(...)) would still land the
  // OLD session's reconciled content on top of whatever the newly-selected
  // session's own, freshly-loaded messages the navigation had just shown --
  // a session mix.
  it("does not let a stale finalize()'s delayed listMessages() land on a different session the user has since navigated to", async () => {
    let resolveListMessagesA!: (
      value: Awaited<ReturnType<typeof mocks.listMessages>>,
    ) => void;
    mocks.listMessages.mockImplementation((sessionId: string) => {
      if (sessionId === "A") {
        return new Promise((resolve) => {
          resolveListMessagesA = resolve;
        });
      }
      return Promise.resolve([]);
    });
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(await screen.findByRole("button", { name: "Session A" }));
    expect(
      await screen.findByText("Session A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "A" }),
      expect.anything(),
    );

    // The assistant's response completes: onDone fires finalize(), which
    // synchronously clears streamingRef.current -- unlocking navigation --
    // before its own await api.listMessages("A") resolves (held open by
    // resolveListMessagesA above).
    const handlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onDone: () => void;
    };
    act(() => {
      handlers.onDone();
    });

    // While that stale finalize is still pending, the user navigates to
    // the other real, pre-existing "Session B" -- streamingRef.current is
    // already false, so nothing blocks it.
    await user.click(screen.getByRole("button", { name: "Session B" }));
    expect(
      await screen.findByText("Session B", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // Session A's stale finalize() finally resolves its own listMessages
    // call.
    await act(async () => {
      resolveListMessagesA([
        {
          id: "assistant-1",
          sessionId: "A",
          userId: "assistant",
          role: "assistant",
          content: "Final assistant reply for A",
          status: "complete",
          model: "gpt-5.2",
          agent: null,
          createdAt: "",
        },
      ]);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The UI must still show Session B, with none of session A's stale,
    // late-arriving content -- no session mix.
    expect(
      screen.getByText("Session B", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Final assistant reply for A"),
    ).not.toBeInTheDocument();
  });

  // Regression (voice acceptance round 18, HIGH -- companion to the
  // navigation case above): the same stale, unlocked finalize() must also
  // never clobber a genuinely NEWER send()'s still-live streaming text when
  // no navigation happens at all -- just a second message sent into the
  // same session while the first's finalize is still awaiting
  // listMessages().
  it("does not let a stale finalize()'s delayed listMessages() clear a newer send()'s still-live streaming text", async () => {
    let resolveListMessages!: (
      value: Awaited<ReturnType<typeof mocks.listMessages>>,
    ) => void;
    mocks.listMessages.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolveListMessages = resolve;
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);

    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await screen.findByText("Session A", {
      selector: ".chat-header .editable-session-title-text",
    });

    // The first send streams and completes.
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    const firstHandlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onDone: () => void;
    };
    act(() => {
      firstHandlers.onDone();
    });
    // finalize() has synchronously cleared streamingRef.current and is now
    // awaiting its own (deliberately deferred) listMessages("A") call.

    // A second, genuinely newer send starts and begins streaming real,
    // visible live text while the first send's finalize is still pending.
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(2));
    const secondHandlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onDelta: (t: string) => void;
    };
    act(() => {
      secondHandlers.onDelta("Newer live answer in progress");
    });
    expect(
      await screen.findByText("Newer live answer in progress"),
    ).toBeInTheDocument();

    // The first send's stale finalize() now finally resolves.
    await act(async () => {
      resolveListMessages([]);
      await new Promise((resolve) => setTimeout(resolve, 0));
    });

    // The second, genuinely newer send's still-live streaming text must
    // survive untouched -- the stale finalize must not have cleared it.
    expect(
      screen.getByText("Newer live answer in progress"),
    ).toBeInTheDocument();
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

  // Regression (voice acceptance round 15, HIGH): abandonPendingSessionCreation
  // ("Stop waiting") used to recompute an intent key from CURRENT settings
  // at the moment it was clicked. If those settings changed after voice's
  // own call joined its entry -- without any navigation, so voice's call is
  // never told to give up -- the recomputed key could coincidentally match
  // a DIFFERENT, entirely unrelated caller's entry (e.g. a typed send fired
  // under the new settings) instead of voice's own now-stale-keyed one,
  // wrongly aborting that unrelated caller's healthy creation. Voice's own
  // waiter must be released by identity (voiceWaiterRef), never by
  // re-deriving a key from settings that may have drifted since voice's own
  // call actually joined.
  it("does not abort an unrelated, differently-keyed session creation when settings changed before Stop waiting was clicked", async () => {
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

    // Voice starts creating a session under the current (blank) settings.
    const call = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
      abandonPendingSessionCreation: () => void;
    };
    let voiceResult: Promise<string> | undefined;
    act(() => {
      voiceResult = call.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    const [, voiceSignal] = mocks.createSession.mock.calls[0] as [
      unknown,
      AbortSignal,
    ];
    expect(voiceSignal.aborted).toBe(false);

    // WITHOUT navigating away, the user edits the draft system prompt and
    // sends a real message -- a genuinely different intent key, so it
    // fires its own, separate creation rather than sharing voice's.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Send prompt");
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));
    const [, sendSignal] = mocks.createSession.mock.calls[1] as [
      unknown,
      AbortSignal,
    ];
    expect(sendSignal.aborted).toBe(false);

    // Voice clicks "Stop waiting" only now, after settings have already
    // changed. A key recomputed from the CURRENT (edited) settings would
    // match send's entry, not voice's own (blank-settings) one -- the
    // exact stale-key defect this round fixes.
    act(() => {
      call.abandonPendingSessionCreation();
    });

    // Voice's own, now-abandoned creation is aborted...
    expect(voiceSignal.aborted).toBe(true);
    // ...but send's entirely unrelated, still-healthy creation must be left
    // completely alone.
    expect(sendSignal.aborted).toBe(false);

    // Send's creation resolves normally and activates -- proving it was
    // never disturbed by voice's abandon.
    await act(async () => {
      resolvers[1]();
      await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    });
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SEND" }),
      expect.anything(),
    );
    expect(
      screen.getByText("Session SEND", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    // Clean up voice's own abandoned (but never network-settled) promise:
    // it safely falls back to whatever session is actually current rather
    // than its own orphaned "VOICE" one, and resolving it here (rather than
    // leaving it pending) avoids a real SESSION_CREATION_TIMEOUT_MS timer
    // dangling past the end of this test.
    await act(async () => {
      resolvers[0]();
      await voiceResult;
    });
    expect(await voiceResult).toBe("SEND");
  });

  // Regression (voice acceptance round 13, MEDIUM): the intent key must
  // incorporate tool overrides and library document selections -- not just
  // the system prompt -- so a later-resolving intent that differs *only* by
  // those fields still correctly supersedes an already-active, differently
  // (but blank-)configured one.
  it("supersedes on a later-resolving intent that differs only by tool overrides and library documents, not the system prompt", async () => {
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
    await user.click(screen.getByRole("button", { name: "Agent & tools" }));
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

  it("atomically owns a voice-first shared creation through send, a late unrelated creation, and upload", async () => {
    const created = [session("SHARED"), session("LATE")];
    const resolvers: Array<() => void> = [];
    mocks.listSessions.mockResolvedValue([]);
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    mocks.uploadLibraryDocument.mockResolvedValue(
      libraryDocument("doc-shared", "a.pdf"),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    await screen.findByText("New conversation", { selector: "strong" });

    const firstVoiceOptions = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let firstVoice!: Promise<string>;
    act(() => {
      firstVoice = firstVoiceOptions.ensureSession(() => true);
    });
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Later voice");
    const lateVoiceOptions = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let lateVoice!: Promise<string>;
    act(() => {
      lateVoice = lateVoiceOptions.ensureSession(() => true);
    });
    expect(mocks.createSession).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolvers[0]();
      await firstVoice;
      await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    });
    expect(await firstVoice).toBe("SHARED");
    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SHARED" }),
      expect.anything(),
    );

    await act(async () => {
      resolvers[1]();
      await lateVoice;
    });
    expect(await lateVoice).toBe("SHARED");
    expect(
      screen.getByText("Session SHARED", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();

    mocks.listSessions.mockResolvedValue(created);
    const handlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onMetadata: (value: {
        userMessageId: string | null;
        assistantMessageId: string;
      }) => void;
      onDelta: (text: string) => void;
      onDone: () => void;
    };
    act(() => {
      handlers.onMetadata({
        userMessageId: "shared-user",
        assistantMessageId: "shared-assistant",
      });
      handlers.onDelta("Shared reply");
      handlers.onDone();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );

    await user.click(screen.getByRole("button", { name: "Queue one upload" }));
    await waitFor(() =>
      expect(mocks.associateLibraryDocument).toHaveBeenCalledWith(
        "SHARED",
        "doc-shared",
      ),
    );
    expect(mocks.createSession).toHaveBeenCalledTimes(2);
    expect(
      screen.getByText("Session SHARED", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  it("claims an early existing session for upload before a late creation's next microtask", async () => {
    const created = [session("ACTIVE"), session("LATE")];
    const resolvers: Array<() => void> = [];
    mocks.listSessions.mockResolvedValue([]);
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    mocks.uploadLibraryDocument.mockResolvedValue(
      libraryDocument("doc-active", "a.pdf"),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    await screen.findByText("New conversation", { selector: "strong" });

    const activeVoiceOptions = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let activeVoice!: Promise<string>;
    act(() => {
      activeVoice = activeVoiceOptions.ensureSession(() => true);
    });
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Late settings");
    const lateVoiceOptions = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let lateVoice!: Promise<string>;
    act(() => {
      lateVoice = lateVoiceOptions.ensureSession(() => true);
    });

    await act(async () => {
      resolvers[0]();
      await activeVoice;
    });
    expect(await activeVoice).toBe("ACTIVE");

    await user.click(screen.getByRole("button", { name: "Queue one upload" }));
    await act(async () => {
      resolvers[1]();
      await lateVoice;
    });

    expect(await lateVoice).toBe("ACTIVE");
    await waitFor(() =>
      expect(mocks.associateLibraryDocument).toHaveBeenCalledWith(
        "ACTIVE",
        "doc-active",
      ),
    );
    expect(
      screen.getByText("Session ACTIVE", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
  });

  // Regression (voice acceptance round 19, HIGH): round 18 only marked
  // consumedSessionIdRef.current from the activation branch. A caller that
  // instead falls through to the shared-entry/settings-mismatch FALLBACK --
  // e.g. a second waiter joining an already-activated, same-key entry --
  // returned its id bare, unmarked, from that path. send()/runUpload() still
  // mark it themselves right after their own `await ensureSession()` call
  // returns, but that is at least one more microtask hop after
  // ensureSession()'s own activating waiter completes its activation in the
  // SAME back-to-back flush -- exactly the gap a differently-keyed, later
  // creation resolving in that same flush can land in and illegitimately
  // supersede, per the same class of race round 18 closed for activation.
  it("marks a shared-entry fallback consumer's session consumed atomically inside ensureSession(), before a later, differently-keyed creation resolving back-to-back can supersede it", async () => {
    const resolvers: Array<() => void> = [];
    const created = [session("SHARED"), session("LATE")];
    mocks.createSession.mockImplementation(
      () =>
        new Promise((resolve) => {
          const value = created[resolvers.length];
          resolvers.push(() => resolve(value));
        }),
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    await screen.findByText("New conversation", { selector: "strong" });

    // Voice's own call registers FIRST against entry #1 (blank settings).
    const voiceOptions = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let voiceResult!: Promise<string>;
    act(() => {
      voiceResult = voiceOptions.ensureSession(() => true);
    });
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(1));

    // send() joins the SAME entry (identical settings/generation -- no new
    // creation) -- it registers SECOND against entry #1.
    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    expect(mocks.createSession).toHaveBeenCalledTimes(1);

    // Only now does settings change and a differently-keyed, LATER voice
    // call start its own, separate entry #2.
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    await user.type(screen.getByLabelText("System prompt"), "Later prompt");
    const lateOptions = mocks.useInlineVoiceLive.mock.calls.at(-1)![0] as {
      ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
    };
    let lateResult!: Promise<string>;
    act(() => {
      lateResult = lateOptions.ensureSession(() => true);
    });
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(2));

    // Resolve BOTH underlying creations back-to-back -- no separating await
    // -- so entry #1's two waiters (voice, then send) and entry #2's own
    // waiter all settle within the same flush. Voice (registered first)
    // activates; send (registered second, same key) falls through to the
    // FALLBACK path -- this round's fix. Entry #2's later, differently-keyed
    // continuation must not supersede once it does.
    await act(async () => {
      resolvers[0]();
      resolvers[1]();
      await voiceResult;
      await lateResult;
      await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledTimes(1));
    });

    expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "SHARED" }),
      expect.anything(),
    );
    expect(await voiceResult).toBe("SHARED");
    // The later, differently-keyed intent must fall back to the already-
    // active "SHARED" session -- never activate its own unseen "LATE" one.
    expect(await lateResult).toBe("SHARED");
    expect(
      screen.getByText("Session SHARED", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Session LATE", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).not.toBeInTheDocument();
  });

  it("fences delayed slash finalize after navigation, edits, rename, and a newer send", async () => {
    const model = (id: string, displayName: string) => ({
      id,
      displayName,
      category: "chat",
      format: "openai",
      conversational: true,
      contextWindow: 128000,
      maxOutputTokens: 32000,
      options: [],
    });
    mocks.listModels.mockResolvedValue({
      models: [model("gpt-5.2", "GPT-5.2"), model("gpt-new", "GPT New")],
    });
    const current = new Map([
      ["A", session("A")],
      ["B", session("B")],
    ]);
    mocks.listSessions.mockImplementation(async () => [...current.values()]);
    mocks.updateSession.mockImplementation(
      async (id: string, value: Record<string, unknown>) => {
        const updated = { ...current.get(id)!, ...value };
        current.set(id, updated);
        return updated;
      },
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await screen.findByText("Session A", {
      selector: ".chat-header .editable-session-title-text",
    });

    let resolveOldList!: (value: ReturnType<typeof session>[]) => void;
    const oldList = new Promise<ReturnType<typeof session>[]>((resolve) => {
      resolveOldList = resolve;
    });
    mocks.listMessages.mockResolvedValueOnce([
      {
        id: "slash-user",
        sessionId: "A",
        userId: "u1",
        role: "user",
        content: "/settings",
        status: "complete",
        model: "gpt-5.2",
        agent: null,
        createdAt: "",
      },
      {
        id: "slash-assistant",
        sessionId: "A",
        userId: "assistant",
        role: "assistant",
        content: "Old command reply",
        status: "complete",
        model: "gpt-5.2",
        agent: null,
        createdAt: "",
      },
    ]);
    mocks.listSessions.mockImplementationOnce(() => oldList);

    await user.click(screen.getByRole("button", { name: "Send slash command" }));
    const oldHandlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onMetadata: (value: {
        userMessageId: string | null;
        assistantMessageId: string;
      }) => void;
      onDelta: (text: string) => void;
      onDone: () => void;
    };
    act(() => {
      oldHandlers.onMetadata({
        userMessageId: "slash-user",
        assistantMessageId: "slash-assistant",
      });
      oldHandlers.onDelta("Old command reply");
      oldHandlers.onDone();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );

    await user.click(screen.getByRole("button", { name: "Session B" }));
    await screen.findByText("Session B", {
      selector: ".chat-header .editable-session-title-text",
    });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Model" }),
      "gpt-new",
    );
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    const prompt = await screen.findByRole("textbox", { name: "System prompt" });
    await user.clear(prompt);
    await user.type(prompt, "Fresh prompt");
    await user.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(current.get("B")).toMatchObject({
        model: "gpt-new",
        systemPrompt: "Fresh prompt",
      }),
    );

    const header = document.querySelector(".chat-header");
    expect(header).not.toBeNull();
    await user.click(within(header as HTMLElement).getByRole("button", { name: "Rename" }));
    const titleInput = within(header as HTMLElement).getByRole("textbox", {
      name: "Conversation title",
    });
    await user.clear(titleInput);
    await user.type(titleInput, "Renamed B");
    await user.click(within(header as HTMLElement).getByRole("button", { name: "Save" }));
    await screen.findByText("Renamed B", {
      selector: ".chat-header .editable-session-title-text",
    });

    await user.click(screen.getByRole("button", { name: "Send draft message" }));
    const newHandlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onDelta: (text: string) => void;
    };
    act(() => newHandlers.onDelta("Newer B answer"));
    expect(await screen.findByText("Newer B answer")).toBeInTheDocument();

    await act(async () => {
      resolveOldList([
        { ...session("A"), title: "Stale A", model: "gpt-5.2" },
        {
          ...session("B"),
          title: "Stale B",
          model: "gpt-5.2",
          systemPrompt: "Stale prompt",
        },
      ]);
      await Promise.resolve();
    });

    expect(screen.getByText("Newer B answer")).toBeInTheDocument();
    expect(
      screen.getByText("Renamed B", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Stale B")).toBeNull();
    expect(screen.getByRole("textbox", { name: "System prompt" })).toHaveValue(
      "Fresh prompt",
    );
    await user.click(screen.getByRole("button", { name: "Model" }));
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-new");
  });

  // Same-session edits must also beat a slash command's late settings refresh.
  it("fences a same-session slash finalize against a model change, a system-prompt edit, and a rename made while it awaits, with no navigation involved", async () => {
    const model = (id: string, displayName: string) => ({
      id,
      displayName,
      category: "chat",
      format: "openai",
      conversational: true,
      contextWindow: 128000,
      maxOutputTokens: 32000,
      options: [],
    });
    mocks.listModels.mockResolvedValue({
      models: [model("gpt-5.2", "GPT-5.2"), model("gpt-new", "GPT New")],
    });
    const current = new Map([["A", session("A")]]);
    mocks.listSessions.mockImplementation(async () => [...current.values()]);
    mocks.updateSession.mockImplementation(
      async (id: string, value: Record<string, unknown>) => {
        const updated = { ...current.get(id)!, ...value };
        current.set(id, updated);
        return updated;
      },
    );
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await screen.findByText("Session A", {
      selector: ".chat-header .editable-session-title-text",
    });

    let resolveStaleList!: (value: ReturnType<typeof session>[]) => void;
    const staleList = new Promise<ReturnType<typeof session>[]>((resolve) => {
      resolveStaleList = resolve;
    });
    mocks.listMessages.mockResolvedValueOnce([
      {
        id: "slash-user",
        sessionId: "A",
        userId: "u1",
        role: "user",
        content: "/settings",
        status: "complete",
        model: "gpt-5.2",
        agent: null,
        createdAt: "",
      },
      {
        id: "slash-assistant",
        sessionId: "A",
        userId: "assistant",
        role: "assistant",
        content: "Old command reply",
        status: "complete",
        model: "gpt-5.2",
        agent: null,
        createdAt: "",
      },
    ]);
    mocks.listSessions.mockImplementationOnce(() => staleList);

    await user.click(screen.getByRole("button", { name: "Send slash command" }));
    const oldHandlers = mocks.streamChat.mock.calls.at(-1)![1] as {
      onMetadata: (value: {
        userMessageId: string | null;
        assistantMessageId: string;
      }) => void;
      onDelta: (text: string) => void;
      onDone: () => void;
    };
    act(() => {
      oldHandlers.onMetadata({
        userMessageId: "slash-user",
        assistantMessageId: "slash-assistant",
      });
      oldHandlers.onDelta("Old command reply");
      oldHandlers.onDone();
    });
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Send draft message" })).toBeEnabled(),
    );

    await user.selectOptions(
      screen.getByRole("combobox", { name: "Model" }),
      "gpt-new",
    );
    await user.click(screen.getByRole("button", { name: "Instructions" }));
    const prompt = await screen.findByRole("textbox", { name: "System prompt" });
    await user.clear(prompt);
    await user.type(prompt, "Fresh prompt");

    const header = document.querySelector(".chat-header");
    expect(header).not.toBeNull();
    await user.click(within(header as HTMLElement).getByRole("button", { name: "Rename" }));
    const titleInput = within(header as HTMLElement).getByRole("textbox", {
      name: "Conversation title",
    });
    await user.clear(titleInput);
    await user.type(titleInput, "Renamed A");
    await user.click(within(header as HTMLElement).getByRole("button", { name: "Save" }));
    await screen.findByText("Renamed A", {
      selector: ".chat-header .editable-session-title-text",
    });

    await act(async () => {
      resolveStaleList([
        {
          ...session("A"),
          title: "Stale A",
          model: "gpt-5.2",
          systemPrompt: "Stale prompt",
        },
      ]);
      await Promise.resolve();
    });

    expect(
      screen.getByText("Renamed A", {
        selector: ".chat-header .editable-session-title-text",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Stale A")).toBeNull();
    expect(screen.getByRole("textbox", { name: "System prompt" })).toHaveValue(
      "Fresh prompt",
    );
    await user.click(screen.getByRole("button", { name: "Model" }));
    expect(screen.getByRole("combobox", { name: "Model" })).toHaveValue("gpt-new");
  });
});
