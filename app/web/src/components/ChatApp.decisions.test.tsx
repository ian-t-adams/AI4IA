// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ToolCatalogItem } from "@/lib/types";
import { ChatApp } from "./ChatApp";
import { MemoryPreferenceProvider } from "./MemoryPreferenceProvider";
import { resetChatAppMocks } from "./chatTestFixtures";

const mocks = vi.hoisted(() => ({
  listModels: vi.fn(), listSessions: vi.fn(), listAgents: vi.fn(),
  getAttachmentCapabilities: vi.fn(), listMessages: vi.fn(), listDocuments: vi.fn(),
  listLibraryDocuments: vi.fn(), listSharedWithMe: vi.fn(), createSession: vi.fn(),
  streamChat: vi.fn(), appendVoiceTurns: vi.fn(), getToolCatalog: vi.fn(),
  toolCatalog: [] as ToolCatalogItem[], updateSession: vi.fn(), getInspector: vi.fn(),
  listMemories: vi.fn(), getLibrarySummary: vi.fn(), createMemory: vi.fn(),
  updateMemory: vi.fn(), deleteMemory: vi.fn(), apiFetch: vi.fn(),
  getImageOptions: vi.fn(async () => ({ models: [] })),
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/api")>()), ...mocks,
}));
vi.mock("@/lib/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth")>()), apiFetch: mocks.apiFetch,
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
vi.mock("./MessageList", () => ({ MessageList: () => <div>Mounted conversation</div> }));
vi.mock("./InlineVoiceLive", () => ({
  InlineVoiceLiveStatus: () => null,
  mergeDisplayMessages: (messages: unknown[]) => messages,
  voiceMessagesForSession: () => [],
  useInlineVoiceLive: () => ({
    active: false, supported: false, phase: "idle", saving: false,
    persistenceError: null, error: null, start: vi.fn(), stop: vi.fn(),
    exitLocked: false, messages: [], boundSessionId: null, avatar: null,
  }),
}));

const offered = [{ id: "decision", displayName: "Decision", residencies: ["us"] }];
let phone = false;
beforeEach(() => {
  resetChatAppMocks(mocks);
  phone = false;
  vi.stubGlobal("matchMedia", vi.fn((query: string) => ({
    matches: phone && (query.includes("720px") || query.includes("1099px")),
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
  })));
  mocks.apiFetch.mockImplementation(async (path) => Response.json(
    path === "/api/decisions/options" ? { models: offered } : { enabled: false },
  ));
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});
function mount() {
  render(<MemoryPreferenceProvider><ChatApp /></MemoryPreferenceProvider>);
}

describe("Decisions workspace", () => {
  it("opens the playground as a page, follows history, and retains the conversation", async () => {
    const user = userEvent.setup();
    mount();
    await user.click(await screen.findByRole("button", { name: "Decisions" }));
    expect(location.hash).toBe("#/decisions");
    const heading = screen.getByRole("heading", { level: 1, name: "Decisions" });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(screen.getByRole("region", { name: "Decision playground" })).toBeVisible();
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("Mounted conversation")).not.toBeVisible();
    await act(async () => {
      window.history.replaceState(null, "", "/");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.getByText("Mounted conversation")).toBeVisible();
    await act(async () => {
      window.history.replaceState(null, "", "/#/decisions");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    expect(screen.getByRole("heading", { level: 1, name: "Decisions" })).toBeVisible();
    expect(mocks.apiFetch.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
    expect(mocks.streamChat).not.toHaveBeenCalled();
  });

  it("closes the phone drawer on selection and keeps navigation reachable from the page", async () => {
    phone = true;
    const user = userEvent.setup();
    mount();
    await user.click(screen.getByRole("button", { name: "Open conversation sidebar" }));
    const drawer = screen.getByRole("dialog", { name: "Chat sessions" });
    await user.click(await within(drawer).findByRole("button", { name: "Decisions" }));
    expect(screen.queryByRole("dialog", { name: "Chat sessions" })).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Decisions" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Open conversation sidebar" }));
    expect(screen.getByRole("dialog", { name: "Chat sessions" })).toBeVisible();
  });

  it("hides unavailable navigation but preserves direct-link availability feedback", async () => {
    mocks.apiFetch.mockImplementation(async (path) => Response.json(
      path === "/api/decisions/options" ? { models: [] } : { enabled: false },
    ));
    window.history.replaceState(null, "", "/#/decisions");
    mount();
    expect(await screen.findByText("No Decision model is available under the current model and residency policy.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Decisions" })).toBeNull();
    expect(screen.getByRole("button", { name: "Run decision" })).toBeDisabled();
  });
});
