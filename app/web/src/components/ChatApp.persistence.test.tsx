// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ChatApp } from "./ChatApp";
import type { StreamHandlers } from "@/lib/api";
import type { ToolCatalogItem } from "@/lib/types";
import { resetChatAppMocks } from "./chatTestFixtures";

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
    streaming,
  }: {
    onSend: (text: string) => void;
    streaming: boolean;
  }) => (
    <button
      type="button"
      disabled={streaming}
      onClick={() => onSend("hello from draft")}
    >
      Send draft message
    </button>
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
  resetChatAppMocks(mocks);
  mocks.voiceOptions = null;
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  document.documentElement.style.removeProperty("--font-scale");
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
