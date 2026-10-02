// @vitest-environment jsdom
import { act, cleanup, render as rtlRender, screen, waitFor } from "@testing-library/react";
import type { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PhotoAvatarConfig } from "@/lib/photoAvatars";
import { voiceProviderCatalog } from "@/lib/data/voice_provider_catalog";
import type { ToolCatalogItem } from "@/lib/types";
import { BUILD_CHECK_MIN_SPACING_MS, WEB_BUILD_ENDPOINT } from "@/lib/webBuild";
import { ChatApp } from "./ChatApp";
import { MemoryPreferenceProvider } from "./MemoryPreferenceProvider";
import { CHAT_MODEL_CATALOG, resetChatAppMocks } from "./chatTestFixtures";

function render(ui: ReactElement) {
  return rtlRender(ui, { wrapper: MemoryPreferenceProvider });
}

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
  apiFetch: vi.fn(),
  getVoiceLiveConfig: vi.fn(),
  voiceActive: false,
  startVoice: vi.fn(),
  stopVoice: vi.fn(),
  reloadPage: vi.fn(),
  fetch: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...mocks, ApiError: actual.ApiError, apiErrorDetail: actual.apiErrorDetail };
});
vi.mock("@/lib/auth", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/auth")>()),
  apiFetch: mocks.apiFetch,
}));
vi.mock("@/lib/inspector", () => ({
  getInspector: mocks.getInspector,
  listMemories: mocks.listMemories,
  getLibrarySummary: mocks.getLibrarySummary,
  createMemory: mocks.createMemory,
  updateMemory: mocks.updateMemory,
  deleteMemory: mocks.deleteMemory,
}));
// The real stale-build hook runs; only the page reload itself is observed.
vi.mock("@/lib/webBuild", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/webBuild")>()),
  reloadPage: mocks.reloadPage,
}));
vi.mock("./VoiceLiveProvider", () => ({
  useVoiceLiveConfig: () => ({ enabled: true, toolsAvailable: false }),
}));
vi.mock("./LibraryProvider", () => ({
  useLibraryConfig: () => ({ enabled: false }),
}));
vi.mock("./CustomToolsProvider", () => ({
  useCustomToolsConfig: () => ({ enabled: false }),
}));
vi.mock("./AdminLink", () => ({ AdminLink: () => null }));
vi.mock("./UserMenu", () => ({ UserMenu: () => null }));
vi.mock("./MessageList", () => ({ MessageList: () => null }));
vi.mock("./InlineVoiceLive", () => ({
  InlineVoiceLiveStatus: () => null,
  mergeDisplayMessages: (messages: unknown[]) => messages,
  voiceMessagesForSession: () => [],
  useInlineVoiceLive: () => ({
    enabled: true,
    active: mocks.voiceActive,
    supported: true,
    phase: mocks.voiceActive ? "listening" : "idle",
    saving: false,
    persistenceError: null,
    error: null,
    start: mocks.startVoice,
    stop: mocks.stopVoice,
    exitLocked: false,
    messages: [],
    boundSessionId: null,
    avatar: null,
    sendText: () => true,
  }),
}));

const PHOTO_AVATARS_OFF: PhotoAvatarConfig = {
  enabled: false,
  available: false,
  reason: "disabled",
  canCreate: false,
  limits: null,
  attributes: null,
  attestation: null,
  disclosure: null,
  pricing: null,
  feedback: null,
};

const START_VOICE = "Start live voice conversation";
const NEW_VERSION = "A new version of AI4IA is available.";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function deploy(read: () => Promise<Response>) {
  mocks.fetch.mockImplementation(async (input: RequestInfo | URL) =>
    String(input) === WEB_BUILD_ENDPOINT ? read() : json({ detail: "Not found." }, 404),
  );
}

function buildReads() {
  return mocks.fetch.mock.calls.filter(([input]) => String(input) === WEB_BUILD_ENDPOINT);
}

// Returning to the tab after the minimum spacing triggers a check.
async function returnToTab() {
  vi.setSystemTime(Date.now() + BUILD_CHECK_MIN_SPACING_MS + 1);
  act(() => {
    window.dispatchEvent(new Event("focus"));
  });
  await waitFor(() => expect(buildReads()).toHaveLength(1));
}

beforeEach(() => {
  resetChatAppMocks(mocks);
  // Only the clock is faked: the poll's own timers never fire during a test.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-10-02T12:00:00Z"));
  vi.stubEnv("AI4IA_WEB_BUILD_ID", "build-a");
  vi.stubGlobal("fetch", mocks.fetch);
  mocks.voiceActive = false;
  mocks.apiFetch.mockImplementation(async () => json(PHOTO_AVATARS_OFF));
  mocks.getVoiceLiveConfig.mockResolvedValue({
    defaultProviderId: "speech_voice_live",
    enabledProviderIds: ["azure_openai", "speech_voice_live"],
    providers: [...voiceProviderCatalog.providers],
  });
  mocks.listModels.mockResolvedValue({
    ...CHAT_MODEL_CATALOG,
    models: [
      ...CHAT_MODEL_CATALOG.models,
      { ...CHAT_MODEL_CATALOG.models[0], id: "gpt-realtime", displayName: "GPT Realtime", category: "realtime" },
    ],
  });
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("a tab running an older web build", () => {
  it("keeps voice available and shows nothing while the deployed build matches", async () => {
    deploy(async () => json({ buildId: "build-a" }));
    const user = userEvent.setup();
    render(<ChatApp />);
    const start = await screen.findByRole("button", { name: START_VOICE });
    await returnToTab();
    expect(screen.queryByText(NEW_VERSION)).toBeNull();
    await user.click(start);
    expect(mocks.startVoice).toHaveBeenCalledTimes(1);
  });

  it("offers a reload and asks for one before a new voice session", async () => {
    deploy(async () => json({ buildId: "build-b" }));
    const user = userEvent.setup();
    render(<ChatApp />);
    const start = await screen.findByRole("button", { name: START_VOICE });
    await returnToTab();
    expect(await screen.findByText(NEW_VERSION)).toBeInTheDocument();
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute("title", expect.stringMatching(/Reload the page before starting a voice session/));
    await user.click(start);
    expect(mocks.startVoice).not.toHaveBeenCalled();
    // Never by itself: only the user's Reload reloads.
    expect(mocks.reloadPage).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(mocks.reloadPage).toHaveBeenCalledTimes(1);
  });

  it("treats an unreadable deployed build as unknown", async () => {
    deploy(async () => { throw new TypeError("Failed to fetch"); });
    const user = userEvent.setup();
    render(<ChatApp />);
    const start = await screen.findByRole("button", { name: START_VOICE });
    await returnToTab();
    expect(screen.queryByText(NEW_VERSION)).toBeNull();
    await user.click(start);
    expect(mocks.startVoice).toHaveBeenCalledTimes(1);
  });

  it("waits to reload until a live voice session ends", async () => {
    deploy(async () => json({ buildId: "build-b" }));
    mocks.voiceActive = true;
    const user = userEvent.setup();
    const view = render(<ChatApp />);
    await screen.findByRole("button", { name: "Stop live voice conversation" });
    await returnToTab();
    const reload = await screen.findByRole("button", { name: "Reload" });
    expect(reload).toBeDisabled();
    expect(reload).toHaveAccessibleDescription("Reload after your voice session ends.");
    await user.click(reload);
    expect(mocks.reloadPage).not.toHaveBeenCalled();
    // The running session itself is untouched and can still be ended.
    expect(screen.getByRole("button", { name: "Stop live voice conversation" })).toBeEnabled();

    // Control: once the session ends, the same Reload works.
    mocks.voiceActive = false;
    view.rerender(<ChatApp />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Reload" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Reload" }));
    expect(mocks.reloadPage).toHaveBeenCalledTimes(1);
  });

  it("confirms before a reload would clear an unsent message", async () => {
    deploy(async () => json({ buildId: "build-b" }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<ChatApp />);
    await screen.findByRole("button", { name: START_VOICE });
    await returnToTab();
    const reload = await screen.findByRole("button", { name: "Reload" });

    await user.type(screen.getByPlaceholderText("Message AI4IA"), "half-written question");
    await user.click(reload);
    expect(confirm).toHaveBeenCalledTimes(1);
    expect(mocks.reloadPage).not.toHaveBeenCalled();

    confirm.mockReturnValue(true);
    await user.click(reload);
    expect(mocks.reloadPage).toHaveBeenCalledTimes(1);

    // Control: with the box cleared, Reload asks nothing.
    await user.clear(screen.getByPlaceholderText("Message AI4IA"));
    confirm.mockClear();
    await user.click(reload);
    expect(confirm).not.toHaveBeenCalled();
    expect(mocks.reloadPage).toHaveBeenCalledTimes(2);
    confirm.mockRestore();
  });
});
