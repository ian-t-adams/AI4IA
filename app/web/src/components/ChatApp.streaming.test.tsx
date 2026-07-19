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
import type { Message, VoiceTurnInput } from "@/lib/types";

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
  appendVoiceTurns: vi.fn(),
}));

// Captures the persistConversation callback ChatApp wires into Voice Live, so
// a test can invoke it directly without mounting the real voice/audio stack.
type PersistConversation = (
  sessionId: string,
  conversationId: string,
  turns: VoiceTurnInput[],
) => Promise<void>;
const voiceMock = vi.hoisted(() => ({
  persistConversation: null as PersistConversation | null,
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
  useInlineVoiceLive: (options: { persistConversation: PersistConversation }) => {
    voiceMock.persistConversation = options.persistConversation;
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
  mocks.appendVoiceTurns.mockResolvedValue([]);
  voiceMock.persistConversation = null;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

// Submits `text` through the already-rendered Composer and returns the
// StreamHandlers ChatApp registered with streamChat for that turn. Split out
// from sendAndCaptureHandlers so a test can drive a second same-session send
// without remounting (and thereby losing) the first turn's component state.
async function sendMessageAndCaptureHandlers(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
): Promise<StreamHandlers> {
  let captured: StreamHandlers | null = null;
  mocks.streamChat.mockImplementationOnce((_input: unknown, handlers: StreamHandlers) => {
    captured = handlers;
    return vi.fn();
  });
  const textbox = await screen.findByLabelText("Message");
  await user.type(textbox, text);
  const sendButton = await screen.findByRole("button", { name: "Send" });
  await waitFor(() => expect(sendButton).toBeEnabled());
  await user.click(sendButton);
  await waitFor(() => expect(captured).not.toBeNull());
  return captured!;
}

// Renders a fresh ChatApp and submits `text` through it.
async function sendAndCaptureHandlers(
  user: ReturnType<typeof userEvent.setup>,
  text: string,
): Promise<StreamHandlers> {
  render(<ChatApp />);
  return sendMessageAndCaptureHandlers(user, text);
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

    // The server echoes this turn's persisted ids before any step/delta --
    // see onMessageIds in api.ts.
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
    });

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
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
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

    // The turn was accepted and persisted (ids echoed) before the upstream
    // model call itself failed mid-stream -- distinct from a pre-persistence
    // rejection (see the 401/422/429 "ghost message" test below), which
    // never reaches this point at all.
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
    });

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
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers.onStep?.({ kind: "tool_start", label: "Looking it up", tool: "web_search" });
      handlers.onStep?.({ kind: "tool_result", label: "Looked it up", tool: "web_search" });
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
    // Only the finalized tool_result counts: the synthetic fallback drops
    // the unfinished-looking tool_start marker (see finalizedSteps).
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
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
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

  it("does not let a stale finalize from a rapid same-session resend wipe the newer turn's live stream", async () => {
    // isSameConversation() only checked sessionId/generation, so a second
    // send in the *same* session (no navigation) wasn't caught by it. A
    // per-turn token is required to detect this case too.
    const user = userEvent.setup();
    const handlers1 = await sendAndCaptureHandlers(user, "First question");
    act(() => {
      handlers1.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers1.onDelta("First partial");
    });
    expect(await screen.findByText("First partial", { exact: false })).toBeInTheDocument();

    let resolveFirstFetch!: (value: Message[]) => void;
    mocks.listMessages.mockImplementationOnce(
      () =>
        new Promise<Message[]>((resolve) => {
          resolveFirstFetch = resolve;
        }),
    );
    const listSessionsCallsBeforeFirstDone = mocks.listSessions.mock.calls.length;
    act(() => {
      handlers1.onDone();
    });

    // Composer re-enables as soon as finalize's synchronous prefix runs,
    // while turn 1's reconciliation fetch is still pending.
    const textbox = await screen.findByLabelText("Message");
    await waitFor(() => expect(textbox).toBeEnabled());

    const handlers2 = await sendMessageAndCaptureHandlers(user, "Second question");
    act(() => {
      handlers2.onMessageIds?.({ userMessageId: "u2", assistantMessageId: "a2" });
      handlers2.onDelta("Second partial");
      handlers2.onStep?.({ kind: "tool_start", label: "Second tool", tool: "web_search" });
    });
    expect(await screen.findByText("Second partial", { exact: false })).toBeInTheDocument();
    expect(await screen.findByText("Second tool")).toBeInTheDocument();

    // Turn 1's stale fetch now resolves successfully. Without a per-turn
    // guard this would reconcile using turn 1's data and wipe turn 2's live
    // streamingText/liveSteps in the process.
    act(() => {
      resolveFirstFetch([
        persistedMessage({ id: "u1", role: "user", content: "First question" }),
        persistedMessage({ id: "a1", role: "assistant", content: "First partial" }),
      ]);
    });
    await waitFor(() =>
      expect(mocks.listSessions.mock.calls.length).toBeGreaterThan(
        listSessionsCallsBeforeFirstDone,
      ),
    );

    expect(await screen.findByText("Second partial", { exact: false })).toBeInTheDocument();
    expect(await screen.findByText("Second tool")).toBeInTheDocument();
  });

  it("supersedes an earlier synthetic fallback reply once a later turn's successful reconciliation lands", async () => {
    // reconcileMessages only drops a previous-only message if its ID is in
    // removeIds. The per-turn optimisticUser.id set used to cover only the
    // *current* turn, so an earlier turn's local-* fallback (created after a
    // failed refetch) never got removed once the real persisted copy arrived.
    const user = userEvent.setup();
    const handlers1 = await sendAndCaptureHandlers(user, "First question");
    act(() => {
      handlers1.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers1.onDelta("Fallback answer");
    });
    expect(await screen.findByText("Fallback answer", { exact: false })).toBeInTheDocument();

    mocks.listMessages.mockRejectedValueOnce(new Error("network blip"));
    act(() => {
      handlers1.onDone();
    });
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Fallback answer")).toBeInTheDocument();

    // Captured now, in turn 1's own window, rather than alongside turn 2's
    // fixture below: persistedMessage() timestamps default to call time, so
    // building every turn's messages in one later literal would push turn
    // 1's createdAt after turn 2's window start and break its resolution.
    const turn1Persisted = [
      persistedMessage({ id: "u1", role: "user", content: "First question" }),
      persistedMessage({ id: "a1", role: "assistant", content: "Fallback answer" }),
    ];

    const handlers2 = await sendMessageAndCaptureHandlers(user, "Second question");
    act(() => {
      handlers2.onMessageIds?.({ userMessageId: "u2", assistantMessageId: "a2" });
      handlers2.onDelta("Second answer");
    });
    expect(await screen.findByText("Second answer", { exact: false })).toBeInTheDocument();

    // Turn 2 succeeds, and its reconciliation returns the full authoritative
    // history -- including turn 1's real persisted reply under a different ID.
    mocks.listMessages.mockResolvedValueOnce([
      ...turn1Persisted,
      persistedMessage({ id: "u2", role: "user", content: "Second question" }),
      persistedMessage({ id: "a2", role: "assistant", content: "Second answer" }),
    ]);
    act(() => {
      handlers2.onDone();
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(2));
    // Only the persisted copy should remain -- no duplicate from the earlier
    // synthetic local-* fallback bubble.
    expect(await screen.findAllByText("Fallback answer")).toHaveLength(1);
    expect(await screen.findByText("Second answer")).toBeInTheDocument();
  });

  it("retries a stale-but-successful reconciliation and falls back to the buffered reply instead of dropping it", async () => {
    // The backend can send SSE [DONE] before its own finally-block Cosmos
    // upsert lands, so a refetch right after [DONE] can succeed (no throw)
    // yet still return the pre-completion "streaming" placeholder.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Stale reconciliation question");
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers.onDelta("Answer built from the live stream.");
    });
    expect(
      await screen.findByText("Answer built from the live stream.", { exact: false }),
    ).toBeInTheDocument();

    const streamingPlaceholder = () => [
      persistedMessage({ id: "u1", role: "user", content: "Stale reconciliation question" }),
      persistedMessage({ id: "a1", role: "assistant", content: "", status: "streaming" }),
    ];
    mocks.listMessages.mockResolvedValueOnce(streamingPlaceholder());
    mocks.listMessages.mockResolvedValueOnce(streamingPlaceholder());
    act(() => {
      handlers.onDone();
    });

    // Both the initial attempt and its retry must run before giving up.
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(2), { timeout: 3000 });
    // Neither attempt ever showed a terminal reply, so the buffered stream
    // content must still render instead of being silently dropped.
    expect(
      await screen.findByText(
        "Answer built from the live stream.",
        {},
        { timeout: 3000 },
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Generating")).toBeNull();
  });

  it("keeps the first turn's finalized reply on screen while a second turn streams live in the same session", async () => {
    // HIGH-1: finalize() used to clear the shared streamingText immediately,
    // relying entirely on a later fetch to put the reply back. If a second
    // send started in the same session before that fetch resolved, the
    // first finalize's isCurrentTurn() check would fail and it would commit
    // neither the fetched history nor a fallback -- the reply vanished with
    // nothing left to render it. Materializing a placeholder before the
    // streaming lock is released closes that gap.
    const user = userEvent.setup();
    const handlers1 = await sendAndCaptureHandlers(user, "First question");
    act(() => {
      handlers1.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers1.onDelta("First reply");
    });
    expect(await screen.findByText("First reply", { exact: false })).toBeInTheDocument();

    let resolveFirstFetch!: (value: Message[]) => void;
    mocks.listMessages.mockImplementationOnce(
      () =>
        new Promise<Message[]>((resolve) => {
          resolveFirstFetch = resolve;
        }),
    );
    act(() => {
      handlers1.onDone();
    });

    // The placeholder renders synchronously, before turn 1's fetch has any
    // chance to settle.
    expect(await screen.findByText("First reply")).toBeInTheDocument();

    const handlers2 = await sendMessageAndCaptureHandlers(user, "Second question");
    act(() => {
      handlers2.onMessageIds?.({ userMessageId: "u2", assistantMessageId: "a2" });
      handlers2.onDelta("Second live reply");
    });

    // Both must be visible at once: turn 1's finalized placeholder and turn
    // 2's still-live stream.
    expect(await screen.findByText("First reply")).toBeInTheDocument();
    expect(
      await screen.findByText("Second live reply", { exact: false }),
    ).toBeInTheDocument();

    act(() => {
      resolveFirstFetch([
        persistedMessage({ id: "u1", role: "user", content: "First question" }),
        persistedMessage({ id: "a1", role: "assistant", content: "First reply" }),
      ]);
    });

    // Turn 1's late reconciliation lands cleanly without disturbing turn 2.
    await waitFor(() => expect(screen.queryAllByText("First reply")).toHaveLength(1));
    expect(
      await screen.findByText("Second live reply", { exact: false }),
    ).toBeInTheDocument();
  });

  it("does not let a second turn's successful reconciliation erase an earlier turn still streaming server-side", async () => {
    // HIGH-2: the old design judged "freshness" solely from the *latest*
    // assistant message, then cleared every locally-tracked id in bulk. If
    // turn A's backend call was genuinely still in flight while turn B (a
    // later, same-session turn) finished and reconciled cleanly, B's fresh,
    // complete assistant made the whole fetch look "fresh" and erased A's
    // fallback even though no terminal persisted A reply existed anywhere.
    // Per-turn windows fix this: B's fetch can only resolve *B's* window.
    const user = userEvent.setup();
    const handlers1 = await sendAndCaptureHandlers(user, "Turn A question");
    act(() => {
      handlers1.onMessageIds?.({ userMessageId: "ua", assistantMessageId: "aa" });
      handlers1.onDelta("A reply");
    });
    expect(await screen.findByText("A reply", { exact: false })).toBeInTheDocument();

    // Captured now (before turn B starts) so these timestamps land inside
    // turn A's own resolution window rather than turn B's -- see the
    // turn1Persisted note in the fallback-supersession test above.
    const turnAStillStreaming = [
      persistedMessage({ id: "ua", role: "user", content: "Turn A question" }),
      persistedMessage({ id: "aa", role: "assistant", content: "", status: "streaming" }),
    ];
    // Turn A's own reconciliation attempts both see the backend's
    // still-in-progress placeholder and give up after the retry budget.
    mocks.listMessages.mockResolvedValueOnce(turnAStillStreaming);
    mocks.listMessages.mockResolvedValueOnce(turnAStillStreaming);
    act(() => {
      handlers1.onDone();
    });
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(2), { timeout: 3000 });
    expect(await screen.findByText("A reply")).toBeInTheDocument();

    const handlers2 = await sendMessageAndCaptureHandlers(user, "Turn B question");
    act(() => {
      handlers2.onMessageIds?.({ userMessageId: "ub", assistantMessageId: "ab" });
      handlers2.onDelta("B reply");
    });
    expect(await screen.findByText("B reply", { exact: false })).toBeInTheDocument();

    // B's own fetch succeeds immediately: its assistant reply is the latest
    // and complete, even though A (earlier in the same fetch) is still
    // shown mid-stream.
    mocks.listMessages.mockResolvedValueOnce([
      ...turnAStillStreaming,
      persistedMessage({ id: "ub", role: "user", content: "Turn B question" }),
      persistedMessage({ id: "ab", role: "assistant", content: "B reply" }),
    ]);
    act(() => {
      handlers2.onDone();
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(3));
    // A's fallback survives, untouched and not duplicated.
    await waitFor(() => expect(screen.queryAllByText("A reply")).toHaveLength(1));
    expect(await screen.findByText("B reply")).toBeInTheDocument();
  });

  it("stops retrying reconciliation once the component unmounts mid-retry", async () => {
    // MEDIUM-1: the retry loop had no way to know the component (or turn)
    // was gone, so an in-flight retry could fire its next attempt -- and
    // try to update state -- after unmount. mountedRef fences every
    // attempt/result so an abandoned turn's polling actually stops.
    const user = userEvent.setup();
    const { unmount } = render(<ChatApp />);
    const handlers = await sendMessageAndCaptureHandlers(user, "Question");
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers.onDelta("Partial reply");
    });
    expect(await screen.findByText("Partial reply", { exact: false })).toBeInTheDocument();

    // The first attempt sees a stale (still-streaming) result, so
    // fetchReconciledMessages schedules a retry after the retry delay.
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "Question" }),
      persistedMessage({ id: "a1", role: "assistant", content: "", status: "streaming" }),
    ]);
    act(() => {
      handlers.onDone();
    });
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(1));
    const listSessionsCallsBeforeUnmount = mocks.listSessions.mock.calls.length;

    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    unmount();

    // Wait well past the retry delay: neither the retry attempt nor the
    // post-reconciliation session refresh may fire after unmount.
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(mocks.listMessages).toHaveBeenCalledTimes(1);
    expect(mocks.listSessions.mock.calls.length).toBe(listSessionsCallsBeforeUnmount);
    expect(consoleErrorSpy).not.toHaveBeenCalled();
    consoleErrorSpy.mockRestore();
  });

  it("routes a completed Voice Live exchange through the same per-turn reconciliation as text chat, without duplicating a fallback bubble", async () => {
    // MEDIUM-2: persistVoiceConversation used to reconcile its listMessages()
    // result with a bespoke, voice-only diff (dedup by newly-created id
    // only), bypassing pendingTurnsRef entirely. A text-chat fallback bubble
    // tracked there was never removed once its real persisted copy arrived
    // through the voice merge, leaving a duplicate. Routing both paths
    // through applyReconciledMessages fixes that.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Text turn");
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers.onDelta("Text fallback reply");
    });
    expect(
      await screen.findByText("Text fallback reply", { exact: false }),
    ).toBeInTheDocument();

    mocks.listMessages.mockRejectedValueOnce(new Error("network blip"));
    act(() => {
      handlers.onDone();
    });
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Text fallback reply")).toBeInTheDocument();

    expect(voiceMock.persistConversation).not.toBeNull();
    mocks.appendVoiceTurns.mockResolvedValueOnce([
      persistedMessage({ id: "voice-1", role: "assistant", content: "Voice reply" }),
    ]);
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "Text turn" }),
      persistedMessage({ id: "a1", role: "assistant", content: "Text fallback reply" }),
      persistedMessage({ id: "voice-1", role: "assistant", content: "Voice reply" }),
    ]);

    await act(async () => {
      await voiceMock.persistConversation?.("A", "conv-1", [
        { role: "user", text: "Voice question" },
      ]);
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(2));
    // The real persisted copy replaces the tracked fallback -- no
    // duplicate -- and the newly-appended voice turn is present too.
    expect(await screen.findAllByText("Text fallback reply")).toHaveLength(1);
    expect(await screen.findByText("Voice reply")).toBeInTheDocument();
  });

  it("resolves a turn by its exact persisted id even when the fetched row's timestamp is skewed from the browser's clock", async () => {
    // HIGH-1: the previous design compared a browser-captured "since"
    // timestamp against server createdAt values, so clock skew between
    // browser and server could make a genuinely-complete reply look like it
    // belonged to the wrong window. Exact-id matching never reads createdAt
    // at all, so a wildly skewed timestamp on the correct row must still
    // resolve the turn.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "What time is it on the server?");
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers.onDelta("It's always server time somewhere.");
    });
    expect(
      await screen.findByText("It's always server time somewhere.", { exact: false }),
    ).toBeInTheDocument();

    // The server's clock is deliberately six hours ahead of the browser's --
    // an id match must not care.
    const skewed = new Date(Date.now() + 6 * 60 * 60 * 1000).toISOString();
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({
        id: "u1",
        role: "user",
        content: "What time is it on the server?",
        createdAt: skewed,
      }),
      persistedMessage({
        id: "a1",
        role: "assistant",
        content: "It's always server time somewhere.",
        createdAt: skewed,
      }),
    ]);
    act(() => {
      handlers.onDone();
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("It's always server time somewhere."),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Generating")).toBeNull();
  });

  it("does not let a complete, interleaved Voice Live reply falsely resolve a text turn that is still streaming server-side", async () => {
    // HIGH-1: a timestamp/freshness heuristic could see *any* new complete
    // assistant row -- including one from an entirely unrelated Voice Live
    // exchange -- and conclude the turn it's tracking must be done. Exact id
    // matching must see straight through this: only a fetched row whose id
    // equals this turn's own assistantMessageId can resolve it, no matter
    // what else completed around the same time.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Text question");
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers.onDelta("Text answer");
    });
    expect(await screen.findByText("Text answer", { exact: false })).toBeInTheDocument();

    const voiceReply = persistedMessage({
      id: "voice-1",
      role: "assistant",
      content: "Voice reply",
    });
    // First attempt: the text turn's own row is still "streaming"
    // server-side, even though an unrelated Voice Live reply already
    // completed and persisted.
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "Text question" }),
      persistedMessage({ id: "a1", role: "assistant", content: "", status: "streaming" }),
      voiceReply,
    ]);
    // Retry: the text turn's own row is now complete too.
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "Text question" }),
      persistedMessage({ id: "a1", role: "assistant", content: "Text answer" }),
      voiceReply,
    ]);
    act(() => {
      handlers.onDone();
    });

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(2), { timeout: 3000 });
    // Exactly one copy of the text reply -- a false-positive resolve on the
    // first attempt would have left a synthetic fallback duplicate behind --
    // and the unrelated Voice reply is present too.
    await waitFor(() => expect(screen.queryAllByText("Text answer")).toHaveLength(1));
    expect(await screen.findByText("Voice reply")).toBeInTheDocument();
    expect(screen.queryByLabelText("Generating")).toBeNull();
  });

  it("does not let clicking the already-open session replace a not-yet-reconciled reply with a stale snapshot", async () => {
    // HIGH-2: selectSession's raw setMessages(msgs) bypassed pending-aware
    // reconciliation entirely, so re-selecting the conversation already on
    // screen -- while finalize's own reconciliation fetch for the turn that
    // *just* finished is still in flight -- could stomp the fallback with
    // whatever pre-completion snapshot that click's own, independent fetch
    // happened to see.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Will I survive a re-click?");
    act(() => {
      handlers.onMessageIds?.({ userMessageId: "u1", assistantMessageId: "a1" });
      handlers.onDelta("Yes, I will.");
    });
    expect(await screen.findByText("Yes, I will.", { exact: false })).toBeInTheDocument();

    // finalize()'s own reconciliation fetch is left pending for the whole
    // test -- the re-click below issues its own, independent listMessages()
    // call, which is what's actually under test here.
    let resolveOwnFetch!: (value: Message[]) => void;
    mocks.listMessages.mockImplementationOnce(
      () =>
        new Promise<Message[]>((resolve) => {
          resolveOwnFetch = resolve;
        }),
    );
    act(() => {
      handlers.onDone();
    });
    // The placeholder renders synchronously, before finalize's own fetch has
    // any chance to settle.
    expect(await screen.findByText("Yes, I will.")).toBeInTheDocument();

    // The user re-clicks the conversation already open in the sidebar while
    // the backend's own Cosmos upsert may still be in flight; this click's
    // *own* fetch still sees the pre-completion "streaming" placeholder.
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "u1", role: "user", content: "Will I survive a re-click?" }),
      persistedMessage({ id: "a1", role: "assistant", content: "", status: "streaming" }),
    ]);
    const sessionButton = await screen.findByRole("button", { name: "Session A" });
    await waitFor(() => expect(sessionButton).toBeEnabled());
    await user.click(sessionButton);

    // The re-click's stale snapshot must not have replaced the reply.
    expect(await screen.findByText("Yes, I will.")).toBeInTheDocument();

    // finalize()'s original fetch now resolves too, with the real persisted
    // reply -- both reconciliation paths converge safely, with no duplicate.
    act(() => {
      resolveOwnFetch([
        persistedMessage({ id: "u1", role: "user", content: "Will I survive a re-click?" }),
        persistedMessage({ id: "a1", role: "assistant", content: "Yes, I will." }),
      ]);
    });
    await waitFor(() => expect(screen.queryAllByText("Yes, I will.")).toHaveLength(1));
  });

  it("removes the optimistic user bubble instead of leaving a ghost message when the backend rejects the turn before persisting anything", async () => {
    // MEDIUM: a pre-persistence HTTP rejection (401/422/429) never reaches
    // the SSE loop at all -- api.ts's own `!resp.ok` branch calls onError
    // directly, with no onMessageIds and no buffered content/steps ever
    // having arrived. Since nothing was ever persisted, there is nothing to
    // reconcile: the optimistic user bubble must be removed immediately
    // rather than left as an unresolvable ghost that no future fetch could
    // ever clear.
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "This will be refused");
    expect(await screen.findByText("This will be refused")).toBeInTheDocument();

    act(() => {
      handlers.onRejected?.(429, "Too many requests. Try again in 30 seconds.");
      handlers.onError("429: Too many requests. Try again in 30 seconds.");
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("429: Too many requests");
    // The ghost user bubble is gone -- nothing was ever persisted for it, so
    // there is no history for a later refresh to bring back either.
    await waitFor(() => expect(screen.queryByText("This will be refused")).toBeNull());
    // No reconciliation fetch was attempted: there was nothing accepted, so
    // there is nothing to reconcile.
    expect(mocks.listMessages).not.toHaveBeenCalled();

    // The composer is immediately usable again for a retry.
    const textbox = await screen.findByLabelText("Message");
    await waitFor(() => expect(textbox).toBeEnabled());
  });

  it("reconciles by the browser clientTurnId even when the first metadata SSE is lost", async () => {
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Recover without metadata");
    const input = mocks.streamChat.mock.calls[0][0] as {
      clientTurnId: string;
    };
    expect(input.clientTurnId).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({
        id: "server-user",
        role: "user",
        content: "Recover without metadata",
        source: "chat",
        clientTurnId: input.clientTurnId,
      }),
      persistedMessage({
        id: "server-assistant",
        role: "assistant",
        content: "Recovered durably",
        source: "chat",
        clientTurnId: input.clientTurnId,
      }),
    ]);

    act(() => handlers.onDone());

    expect(await screen.findByText("Recovered durably")).toBeInTheDocument();
    expect(screen.getAllByText("Recover without metadata")).toHaveLength(1);
  });

  it("keeps the local fallback without guessing against a fully old null-ID server", async () => {
    const user = userEvent.setup();
    const handlers = await sendAndCaptureHandlers(user, "Legacy request");
    act(() => handlers.onDelta("Legacy answer"));
    mocks.listMessages.mockResolvedValue([
      persistedMessage({ id: "legacy-u", role: "user", content: "Legacy request" }),
      persistedMessage({
        id: "legacy-a",
        role: "assistant",
        content: "Legacy answer",
      }),
    ]);

    act(() => handlers.onDone());

    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(2));
    expect(screen.getAllByText("Legacy request")).toHaveLength(1);
    expect(screen.getAllByText("Legacy answer")).toHaveLength(1);
  });

  it("discards an older same-session history snapshot that resolves last", async () => {
    mocks.listSessions.mockResolvedValue([session("A")]);
    const user = userEvent.setup();
    render(<ChatApp />);
    const sessionButton = await screen.findByRole("button", { name: "Session A" });
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "a1", role: "assistant", content: "initial" }),
    ]);
    await user.click(sessionButton);
    expect(await screen.findByText("initial")).toBeInTheDocument();

    let resolveOlder!: (messages: Message[]) => void;
    let resolveNewer!: (messages: Message[]) => void;
    mocks.listMessages
      .mockImplementationOnce(
        () =>
          new Promise<Message[]>((resolve) => {
            resolveOlder = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<Message[]>((resolve) => {
            resolveNewer = resolve;
          }),
      );
    await user.click(sessionButton);
    await waitFor(() => expect(resolveOlder).toBeTypeOf("function"));
    await user.click(sessionButton);
    await waitFor(() => expect(resolveNewer).toBeTypeOf("function"));
    act(() => {
      resolveNewer([
        persistedMessage({ id: "a1", role: "assistant", content: "newest" }),
      ]);
    });
    expect(await screen.findByText("newest")).toBeInTheDocument();
    act(() => {
      resolveOlder([
        persistedMessage({ id: "a1", role: "assistant", content: "stale older" }),
      ]);
    });
    await act(async () => {});
    expect(screen.queryByText("stale older")).toBeNull();
    expect(screen.getByText("newest")).toBeInTheDocument();
  });

  it("never regresses a terminal assistant row to a delayed streaming snapshot", async () => {
    mocks.listSessions.mockResolvedValue([session("A")]);
    const user = userEvent.setup();
    render(<ChatApp />);
    const sessionButton = await screen.findByRole("button", { name: "Session A" });
    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({ id: "a1", role: "assistant", content: "final answer" }),
    ]);
    await user.click(sessionButton);
    expect(await screen.findByText("final answer")).toBeInTheDocument();

    mocks.listMessages.mockResolvedValueOnce([
      persistedMessage({
        id: "a1",
        role: "assistant",
        content: "",
        status: "streaming",
      }),
    ]);
    await user.click(sessionButton);
    await waitFor(() => expect(mocks.listMessages).toHaveBeenCalledTimes(2));
    expect(screen.getByText("final answer")).toBeInTheDocument();
  });
});
