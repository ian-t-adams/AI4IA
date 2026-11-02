// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AgentSummary, ToolCatalogItem, UserAgent } from "@/lib/types";
import { ChatApp } from "./ChatApp";
import { MemoryPreferenceProvider, type MemoryPreferenceOwner } from "./MemoryPreferenceProvider";
import { resetChatAppMocks } from "./chatTestFixtures";

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(), listSessions: vi.fn(), listAgents: vi.fn(),
  getAttachmentCapabilities: vi.fn(), listMessages: vi.fn(), listDocuments: vi.fn(),
  listLibraryDocuments: vi.fn(), listSharedWithMe: vi.fn(), createSession: vi.fn(),
  streamChat: vi.fn(), toolCatalog: [] as ToolCatalogItem[], getToolCatalog: vi.fn(),
  updateSession: vi.fn(), getInspector: vi.fn(), listMemories: vi.fn(),
  getLibrarySummary: vi.fn(), createMemory: vi.fn(), updateMemory: vi.fn(),
  deleteMemory: vi.fn(), appendVoiceTurns: vi.fn(), getImageOptions: vi.fn(),
  listMyAgents: vi.fn(), listOfficialMcpServers: vi.fn(), createAgent: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(), ...mocks,
}));
vi.mock("@/lib/inspector", () => ({
  getInspector: mocks.getInspector, listMemories: mocks.listMemories,
  getLibrarySummary: mocks.getLibrarySummary, createMemory: mocks.createMemory,
  updateMemory: mocks.updateMemory, deleteMemory: mocks.deleteMemory,
}));
vi.mock("./VoiceLiveProvider", () => ({
  useVoiceLiveConfig: () => ({ enabled: false, toolsAvailable: false }),
}));
vi.mock("./LibraryProvider", () => ({ useLibraryConfig: () => ({ enabled: false }) }));
vi.mock("./CustomToolsProvider", () => ({ useCustomToolsConfig: () => ({ enabled: false }) }));
vi.mock("./AdminLink", () => ({ AdminLink: () => null }));
vi.mock("./UserMenu", () => ({ UserMenu: () => null }));
vi.mock("./MessageList", () => ({ MessageList: () => null }));
vi.mock("./PublicationControls", () => ({ PublicationControls: () => null }));
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

function agent(owner: string, name: string, displayName: string): UserAgent {
  return {
    id: name, userId: owner, name, displayName, description: "", revision: 1,
    incarnation: "a".repeat(32), systemPrompt: `${displayName} synthetic instructions`,
    defaultModel: null, tools: [], links: [], enabled: true, createdAt: "", updatedAt: "",
  };
}
const ownerAAgent = agent("owner-a", "a-helper", "Owner A helper");
const ownerBAgent = agent("owner-b", "b-helper", "Owner B helper");
const summary = (a: UserAgent): AgentSummary => ({
  name: a.name, displayName: a.displayName, description: a.description, enabled: true,
});

let key: string | null = "owner-a";
let notify = () => {};
const owner: MemoryPreferenceOwner = {
  getSnapshot: () => key,
  subscribe: (callback) => { notify = callback; return () => {}; },
};
function switchTo(next: string | null) {
  act(() => { key = next; notify(); });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

beforeEach(() => {
  resetChatAppMocks(mocks);
  key = "owner-a";
  mocks.getImageOptions.mockResolvedValue({
    maxSelectedModels: 3, currency: "USD", priceVersion: null, models: [],
  });
  mocks.listOfficialMcpServers.mockResolvedValue([]);
  mocks.listMyAgents.mockImplementation(async () =>
    key === "owner-a" ? [ownerAAgent] : key === "owner-b" ? [ownerBAgent] : []);
  mocks.listAgents.mockImplementation(async () =>
    key === "owner-a" ? [summary(ownerAAgent)] : key === "owner-b" ? [summary(ownerBAgent)] : []);
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

async function openStudio(user: ReturnType<typeof userEvent.setup>) {
  render(<MemoryPreferenceProvider owner={owner}><ChatApp /></MemoryPreferenceProvider>);
  await user.click(await screen.findByRole("button", { name: /Agents & workflows/ }));
}

describe("agents follow the signed-in account", () => {
  it("replaces the previous owner's saved agents, selection and Try with the new owner's", async () => {
    const user = userEvent.setup();
    await openStudio(user);
    await user.click(await screen.findByRole("button", { name: "Owner A helper" }));
    expect(screen.getByRole("button", { name: "Try it in chat" })).toBeEnabled();
    const listCalls = mocks.listMyAgents.mock.calls.length;
    const catalogCalls = mocks.listAgents.mock.calls.length;

    switchTo("owner-b");
    expect(screen.queryByRole("button", { name: "Owner A helper" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Try it in chat" })).toBeNull();
    expect(screen.getByLabelText("What should this agent do?")).toHaveValue("");
    expect(await screen.findByRole("button", { name: "Owner B helper" })).toBeVisible();
    expect(mocks.listMyAgents.mock.calls.length).toBeGreaterThan(listCalls);
    expect(mocks.listAgents.mock.calls.length).toBeGreaterThan(catalogCalls);
    expect(screen.queryByRole("button", { name: "Owner A helper" })).toBeNull();
  });

  it("shows nothing while signed out and reloads the same owner's agents on return", async () => {
    const user = userEvent.setup();
    await openStudio(user);
    await user.click(await screen.findByRole("button", { name: "Owner A helper" }));

    mocks.listMyAgents.mockClear();
    mocks.listAgents.mockClear();
    switchTo(null);
    expect(screen.queryByRole("button", { name: "Owner A helper" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Try it in chat" })).toBeNull();
    expect(await screen.findByText("Sign in to load your agents.")).toBeVisible();
    expect(mocks.listMyAgents).not.toHaveBeenCalled();
    expect(mocks.listAgents).not.toHaveBeenCalled();

    switchTo("owner-a");
    expect(await screen.findByRole("button", { name: "Owner A helper" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Owner B helper" })).toBeNull();
    expect(mocks.listMyAgents).toHaveBeenCalled();
    expect(mocks.listAgents).toHaveBeenCalled();
  });

  it("ignores a late saved-agent list from the previous owner", async () => {
    const late = deferred<UserAgent[]>();
    mocks.listMyAgents.mockImplementationOnce(() => late.promise);
    const user = userEvent.setup();
    await openStudio(user);
    expect(screen.getByText("Loading your agents...")).toBeVisible();

    switchTo("owner-b");
    expect(await screen.findByRole("button", { name: "Owner B helper" })).toBeVisible();
    await act(async () => { late.resolve([ownerAAgent]); await late.promise; });
    expect(screen.queryByRole("button", { name: "Owner A helper" })).toBeNull();
    expect(screen.getByRole("button", { name: "Owner B helper" })).toBeVisible();
  });

  it("ignores a late save from the previous owner and does not refresh the new owner's catalog for it", async () => {
    mocks.listMyAgents.mockImplementation(async () => (key === "owner-b" ? [ownerBAgent] : []));
    const late = deferred<UserAgent>();
    mocks.createAgent.mockImplementationOnce(() => late.promise);
    const user = userEvent.setup();
    await openStudio(user);
    await user.type(await screen.findByLabelText("What should this agent do?"), "Draft for A");
    await user.type(screen.getByLabelText("Agent name"), "Owner A helper");
    await user.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => expect(mocks.createAgent).toHaveBeenCalledTimes(1));

    switchTo("owner-b");
    expect(await screen.findByRole("button", { name: "Owner B helper" })).toBeVisible();
    const listCalls = mocks.listMyAgents.mock.calls.length;
    const catalogCalls = mocks.listAgents.mock.calls.length;
    await act(async () => { late.resolve(ownerAAgent); await late.promise; });
    expect(screen.queryByText(/Saved\. Use @a-helper/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Try it in chat" })).toBeNull();
    expect(screen.getByLabelText("What should this agent do?")).toHaveValue("");
    expect(screen.getByRole("button", { name: "Create agent" })).toBeEnabled();
    expect(mocks.listMyAgents.mock.calls.length).toBe(listCalls);
    expect(mocks.listAgents.mock.calls.length).toBe(catalogCalls);
  });

  it("clears an unsaved draft visibly instead of carrying it to the next account", async () => {
    const user = userEvent.setup();
    await openStudio(user);
    await screen.findByRole("button", { name: "Owner A helper" });
    await user.type(screen.getByLabelText("What should this agent do?"), "Owner A private notes");
    expect(screen.getByText("Unsaved changes")).toBeVisible();

    switchTo("owner-b");
    expect(screen.getByLabelText("What should this agent do?")).toHaveValue("");
    expect(screen.getByText("Unsaved agent changes were cleared because the signed-in account changed.")).toBeVisible();
    expect(screen.queryByText("Owner A private notes")).toBeNull();
  });

  it("tries the new owner's agent in the current chat draft without sending", async () => {
    const user = userEvent.setup();
    render(<MemoryPreferenceProvider owner={owner}><ChatApp /></MemoryPreferenceProvider>);
    await screen.findByRole("button", { name: "Session A" });
    switchTo("owner-b");
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    const composer = await screen.findByRole("combobox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "Summarize these notes");
    await user.click(screen.getByRole("button", { name: /Agents & workflows/ }));
    await user.click(await screen.findByRole("button", { name: "Owner B helper" }));
    await user.click(screen.getByRole("button", { name: "Try it in chat" }));
    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(composer).toHaveValue("@b-helper Summarize these notes");
    expect(mocks.streamChat).not.toHaveBeenCalled();
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(mocks.updateSession).not.toHaveBeenCalled();
  });

  it("offers only the current owner's agents in the @-mention menu", async () => {
    const late = deferred<AgentSummary[]>();
    mocks.listAgents.mockImplementationOnce(() => late.promise);
    const user = userEvent.setup();
    render(<MemoryPreferenceProvider owner={owner}><ChatApp /></MemoryPreferenceProvider>);
    await screen.findByRole("button", { name: "Session A" });
    switchTo("owner-b");
    await act(async () => { late.resolve([summary(ownerAAgent)]); await late.promise; });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    const composer = await screen.findByRole("combobox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "@");
    expect(await screen.findByRole("option", { name: /@b-helper/ })).toBeVisible();
    expect(screen.queryByRole("option", { name: /@a-helper/ })).toBeNull();
  });
});
