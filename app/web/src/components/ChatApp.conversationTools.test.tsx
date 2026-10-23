// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StreamHandlers } from "@/lib/api";
import type { Message, PendingToolApprovalPrompt, Session, ToolCatalogItem } from "@/lib/types";
import { ChatApp } from "./ChatApp";
import { MemoryPreferenceProvider, type MemoryPreferenceOwner } from "./MemoryPreferenceProvider";
import { makeChatSession, makeInspectorSnapshot, resetChatAppMocks } from "./chatTestFixtures";

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(), listSessions: vi.fn(), listAgents: vi.fn(),
  getAttachmentCapabilities: vi.fn(), listMessages: vi.fn(), listDocuments: vi.fn(),
  listLibraryDocuments: vi.fn(), listSharedWithMe: vi.fn(), createSession: vi.fn(),
  streamChat: vi.fn(), toolCatalog: [] as ToolCatalogItem[], getToolCatalog: vi.fn(),
  updateSession: vi.fn(), getInspector: vi.fn(), listMemories: vi.fn(),
  getLibrarySummary: vi.fn(), createMemory: vi.fn(), updateMemory: vi.fn(),
  deleteMemory: vi.fn(), appendVoiceTurns: vi.fn(), getImageOptions: vi.fn(),
  apiErrorDetail: (reason: unknown) => reason instanceof Error ? reason.message : "Save failed",
}));
vi.mock("@/lib/api", () => mocks);
vi.mock("@/lib/inspector", () => ({
  getInspector: mocks.getInspector, listMemories: mocks.listMemories,
  getLibrarySummary: mocks.getLibrarySummary, createMemory: mocks.createMemory,
  updateMemory: mocks.updateMemory, deleteMemory: mocks.deleteMemory,
}));
vi.mock("./VoiceLiveProvider", () => ({
  useVoiceLiveConfig: () => ({ enabled: false, toolsAvailable: false }),
}));
vi.mock("./LibraryProvider", () => ({ useLibraryConfig: () => ({ enabled: true }) }));
vi.mock("./CustomToolsProvider", () => ({ useCustomToolsConfig: () => ({ enabled: false }) }));
vi.mock("./AdminLink", () => ({ AdminLink: () => null }));
vi.mock("./UserMenu", () => ({ UserMenu: () => null }));
vi.mock("./MessageList", () => ({
  MessageList: ({ messages }: { messages: Message[] }) => (
    <div aria-label="Conversation">{messages.map((message) => <p key={message.id}>{message.content}</p>)}</div>
  ),
}));
vi.mock("./InlineVoiceLive", () => ({
  InlineVoiceLiveStatus: () => null,
  mergeDisplayMessages: (messages: unknown[]) => messages,
  voiceMessagesForSession: () => [],
  useInlineVoiceLive: () => ({
    active: false, supported: false, phase: "idle", saving: false,
    persistenceError: null, error: null, start: vi.fn(), stop: vi.fn(),
    exitLocked: false, messages: [], boundSessionId: null,
  }),
}));

type ChatInput = Parameters<typeof import("@/lib/api").streamChat>[0];
const streams: { input: ChatInput; handlers: StreamHandlers }[] = [];
const sessions = new Map<string, Session>();
const master = () => screen.getByRole("switch", { name: "Conversation tools" });
const textarea = () => screen.getByRole("combobox", { name: "Message" });

async function openTools(user: ReturnType<typeof userEvent.setup>) {
  const opener = screen.queryByRole("button", { name: "Open conversation inspector" });
  if (opener) await user.click(opener);
  const section = screen.getByRole("button", { name: "Agent & tools" });
  if (section.getAttribute("aria-expanded") !== "true") await user.click(section);
  await waitFor(() => expect(master()).toBeEnabled());
}

async function submit(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(textarea(), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

async function finish(index: number, prompts: PendingToolApprovalPrompt[] = []) {
  const { input, handlers } = streams[index];
  const messages: Message[] = [{
    id: `assistant-${index}`, sessionId: input.sessionId, userId: "u1",
    role: "assistant", content: "Synthetic answer", status: "complete",
    model: input.model ?? null, agent: null, createdAt: "2026-10-22T00:00:00Z",
  }];
  mocks.listMessages.mockResolvedValue(messages);
  act(() => {
    handlers.onMetadata({ userMessageId: `user-${index}`, assistantMessageId: messages[0].id });
    handlers.onDelta("Synthetic answer");
    if (prompts.length) handlers.onApprovals?.(prompts);
    handlers.onDone();
  });
  await waitFor(() => expect(screen.queryByRole("button", { name: "Stop" })).not.toBeInTheDocument());
}

beforeEach(() => {
  vi.resetAllMocks();
  resetChatAppMocks(mocks);
  sessions.clear();
  for (const id of ["A", "B", "C"]) sessions.set(id, makeChatSession(id));
  mocks.listSessions.mockImplementation(async () => [sessions.get("A"), sessions.get("B")]);
  mocks.updateSession.mockImplementation(async (id: string, patch: object) => {
    const updated = { ...sessions.get(id)!, ...patch };
    sessions.set(id, updated);
    return updated;
  });
  mocks.getInspector.mockImplementation(async (id: string) => {
    const result = makeInspectorSnapshot(id);
    result.tools.enabled = sessions.get(id)?.toolsEnabled !== false;
    return result;
  });
  mocks.getImageOptions.mockResolvedValue({
    maxSelectedModels: 3, currency: "USD", priceVersion: null, models: [],
  });
  streams.length = 0;
  mocks.streamChat.mockImplementation((input: ChatInput, handlers: StreamHandlers) => {
    streams.push({ input, handlers });
    return vi.fn();
  });
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe("ChatApp conversation tools", () => {
  it("does not carry draft tool denials across owners", async () => {
    let key = "first-owner";
    let notify = () => {};
    const owner: MemoryPreferenceOwner = {
      getSnapshot: () => key,
      subscribe: (callback) => { notify = callback; return () => {}; },
    };
    const user = userEvent.setup();
    render(<MemoryPreferenceProvider owner={owner}><ChatApp /></MemoryPreferenceProvider>);
    await screen.findByRole("button", { name: "Session A" });
    await openTools(user);
    await user.click(master());
    expect(master()).not.toBeChecked();
    expect(screen.getByText("Chat tools off")).toBeVisible();
    act(() => { key = "second-owner"; notify(); });
    await openTools(user);
    expect(master()).toBeChecked();
    expect(screen.queryByText("Chat tools off")).not.toBeInTheDocument();
    expect(mocks.updateSession).not.toHaveBeenCalled();
  });

  it("persists draft tools-off during lazy creation without changing memory or resetting after a turn", async () => {
    const user = userEvent.setup();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await screen.findByRole("button", { name: "Session A" });
    await openTools(user);
    expect(master()).toBeChecked();
    await user.click(master());
    await submit(user, "First synthetic question");
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(mocks.createSession).toHaveBeenCalledWith(expect.objectContaining({ toolsEnabled: false }), expect.anything());
    expect(streams[0].input).not.toHaveProperty("allowAutomaticMemory");
    expect(streams[0].input).not.toHaveProperty("allowTools");
    sessions.set("C", { ...makeChatSession("C"), toolsEnabled: false });
    await finish(0);
    await openTools(user);
    expect(master()).not.toBeChecked();
    await submit(user, "Second synthetic question");
    expect(streams).toHaveLength(2);
    expect(mocks.createSession).toHaveBeenCalledTimes(1);
    expect(mocks.createMemory).not.toHaveBeenCalled();
    expect(mocks.updateMemory).not.toHaveBeenCalled();
    expect(mocks.deleteMemory).not.toHaveBeenCalled();
    expect(screen.queryByRole("checkbox", { name: "Text only" })).not.toBeInTheDocument();
  });

  it("restores each conversation and a reload while a new draft defaults to tools on", async () => {
    const user = userEvent.setup();
    const rendered = render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await openTools(user);
    await user.click(master());
    await waitFor(() => expect(master()).toBeEnabled());
    expect(master()).not.toBeChecked();
    expect(mocks.updateSession).toHaveBeenLastCalledWith("A", { toolsEnabled: false });
    await user.type(textarea(), "Keep my unsent text");
    await user.click(screen.getByRole("button", { name: "Session B" }));
    await openTools(user);
    expect(master()).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Session A" }));
    await openTools(user);
    expect(master()).not.toBeChecked();
    expect(textarea()).toHaveValue("Keep my unsent text");
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await openTools(user);
    expect(master()).toBeChecked();
    rendered.unmount();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await openTools(user);
    expect(master()).not.toBeChecked();
  });

  it("does not send with unconfirmed settings and ignores another session's late save", async () => {
    let resolve!: (value: Session) => void;
    mocks.updateSession.mockImplementation(() => new Promise<Session>((done) => { resolve = done; }));
    const user = userEvent.setup();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await openTools(user);
    await user.type(textarea(), "Synthetic");
    await user.click(master());
    expect(master()).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    await user.click(textarea());
    await user.keyboard("{Enter}");
    expect(streams).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Session B" }));
    await openTools(user);
    await act(async () => resolve({ ...makeChatSession("A"), toolsEnabled: false }));
    expect(master()).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(streams[0].input.sessionId).toBe("B");
  });

  it("continues only the original call identifiers after a conversation-settings change", async () => {
    const user = userEvent.setup();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    await submit(user, "Synthetic original turn");
    await finish(0, [{
      id: "request-1", messageId: "assistant-0", operationId: "0123456789abcdef:1:0",
      tool: "synthetic_tool", label: "Synthetic tool", host: null, purpose: "Synthetic",
      risk: "external", argumentsDigest: "d".repeat(64), argumentsPreview: {},
      grantHash: "h".repeat(64), expiresAt: "2099-01-01T00:00:00Z",
      createdAt: "2026-10-22T00:00:00Z", grant: "synthetic-grant",
    }]);
    await openTools(user);
    await user.click(master());
    await waitFor(() => expect(master()).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Approve and continue Synthetic tool" }));
    expect(streams[1].input).toMatchObject({
      sessionId: "A", content: "",
      continuation: { messageId: "assistant-0", requestId: "request-1", grant: "synthetic-grant" },
    });
    expect(streams[1].input).not.toHaveProperty("toolsEnabled");
    expect(streams[1].input).not.toHaveProperty("allowTools");
    expect(streams[1].input).not.toHaveProperty("allowAutomaticMemory");
  });
});
