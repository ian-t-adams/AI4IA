// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { StreamHandlers } from "@/lib/api";
import type { Message, PendingToolApprovalPrompt, Session, ToolCatalogItem } from "@/lib/types";
import { ChatApp } from "./ChatApp";
import { MemoryPreferenceProvider, type MemoryPreferenceOwner } from "./MemoryPreferenceProvider";
import {
  CHAT_MODEL_CATALOG,
  makeChatSession,
  makeInspectorSnapshot,
  resetChatAppMocks,
} from "./chatTestFixtures";

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(), listSessions: vi.fn(), listAgents: vi.fn(),
  getAttachmentCapabilities: vi.fn(), listMessages: vi.fn(), listDocuments: vi.fn(),
  listLibraryDocuments: vi.fn(), listSharedWithMe: vi.fn(), createSession: vi.fn(),
  streamChat: vi.fn(), toolCatalog: [] as ToolCatalogItem[], getToolCatalog: vi.fn(),
  updateSession: vi.fn(), getInspector: vi.fn(), listMemories: vi.fn(),
  getLibrarySummary: vi.fn(), createMemory: vi.fn(), updateMemory: vi.fn(),
  deleteMemory: vi.fn(), appendVoiceTurns: vi.fn(), getImageOptions: vi.fn(),
  messageListProps: vi.fn<(props: Parameters<typeof import("./MessageList").MessageList>[0]) => void>(),
  reviewToolApproval: vi.fn(), denyToolApproval: vi.fn(),
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
  MessageList: (props: Parameters<typeof import("./MessageList").MessageList>[0]) => {
    mocks.messageListProps(props);
    return <div aria-label="Conversation">{props.messages.map((message) => <p key={message.id}>{message.content}</p>)}</div>;
  },
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
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("ChatApp conversation tools", () => {
  it.each([true, false])(
    "preserves stored consent and pending history with usable actions only outside pilot (pilot=%s)",
    async (pilot) => {
      const consent = {
        id: "stored-consent", scope: "session" as const, toolCount: 1,
        grantedAt: "2026-10-22T00:00:00Z", expiresAt: "2099-01-01T00:00:00Z",
      };
      sessions.set("A", { ...makeChatSession("A"), toolConsent: consent });
      mocks.getInspector.mockResolvedValue({
        ...makeInspectorSnapshot("A"),
        toolConsent: consent, toolAutoApproveAvailable: true,
        toolConsentActive: true, toolConsentStatus: "active",
      });
      const pending = {
        id: "stored-request", messageId: "stored-answer", operationId: "0123456789abcdef:1:0",
        tool: "synthetic_tool", label: "Stored synthetic tool", host: null,
        argumentsDigest: "d".repeat(64), argumentsPreview: {}, grantHash: "h".repeat(64),
        createdAt: "2026-10-22T00:00:00Z", expiresAt: "2099-01-01T00:00:00Z",
      };
      const messages: Message[] = [{
        id: "stored-answer", sessionId: "A", userId: "u1", role: "assistant",
        content: "Retained pending answer", status: "complete", model: "gpt-5.2",
        agent: null, createdAt: "2026-10-22T00:00:00Z",
        continuationStatus: "waiting", pendingApprovals: [pending],
      }];
      const saved = JSON.stringify({ session: sessions.get("A"), messages });
      mocks.listMessages.mockResolvedValue(messages);
      mocks.reviewToolApproval.mockResolvedValue({ ...pending, grant: "synthetic-grant" });
      const user = userEvent.setup();
      render(<ChatApp chatPilotCompatibilityEnabled={pilot} />, { wrapper: MemoryPreferenceProvider });
      await user.click(await screen.findByRole("button", { name: "Session A" }));
      expect(await screen.findByText("Retained pending answer")).toBeVisible();
      const props = mocks.messageListProps.mock.lastCall?.[0];
      if (!props) throw new Error("Expected the selected conversation to render.");
      expect(props.textOnly).toBe(pilot);

      if (pilot) {
        expect(props.onCitation).toBeUndefined();
        expect(props.onInspectMemory).toBeUndefined();
        expect(screen.getByLabelText("Stored session tool auto-approval")).toHaveTextContent("Stored tool consent is unchanged");
        expect(screen.getByLabelText("Stored pending tool approvals")).toHaveTextContent("Stored synthetic tool");
        expect(screen.queryByRole("button", { name: "Revoke auto-approval" })).toBeNull();
        expect(screen.queryByRole("button", { name: "Refresh consent status" })).toBeNull();
        expect(screen.queryByRole("region", { name: "Tool approvals" })).toBeNull();
        expect(screen.queryByRole("button", { name: /Review pending call/ })).toBeNull();
        expect(screen.queryByRole("button", { name: /Approve and continue/ })).toBeNull();
        expect(screen.queryByRole("button", { name: /Deny Stored/ })).toBeNull();
      } else {
        expect(props.onCitation).toBeTypeOf("function");
        expect(props.onInspectMemory).toBeTypeOf("function");
        expect(screen.getByRole("button", { name: "Revoke auto-approval" })).toBeVisible();
        expect(screen.getByRole("button", { name: "Deny Stored synthetic tool" })).toBeVisible();
        await user.click(screen.getByRole("button", { name: "Review pending call Stored synthetic tool" }));
        await waitFor(() => expect(mocks.reviewToolApproval).toHaveBeenCalledWith(
          "A", expect.objectContaining({ id: "stored-request", messageId: "stored-answer" }),
        ));
      }
      if (pilot) expect(mocks.reviewToolApproval).not.toHaveBeenCalled();
      expect(mocks.denyToolApproval).not.toHaveBeenCalled();
      expect(mocks.updateSession).not.toHaveBeenCalled();
      expect(mocks.streamChat).not.toHaveBeenCalled();
      expect(JSON.stringify({ session: sessions.get("A"), messages })).toBe(saved);
    },
  );

  it("keeps the normal draft unchanged when pilot compatibility is off", async () => {
    const user = userEvent.setup();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await screen.findByRole("button", { name: "Session A" });
    await user.click(screen.getByRole("button", { name: "New chat" }));
    await openTools(user);
    expect(master()).toBeChecked();
    await submit(user, "Ordinary chat");
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(mocks.createSession).toHaveBeenCalledWith(
      expect.not.objectContaining({ toolsEnabled: false }),
      expect.anything(),
    );
    expect(streams[0].input).not.toHaveProperty("allowTools");
    expect(streams[0].input).not.toHaveProperty("allowAutomaticMemory");
    expect(streams[0].input.params?.max_tokens).toBe(1024);
    expect(mocks.streamChat.mock.calls[0]).toHaveLength(2);
  });

  it("creates a tools-off session and explicitly denies tools and automatic memory in pilot mode", async () => {
    const user = userEvent.setup();
    render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
    await screen.findByRole("button", { name: "Session A" });
    await user.click(screen.getByRole("button", { name: "New chat" }));

    expect(screen.getByText(/Isolated chat-pilot compatibility is enabled/)).toHaveTextContent(
      /fixed 256 output-token budget/,
    );
    expect(screen.getByText(/Isolated chat-pilot compatibility is enabled/)).toHaveTextContent(
      /Reasoning tokens count against that budget and may leave a short or empty visible reply/,
    );
    expect(screen.queryByRole("button", { name: "Open conversation inspector" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Attach files" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Rename" })).not.toBeInTheDocument();

    await submit(user, "/help");
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(streams).toHaveLength(0);

    await user.clear(textarea());
    await submit(user, "Synthetic plain text");
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(mocks.createSession).toHaveBeenCalledWith(
      expect.objectContaining({
        toolsEnabled: false,
        agentName: null,
        libraryDocumentIds: [],
      }),
      expect.anything(),
    );
    expect(streams[0].input).toMatchObject({
      allowTools: false,
      allowAutomaticMemory: false,
      params: { max_tokens: 256 },
    });
    expect(streams[0].input).not.toHaveProperty("requireFreshSession");
    expect(mocks.streamChat.mock.calls[0]).toHaveLength(3);
    expect(mocks.streamChat.mock.calls[0][2]).toEqual({ rejectRedirects: true });
  });

  it("shows a pilot session's real title read-only while normal mode retains rename", async () => {
    const user = userEvent.setup();
    const rendered = render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    const main = screen.getByRole("main");
    expect(within(main).getByText("Session A")).toBeVisible();
    expect(within(main).queryByRole("button", { name: "Rename" })).toBeNull();
    rendered.unmount();

    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    const normalMain = screen.getByRole("main");
    await user.click(within(normalMain).getByRole("button", { name: "Rename" }));
    const title = screen.getByRole("textbox", { name: "Conversation title" });
    await user.clear(title);
    await user.type(title, "Renamed session");
    await user.keyboard("{Enter}");
    await waitFor(() =>
      expect(mocks.updateSession).toHaveBeenLastCalledWith("A", { title: "Renamed session" }),
    );
  });

  it("lets a new pilot chat choose only conversational models and uses that choice without PATCH", async () => {
    const [chatModel] = CHAT_MODEL_CATALOG.models;
    if (!chatModel) throw new Error("Expected the conversational chat fixture.");
    const selectedModel = { ...chatModel, id: "gpt-5.3", displayName: "GPT-5.3" };
    const capabilityModel = {
      ...chatModel,
      id: "image-model",
      displayName: "Image model",
      category: "image",
      conversational: false,
    };
    mocks.listModels.mockResolvedValue({
      ...CHAT_MODEL_CATALOG,
      models: [chatModel, selectedModel, capabilityModel],
    });
    let resolveCreate!: (session: Session) => void;
    mocks.createSession.mockReturnValue(
      new Promise<Session>((resolve) => { resolveCreate = resolve; }),
    );
    const user = userEvent.setup();
    render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "New chat" }));
    const picker = screen.getByRole("combobox", { name: "Model for new conversation" });
    expect(within(picker).queryByRole("option", { name: "Image model" })).toBeNull();
    await user.selectOptions(picker, "gpt-5.3");

    await submit(user, "Use the selected model");
    await waitFor(() => expect(mocks.createSession).toHaveBeenCalledOnce());
    expect(picker).toBeDisabled();
    expect(mocks.createSession).toHaveBeenCalledWith(
      expect.objectContaining({ model: "gpt-5.3" }),
      expect.anything(),
    );

    await act(async () => {
      resolveCreate({ ...makeChatSession("C"), model: "gpt-5.3" });
    });
    await waitFor(() => expect(streams).toHaveLength(1));
    expect(screen.getByText("Model: GPT-5.3")).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Model for new conversation" })).toBeNull();
    expect(streams[0].input).toMatchObject({
      model: "gpt-5.3",
      content: "Use the selected model",
    });
    expect(mocks.updateSession).not.toHaveBeenCalled();

    await finish(0);
    expect(screen.getByText("Model: GPT-5.3")).toBeVisible();
    expect(screen.queryByRole("combobox", { name: "Model for new conversation" })).toBeNull();
    expect(mocks.updateSession).not.toHaveBeenCalled();
  });

  it.each(["/tmp/log.txt", "/", "@", "@42", "@café", "@agenté hello"])(
    "allows ordinary pilot text beginning with %s",
    async (text) => {
      const user = userEvent.setup();
      render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
      await user.click(await screen.findByRole("button", { name: "New chat" }));
      await submit(user, text);
      await waitFor(() => expect(streams).toHaveLength(1));
      expect(streams[0].input.content).toBe(text);
    },
  );

  it("does not refresh command-controlled state for ordinary slash-prefixed pilot text", async () => {
    const [chatModel] = CHAT_MODEL_CATALOG.models;
    if (!chatModel) throw new Error("Expected the conversational chat fixture.");
    mocks.listModels.mockResolvedValue({
      ...CHAT_MODEL_CATALOG,
      models: [
        chatModel,
        { ...chatModel, id: "gpt-5.3", displayName: "GPT-5.3" },
      ],
    });
    const user = userEvent.setup();
    render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "New chat" }));
    await submit(user, "/tmp/log.txt");
    await waitFor(() => expect(streams).toHaveLength(1));
    mocks.listSessions.mockResolvedValue([
      { ...makeChatSession("C"), model: "gpt-5.3" },
    ]);

    await finish(0);
    expect(screen.getByText("Model: GPT-5.2")).toBeVisible();
  });

  it.each([
    "/help",
    "/foo bar",
    "@agent hello",
    "@agent/hello",
    "@agent-",
    "@agent.",
    "@a-bé",
  ])(
    "continues to refuse recognized pilot command input %s",
    async (text) => {
      const user = userEvent.setup();
      render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
      await user.click(await screen.findByRole("button", { name: "New chat" }));
      await submit(user, text);
      expect(mocks.createSession).not.toHaveBeenCalled();
      expect(streams).toHaveLength(0);
      expect(screen.getByRole("alert")).toHaveTextContent(
        "Agent mentions and slash commands are unavailable in text-only pilot mode.",
      );
    },
  );

  it.each(["#/library", "#/studio", "#/settings"])(
    "keeps the pilot chat visible for the initial %s hash",
    async (hash) => {
      window.history.replaceState(null, "", `/${hash}`);
      render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
      await screen.findByRole("button", { name: "Session A" });
      expect(textarea()).toBeVisible();
      expect(window.location.hash).toBe(hash);
    },
  );

  it("keeps pilot chat visible through later hashchange and popstate events without adding history", async () => {
    window.history.replaceState(null, "", "/#/chat");
    render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
    await screen.findByRole("button", { name: "Session A" });
    const historyLength = window.history.length;

    act(() => {
      window.history.replaceState(null, "", "/#/library");
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(textarea()).toBeVisible();
    act(() => {
      window.history.replaceState(null, "", "/#/settings");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(textarea()).toBeVisible();
    expect(window.history.length).toBe(historyLength);
  });

  it("keeps a phone-sized pilot chat and its sidebar control reachable", async () => {
    vi.stubGlobal("matchMedia", (query: string) => ({
      matches: [
        "(max-width: 720px)",
        "(max-width: 1099px)",
        "(max-width: 1439px)",
      ].includes(query),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    window.history.replaceState(null, "", "/#/library");
    const user = userEvent.setup();
    render(<ChatApp chatPilotCompatibilityEnabled />, { wrapper: MemoryPreferenceProvider });
    await screen.findByRole("button", { name: "Open conversation sidebar" });
    expect(textarea()).toBeVisible();
    const opener = screen.getByRole("button", { name: "Open conversation sidebar" });
    await user.click(opener);
    expect(screen.getByRole("button", { name: "Collapse sidebar" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(textarea()).toBeVisible();
  });

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
