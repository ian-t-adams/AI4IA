// @vitest-environment jsdom
//
// End-to-end streaming render regression tests. Unlike ChatApp.test.tsx (which
// mocks out MessageList and streamChat entirely), these tests render the real
// MessageList and drive the real ChatApp state machine through streamChat's
// StreamHandlers, so a bug in how ChatApp feeds live state to real DOM output
// can actually be caught. This covers the reported production symptom:
// submitted/streamed chat text not rendering until a manual refresh.
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatApp } from "./ChatApp";
import type { StreamHandlers } from "@/lib/api";
import type { Message } from "@/lib/types";

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
  fetchImageArtifact: vi.fn(),
  fetchVideoArtifact: vi.fn(),
  fetchDocumentArtifact: vi.fn(),
}));

vi.mock("@/lib/api", () => mocks);
vi.mock("@/lib/inspector", () => ({
  getInspector: mocks.getInspector,
  listMemories: mocks.listMemories,
  getLibrarySummary: mocks.getLibrarySummary,
  deleteMemory: mocks.deleteMemory,
}));
// Speech playback owns <audio> + object-URL plumbing and hits the TTS endpoint;
// stub it the same way MessageList.test.tsx does so real MessageList mounts
// cleanly without audio/network.
vi.mock("@/lib/voice", () => ({
  useSpeechPlayback: () => ({ activeId: null, busyId: null, toggle: vi.fn() }),
}));
vi.mock("./VoiceLiveProvider", () => ({
  useVoiceLiveConfig: () => ({ enabled: false, toolsAvailable: false }),
}));
vi.mock("./LibraryProvider", () => ({
  useLibraryConfig: () => ({ enabled: false }),
}));
vi.mock("./CustomToolsProvider", () => ({
  useCustomToolsConfig: () => ({ enabled: false }),
}));
vi.mock("./AdminLink", () => ({ AdminLink: () => null }));
vi.mock("./UserMenu", () => ({ UserMenu: () => null }));
vi.mock("./InlineVoiceLive", () => ({
  InlineVoiceLiveStatus: () => null,
  mergeDisplayMessages: (messages: unknown[]) => messages,
  voiceMessagesForSession: () => [],
  useInlineVoiceLive: () => ({
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
  }),
}));

// jsdom has no layout engine, so scrollIntoView (called by MessageList in an
// effect after every render) is undefined; provide a no-op so mounting the
// real MessageList doesn't throw. Real browsers always implement this.
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

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

function persistedMessage(over: Partial<Message> & Pick<Message, "id" | "role" | "content">): Message {
  return {
    sessionId: "A",
    userId: "me",
    status: "complete",
    model: "gpt-5.2",
    agent: null,
    createdAt: new Date().toISOString(),
    ...over,
  };
}

// Realistic InspectorSnapshot fixture. ConversationInspector is always
// mounted alongside ChatApp and fetches this on session load; giving it a
// well-formed response (matching the real API contract) keeps it from
// throwing/rendering its own incidental "alert"/"status" DOM, which would
// otherwise collide with these tests' own alert/status assertions.
function inspectorSnapshot(sessionId: string) {
  return {
    generatedAt: new Date().toISOString(),
    sessionId,
    title: `Session ${sessionId}`,
    model: {
      id: "gpt-5.2",
      displayName: "GPT-5.2",
      contextWindow: 128000,
      maxOutputTokens: 32000,
    },
    instructions: {
      source: "default" as const,
      editable: true,
      value: null,
      agentName: null,
    },
    agent: {
      enabled: false,
      name: null,
      displayName: null,
      description: null,
    },
    tools: {
      inherited: [],
      added: [],
      removed: [],
      effective: [],
      voiceEffective: [],
    },
    attachments: [],
    libraryDocuments: [],
    librarySelectionMode: "explicit" as const,
    sessionUsage: {
      sessionId,
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
      applies: "next_connection" as const,
    },
  };
}

beforeEach(() => {
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
  mocks.listSessions.mockResolvedValue([]);
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
    ...session("A"),
    ...value,
  }));
  mocks.listTools.mockResolvedValue([]);
  mocks.getToolCatalog.mockResolvedValue({ tools: [], inheritedTools: [] });
  mocks.getInspector.mockImplementation((sessionId: string) =>
    Promise.resolve(inspectorSnapshot(sessionId)),
  );
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
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Submits `text` through the real Composer and returns the StreamHandlers
// ChatApp registered with streamChat for that turn.
async function sendAndCaptureHandlers(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
): Promise<StreamHandlers> {
  let captured: StreamHandlers | null = null;
  mocks.streamChat.mockImplementationOnce((_input: unknown, handlers: StreamHandlers) => {
    captured = handlers;
    return vi.fn();
  });
  render(<ChatApp />);
  const textbox = await screen.findByLabelText("Message");
  await user.type(textbox, text);
  const sendButton = await screen.findByRole("button", { name: "Send" });
  await waitFor(() => expect(sendButton).toBeEnabled());
  await user.click(sendButton);
  await waitFor(() => expect(captured).not.toBeNull());
  return captured!;
}

describe("ChatApp streaming render (real MessageList, no mocks on the render path)", () => {
  it("renders the submitted user message immediately, without waiting on the stream", async () => {
    const user = userEvent.setup();
    mocks.streamChat.mockImplementation(() => vi.fn());
    render(<ChatApp />);

    const textbox = await screen.findByLabelText("Message");
    await user.type(textbox, "Hello there");
    const sendButton = await screen.findByRole("button", { name: "Send" });
    await waitFor(() => expect(sendButton).toBeEnabled());
    await user.click(sendButton);

    // The optimistic user bubble must appear synchronously with the send,
    // long before any network/stream activity resolves.
    expect(await screen.findByText("Hello there")).toBeInTheDocument();
  });

  it("renders streamed assistant deltas live and persists activity steps after finalize, with no refresh", async () => {
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "What's the weather?");

    expect(await screen.findByText("What's the weather?")).toBeInTheDocument();

    // Live activity step arrives before any content.
    act(() => {
      handlers.onStep?.({ kind: "tool_start", label: "Searching the web", tool: "web_search" });
    });
    expect(await screen.findByText("Searching the web")).toBeInTheDocument();

    // Streamed content deltas must render incrementally as they arrive.
    act(() => {
      handlers.onDelta("It's ");
    });
    expect(await screen.findByText("It's", { exact: false })).toBeInTheDocument();

    act(() => {
      handlers.onDelta("sunny today.");
    });
    expect(await screen.findByText("It's sunny today.")).toBeInTheDocument();

    // Finalize: backend has persisted both turns with the redacted step trace.
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "What's the weather?" }),
      persistedMessage({
        id: "a1",
        role: "assistant",
        content: "It's sunny today.",
        steps: [{ kind: "tool_start", label: "Searching the web", tool: "web_search" }],
      }),
    ]);
    act(() => {
      handlers.onDone();
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalled());
    // Final reconciled content renders without any refresh/remount.
    expect(await screen.findByText("It's sunny today.")).toBeInTheDocument();
    // The step trace persists, collapsed, on the finished turn.
    expect(await screen.findByText(/Activity · 1 step/)).toBeInTheDocument();
    // No stray "generating" indicator remains once the turn is done.
    expect(screen.queryByLabelText("Generating")).toBeNull();
  });

  it("reconciles to the server's cancelled message on abort, without leaving stale streaming UI", async () => {
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Cancel me");

    act(() => {
      handlers.onDelta("partial reply");
    });
    expect(await screen.findByText("partial reply", { exact: false })).toBeInTheDocument();

    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "Cancel me" }),
      persistedMessage({
        id: "a1",
        role: "assistant",
        content: "partial reply",
        status: "cancelled",
      }),
    ]);
    act(() => {
      handlers.onAbort?.();
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalled());
    expect(await screen.findByText("partial reply")).toBeInTheDocument();
    // The streaming placeholder + its cursor/spinner must be gone.
    expect(screen.queryByLabelText("Generating")).toBeNull();
    expect(screen.getByText("Ready")).toBeInTheDocument();
  });

  it("surfaces stream errors and still reconciles the persisted state", async () => {
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Trigger an error");

    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "Trigger an error" }),
      persistedMessage({ id: "a1", role: "assistant", content: "", status: "error" }),
    ]);
    act(() => {
      handlers.onError("502: upstream failure");
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("502: upstream failure");
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalled());
    expect(await screen.findByText("Trigger an error")).toBeInTheDocument();
  });

  it("reproduces the reported production bug: reconciliation fetch fails after the stream finishes, and the reply must not vanish", async () => {
    // This is the direct regression test for the root cause: finalize()
    // used to clear the streaming placeholder immediately, then rely
    // entirely on a `listMessages()` refetch to put the finished reply back
    // into `messages`. If that refetch failed for any transient reason, the
    // fully-streamed assistant reply disappeared with no recovery short of a
    // manual page refresh -- exactly the symptom reported in production.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "What's the capital of France?");

    act(() => {
      handlers.onStep?.({ kind: "tool_start", label: "Looking it up", tool: "web_search" });
      handlers.onDelta("Paris is the capital of France.");
    });
    expect(
      await screen.findByText("Paris is the capital of France.", { exact: false }),
    ).toBeInTheDocument();

    // The backend has already persisted the turn (independently, in its own
    // `finally`), but the frontend's post-stream reconciliation fetch fails.
    mocks.listMessages.mockRejectedValueOnce(new Error("network blip"));
    act(() => {
      handlers.onDone();
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalled());
    // The reply must still render -- reconstructed from the buffered stream
    // content -- instead of silently disappearing.
    expect(await screen.findByText("Paris is the capital of France.")).toBeInTheDocument();
    expect(await screen.findByText(/Activity · 1 step/)).toBeInTheDocument();
    // No stale "generating" placeholder should linger once finalize settles.
    expect(screen.queryByLabelText("Generating")).toBeNull();
  });

  it("does not let a stale reconciliation from an abandoned turn pollute a conversation the user has since switched to", async () => {
    // finalize() clears the streaming lock (which normally blocks
    // navigation) as its very first action, *before* awaiting the
    // reconciliation fetch. That opens a real window where the user can
    // switch conversations before the fetch resolves; this must not let the
    // old turn's data leak into whatever the user is looking at now.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Old session question");

    act(() => {
      handlers.onDelta("Old session answer.");
    });
    expect(
      await screen.findByText("Old session answer.", { exact: false }),
    ).toBeInTheDocument();

    let resolveListMessages!: (value: Message[]) => void;
    mocks.listMessages.mockImplementationOnce(
      () =>
        new Promise<Message[]>((resolve) => {
          resolveListMessages = resolve;
        }),
    );
    const listSessionsCallsBeforeDone = mocks.listSessions.mock.calls.length;
    act(() => {
      handlers.onDone();
    });

    // The streaming lock is gone, so sidebar navigation is available again
    // even though this turn's reconciliation fetch is still in flight.
    const newChatButton = await screen.findByRole("button", { name: "+ New chat" });
    await waitFor(() => expect(newChatButton).toBeEnabled());

    // The user starts a new conversation before the stale fetch resolves.
    await user.click(newChatButton);
    expect(screen.queryByText("Old session question")).toBeNull();

    // The stale fetch for the abandoned turn now resolves with its
    // persisted messages. Without the isSameConversation() guard, this
    // would splice the old turn's reply into the new, unrelated conversation.
    act(() => {
      resolveListMessages([
        persistedMessage({ id: "u1", role: "user", content: "Old session question" }),
        persistedMessage({ id: "a1", role: "assistant", content: "Old session answer." }),
      ]);
    });
    // finalize()'s post-fetch continuation always ends by refreshing the
    // session list; wait for that so we know the (guarded) reconciliation
    // has fully run before asserting nothing leaked.
    await waitFor(() =>
      expect(mocks.listSessions.mock.calls.length).toBeGreaterThan(listSessionsCallsBeforeDone),
    );

    expect(screen.queryByText("Old session question")).toBeNull();
    expect(screen.queryByText("Old session answer.")).toBeNull();
  });
});
