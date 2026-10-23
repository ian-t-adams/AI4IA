// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { StreamHandlers } from "@/lib/api";
import type { Message, PendingToolApprovalPrompt, Session, ToolCatalogItem } from "@/lib/types";
import { ChatApp } from "./ChatApp";
import { MemoryPreferenceProvider } from "./MemoryPreferenceProvider";
import { makeChatSession, resetChatAppMocks } from "./chatTestFixtures";

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
  getImageOptions: vi.fn(async () => ({
    maxSelectedModels: 3, currency: "USD", priceVersion: null, models: [],
  })),
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
vi.mock("./MessageList", () => ({
  MessageList: ({ messages }: { messages: Message[] }) => (
    <div aria-label="Conversation">
      {messages.map((message) => <p key={message.id}>{message.content}</p>)}
    </div>
  ),
}));
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

type ChatInput = Parameters<typeof import("@/lib/api").streamChat>[0];
const streams: { input: ChatInput; handlers: StreamHandlers }[] = [];
const restricted = { allowTools: false, allowAutomaticMemory: false } as const;

function approval(id = "request-1"): PendingToolApprovalPrompt {
  return {
    id,
    messageId: "assistant-0",
    operationId: "0123456789abcdef:1:0",
    tool: "synthetic_tool",
    label: "Synthetic tool",
    host: null,
    purpose: "Synthetic continuation control",
    risk: "external",
    argumentsDigest: "d".repeat(64),
    argumentsPreview: {},
    grantHash: "h".repeat(64),
    expiresAt: "2099-01-01T00:00:00Z",
    createdAt: "2026-10-22T00:00:00Z",
    grant: "synthetic-grant",
  };
}

const checkbox = () => screen.getByRole("checkbox", { name: "Text only" });
const textarea = () => screen.getByRole("combobox", { name: "Message" });

async function openChat() {
  const user = userEvent.setup();
  render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
  await user.click(await screen.findByRole("button", { name: "Session A" }));
  return user;
}

async function submit(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(textarea(), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

async function finish(index: number, prompts: PendingToolApprovalPrompt[] = []) {
  const { input, handlers } = streams[index];
  const messages: Message[] = [
    {
      id: `user-${index}`,
      sessionId: input.sessionId,
      userId: "u1",
      role: "user",
      content: input.content,
      status: "complete",
      model: input.model ?? null,
      agent: null,
      createdAt: "2026-10-22T00:00:00Z",
    },
    {
      id: `assistant-${index}`,
      sessionId: input.sessionId,
      userId: "u1",
      role: "assistant",
      content: "Synthetic answer",
      status: "complete",
      model: input.model ?? null,
      agent: null,
      createdAt: "2026-10-22T00:00:01Z",
    },
  ];
  mocks.listMessages.mockResolvedValue(messages);
  act(() => {
    handlers.onMetadata({ userMessageId: messages[0].id, assistantMessageId: messages[1].id });
    handlers.onDelta("Synthetic answer");
    if (prompts.length) handlers.onApprovals?.(prompts);
    handlers.onDone();
  });
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument(),
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  resetChatAppMocks(mocks);
  mocks.getImageOptions.mockResolvedValue({
    maxSelectedModels: 3, currency: "USD", priceVersion: null, models: [],
  });
  streams.length = 0;
  mocks.streamChat.mockImplementation((input: ChatInput, handlers: StreamHandlers) => {
    streams.push({ input, handlers });
    return vi.fn();
  });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ChatApp per-turn text-only requests", () => {
  it.each([false, true])("captures the send choice and resets only that choice (text-only=%s)", async (textOnly) => {
    const user = await openChat();
    expect(checkbox()).not.toBeChecked();
    if (textOnly) await user.click(checkbox());
    await submit(user, "ordinary question");
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(streams[0].input).toEqual({
      sessionId: "A",
      content: "ordinary question",
      model: "gpt-5.2",
      params: { temperature: 0.7, top_p: 1, max_tokens: 1024 },
      approvals: undefined,
      ...(textOnly ? restricted : {}),
    });
    expect(checkbox()).not.toBeChecked();
    expect(mocks.updateSession).not.toHaveBeenCalled();
    expect(mocks.createMemory).not.toHaveBeenCalled();
    expect(mocks.updateMemory).not.toHaveBeenCalled();
    expect(mocks.deleteMemory).not.toHaveBeenCalled();
    await finish(0);
    await submit(user, "next question");
    expect(streams[1].input).not.toHaveProperty("allowTools");
    expect(streams[1].input).not.toHaveProperty("allowAutomaticMemory");
  });

  it.each([false, true])("keeps the originating choice on approval continuation (text-only=%s)", async (textOnly) => {
    const user = await openChat();
    if (textOnly) await user.click(checkbox());
    await submit(user, "question with a continuation");
    // An unexpected prompt cannot use the browser's next draft to replace
    // the original server-owned restrictions.
    await finish(0, [approval()]);
    if (!textOnly) await user.click(checkbox());
    await user.click(screen.getByRole("button", { name: "Approve and continue Synthetic tool" }));
    expect(streams).toHaveLength(2);
    expect(streams[1].input).toMatchObject({
      sessionId: "A",
      content: "",
      continuation: { messageId: "assistant-0", requestId: "request-1", grant: "synthetic-grant" },
    });
    expect(streams[1].input).not.toHaveProperty("allowTools");
    expect(streams[1].input).not.toHaveProperty("allowAutomaticMemory");
    if (!textOnly) {
      expect(streams[1].input).not.toHaveProperty("allowTools");
      expect(streams[1].input).not.toHaveProperty("allowAutomaticMemory");
      expect(checkbox()).toBeChecked();
    }
    await finish(1);
    await submit(user, "a genuinely new turn");
    if (textOnly) {
      expect(streams[2].input).not.toHaveProperty("allowTools");
      expect(streams[2].input).not.toHaveProperty("allowAutomaticMemory");
    } else {
      expect(streams[2].input).toMatchObject(restricted);
    }
  });

  it("resets on conversation changes without discarding the unsent draft", async () => {
    const user = await openChat();
    await user.type(textarea(), "unsent draft");
    await user.click(checkbox());
    await user.click(screen.getByRole("button", { name: "Session B" }));
    expect(checkbox()).not.toBeChecked();
    expect(textarea()).toHaveValue("unsent draft");
    await user.click(screen.getByRole("button", { name: "Session A" }));
    expect(checkbox()).not.toBeChecked();
    await user.click(checkbox());
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(checkbox()).not.toBeChecked();
    expect(textarea()).toHaveValue("unsent draft");
    expect(mocks.updateSession).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(streams[0].input.sessionId).toBe("C");
    expect(streams[0].input).not.toHaveProperty("allowTools");
    expect(streams[0].input).not.toHaveProperty("allowAutomaticMemory");
  });

  it("keeps existing history, attachments, instructions and saved tool choices", async () => {
    mocks.listSessions.mockResolvedValue([{
      ...makeChatSession("A"),
      systemPrompt: "Keep my custom instructions.",
      toolOverrides: { added: ["calculator"], removed: [] },
    }]);
    mocks.listMessages.mockResolvedValue([{
      id: "earlier",
      sessionId: "A",
      userId: "u1",
      role: "user",
      content: "Earlier conversation context",
      status: "complete",
      model: "gpt-5.2",
      agent: null,
      createdAt: "2026-10-21T00:00:00Z",
    }]);
    mocks.listDocuments.mockResolvedValue([{
      id: "attached-document",
      filename: "existing-notes.pdf",
      size: 128,
      charCount: 64,
      truncated: false,
    }]);
    const user = await openChat();
    expect(await screen.findByText("Earlier conversation context")).toBeInTheDocument();
    expect(await screen.findByText("existing-notes.pdf")).toBeInTheDocument();
    await user.click(checkbox());
    await submit(user, "answer with the existing context");
    expect(streams[0].input).toMatchObject({ sessionId: "A", ...restricted });
    expect(screen.getByText("Earlier conversation context")).toBeInTheDocument();
    expect(screen.getByText("existing-notes.pdf")).toBeInTheDocument();
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(mocks.updateSession).not.toHaveBeenCalled();
  });

  it("captures before lazy session creation and ignores later draft toggles", async () => {
    let resolveSession!: (session: Session) => void;
    mocks.createSession.mockImplementation(() => new Promise<Session>((resolve) => {
      resolveSession = resolve;
    }));
    const user = userEvent.setup();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await screen.findByRole("button", { name: "Session A" });
    await user.click(checkbox());
    await submit(user, "first question");
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledTimes(1));
    expect(checkbox()).not.toBeChecked();
    await user.click(checkbox());
    await user.click(checkbox());
    await act(async () => resolveSession(makeChatSession("C")));
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(streams[0].input).toMatchObject({ sessionId: "C", ...restricted });
    expect(mocks.createSession.mock.calls[0][0]).not.toHaveProperty("allowTools");
    expect(mocks.createSession.mock.calls[0][0]).not.toHaveProperty("allowAutomaticMemory");
    await finish(0);
    await submit(user, "second question");
    expect(streams[1].input).not.toHaveProperty("allowTools");
    expect(streams[1].input).not.toHaveProperty("allowAutomaticMemory");
  });

  it("does not let old callbacks transplant restrictions or approvals into another conversation", async () => {
    const user = await openChat();
    await user.click(checkbox());
    await submit(user, "restricted turn");
    await finish(0, [approval()]);
    await user.click(screen.getByRole("button", { name: "Session B" }));
    act(() => streams[0].handlers.onApprovals?.([approval("late")]));
    expect(screen.queryByRole("region", { name: "Tool approvals" })).not.toBeInTheDocument();
    expect(checkbox()).not.toBeChecked();
    await submit(user, "unrestricted new turn");
    expect(streams[1].input.sessionId).toBe("B");
    expect(streams[1].input).not.toHaveProperty("allowTools");
    expect(streams[1].input).not.toHaveProperty("allowAutomaticMemory");
  });

  it("shows required-source rejection without rewriting it or retrying with tools", async () => {
    const user = await openChat();
    await user.click(checkbox());
    await submit(user, "@required-source keep these instructions");
    act(() => streams[0].handlers.onError("422: This source requires tools.", {
      accepted: false,
      persistenceFailed: false,
      definitePreAcceptance: true,
    }));
    expect(await screen.findByText("422: This source requires tools.")).toBeInTheDocument();
    expect(streams).toHaveLength(1);
    expect(streams[0].input).toMatchObject({
      content: "@required-source keep these instructions",
      ...restricted,
    });
    expect(mocks.updateSession).not.toHaveBeenCalled();
    expect(checkbox()).not.toBeChecked();
  });
});
