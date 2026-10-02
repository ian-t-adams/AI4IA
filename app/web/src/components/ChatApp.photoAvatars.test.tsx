// @vitest-environment jsdom
import { act, cleanup, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PhotoAvatar, PhotoAvatarConfig } from "@/lib/photoAvatars";
import { voiceProviderCatalog } from "@/lib/data/voice_provider_catalog";
import { VOICE_PREFERENCES_STORAGE_NAME } from "@/lib/voicePreferences";
import type { ToolCatalogItem } from "@/lib/types";
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
  // ChatApp reads image options on mount to gate image editing; editing stays off here.
  getImageOptions: vi.fn(async () => ({
    maxSelectedModels: 3, currency: "USD", priceVersion: null, models: [],
  })),
  apiFetch: vi.fn(),
  getVoiceLiveConfig: vi.fn(),
  voiceEnabled: false,
  voiceSupported: false,
  voiceActive: false,
  voiceSaving: false,
  avatarVideoSupported: true,
  useInlineVoiceLive: vi.fn(),
  startVoice: vi.fn(),
  stopVoice: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...mocks, ApiError: actual.ApiError, apiErrorDetail: actual.apiErrorDetail };
});
// Photo avatars talk to the API through apiFetch; everything else stays real.
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
vi.mock("./VoiceLiveProvider", () => ({
  useVoiceLiveConfig: () => ({ enabled: mocks.voiceEnabled, toolsAvailable: false }),
}));
vi.mock("@/lib/avatarVideo", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/avatarVideo")>()),
  supportsAvatarVideo: () => mocks.avatarVideoSupported,
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
  useInlineVoiceLive: (options: unknown) => {
    mocks.useInlineVoiceLive(options);
    return {
      active: mocks.voiceActive,
      supported: mocks.voiceSupported,
      phase: mocks.voiceActive ? "listening" : "idle",
      saving: mocks.voiceSaving,
      persistenceError: null,
      error: null,
      start: mocks.startVoice,
      stop: mocks.stopVoice,
      exitLocked: false,
      messages: [],
      boundSessionId: null,
      avatar: null,
    };
  },
}));

const DISABLED: PhotoAvatarConfig = {
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

const ENABLED: PhotoAvatarConfig = {
  enabled: true,
  available: false,
  reason: "capability_unavailable",
  canCreate: false,
  limits: {
    maxAvatars: 5,
    avatarCount: 0,
    maxCreationsPerDay: 5,
    creationsInLastDay: 0,
    nextCreationAt: null,
    promptMaxChars: 1000,
    displayNameMaxChars: 60,
  },
  attributes: { gender: [], age: [], ethnicity: [], style: [] },
  attestation: { version: "v1", statements: [] },
  disclosure: { label: "AI-generated", text: "Synthetic." },
  pricing: { currency: "USD", estimatedUsdPerAvatar: null, known: false, priceVersion: null },
  feedback: { reasons: ["other"], detailsMaxChars: 1000, microsoftReportUrl: "https://aka.ms/reportabuse" },
};

const READY: PhotoAvatar = {
  id: "a".repeat(32),
  displayName: "Office guide",
  prompt: "A fictional adult office guide.",
  attributes: { gender: null, age: null, ethnicity: null, style: null },
  status: "ready",
  failure: null,
  preview: null,
  disclosure: { aiGenerated: true, label: "AI-generated" },
  cost: { currency: "USD", estimatedUsd: 2, known: true, priceVersion: "v1", basis: "per_avatar" },
  usable: true,
  reported: false,
  createdAt: "2026-09-26T12:00:00Z",
  updatedAt: "2026-09-26T12:00:00Z",
  readyAt: "2026-09-26T12:00:00Z",
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function serve(config: PhotoAvatarConfig | Error, avatars: PhotoAvatar[] = []) {
  mocks.apiFetch.mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/photo-avatars/config") {
      if (config instanceof Error) throw config;
      return json(config);
    }
    if (path === "/api/photo-avatars") return json({ avatars });
    return json({ detail: "Not found.", code: "not_found" }, 404);
  });
}

function configReads() {
  return mocks.apiFetch.mock.calls.filter(([input]) => String(input) === "/api/photo-avatars/config");
}

beforeEach(() => {
  resetChatAppMocks(mocks);
  mocks.apiFetch.mockReset();
  mocks.voiceEnabled = false;
  mocks.voiceSupported = false;
  mocks.voiceActive = false;
  mocks.voiceSaving = false;
  mocks.avatarVideoSupported = true;
  mocks.getVoiceLiveConfig.mockResolvedValue({
    defaultProviderId: "azure_openai",
    enabledProviderIds: ["azure_openai", "speech_voice_live"],
    providers: [...voiceProviderCatalog.providers],
  });
  window.localStorage.clear();
});

function enableVoice() {
  mocks.voiceEnabled = true;
  mocks.voiceSupported = true;
  mocks.listModels.mockResolvedValue({
    ...CHAT_MODEL_CATALOG,
    models: [
      ...CHAT_MODEL_CATALOG.models,
      { ...CHAT_MODEL_CATALOG.models[0], id: "gpt-realtime", displayName: "GPT Realtime", category: "realtime" },
    ],
  });
  serve({ ...ENABLED, available: true, reason: "available" }, [READY]);
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("photo avatars in the workspace", () => {
  it.each([
    ["the server reports it disabled", DISABLED],
    ["the config read fails", new TypeError("Failed to fetch")],
  ])("offers no entry when %s", async (_label, config) => {
    serve(config);
    render(<ChatApp />);
    await screen.findByRole("button", { name: "Session A" });
    await waitFor(() => expect(configReads()).toHaveLength(1));
    // Let the settled read commit before asserting the entry stayed absent.
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByRole("button", { name: "Photo avatars" })).toBeNull();
    expect(screen.queryByRole("dialog", { name: "Photo avatars" })).toBeNull();
    expect(mocks.apiFetch.mock.calls.map(([input]) => String(input))).toEqual([
      "/api/photo-avatars/config",
    ]);
  });

  it("offers the entry and opens the gallery when the server reports it enabled", async () => {
    // Control for the test above: only `enabled` differs.
    const user = userEvent.setup();
    serve(ENABLED);
    render(<ChatApp />);
    const entry = await screen.findByRole("button", { name: "Photo avatars" });
    await user.click(entry);
    const dialog = await screen.findByRole("dialog", { name: "Photo avatars" });
    expect(await within(dialog).findByText(/Limited Access approval/)).toBeInTheDocument();
    expect(await within(dialog).findByText(/No avatars yet/)).toBeInTheDocument();
    expect(mocks.apiFetch.mock.calls.map(([input]) => String(input))).toContain("/api/photo-avatars");
    await user.click(within(dialog).getByRole("button", { name: "Close photo avatars" }));
    expect(screen.queryByRole("dialog", { name: "Photo avatars" })).toBeNull();
    await waitFor(() => expect(screen.getByRole("button", { name: "Photo avatars" })).toHaveFocus());
  });
});

describe("gallery-to-voice integration", () => {
  it("uses a newly ready avatar that was absent from the initial live-voice snapshot", async () => {
    enableVoice();
    serve({ ...ENABLED, available: true, reason: "available" }, []);
    const user = userEvent.setup();
    render(<ChatApp />);
    await waitFor(() => expect(mocks.apiFetch.mock.calls.filter(
      ([input]) => String(input) === "/api/photo-avatars",
    )).toHaveLength(1));
    const newlyReady = { ...READY, id: "b".repeat(32), displayName: "New guide" };
    serve({ ...ENABLED, available: true, reason: "available" }, [newlyReady]);
    await user.click(await screen.findByRole("button", { name: "Photo avatars" }));
    await user.click(await screen.findByRole("button", { name: "Use New guide in Voice Live" }));
    const start = await screen.findByRole("button", { name: "Start talking" });
    await waitFor(() => expect(start).toBeEnabled());
    expect(screen.getByRole("heading", { name: "Talk with New guide" })).toHaveFocus();
    await user.click(start);
    expect(mocks.startVoice).toHaveBeenCalledTimes(1);
    expect(mocks.useInlineVoiceLive).toHaveBeenLastCalledWith(expect.objectContaining({
      providerId: "speech_voice_live", avatar: { id: newlyReady.id, label: "AI-generated" },
    }));
  });

  it("selects the owned avatar and Azure Speech, then starts only on an explicit talking action", async () => {
    enableVoice();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Photo avatars" }));
    await user.click(await screen.findByRole("button", { name: "Use Office guide in Voice Live" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Photo avatars" })).toBeNull());
    const card = await screen.findByRole("region", { name: "Avatar voice" });
    const start = within(card).getByRole("button", { name: "Start talking" });
    await waitFor(() => expect(start).toBeEnabled());
    expect(within(card).getByText("Talk with Office guide")).toBeInTheDocument();
    expect(within(card).getByText(/billed while the session is connected/)).toBeInTheDocument();
    expect(mocks.startVoice).not.toHaveBeenCalled();
    expect(mocks.useInlineVoiceLive).toHaveBeenLastCalledWith(expect.objectContaining({
      providerId: "speech_voice_live",
      avatar: { id: READY.id, label: "AI-generated" },
    }));
    const saved = JSON.parse(window.localStorage.getItem(VOICE_PREFERENCES_STORAGE_NAME)!);
    expect(saved.provider).toBe("speech_voice_live");
    expect(saved.speechAvatarId).toBe(READY.id);
    await user.click(start);
    expect(mocks.startVoice).toHaveBeenCalledTimes(1);
    await user.click(within(card).getByRole("button", { name: "Voice only" }));
    expect(screen.queryByRole("region", { name: "Avatar voice" })).toBeNull();
    expect(mocks.useInlineVoiceLive).toHaveBeenLastCalledWith(expect.objectContaining({
      providerId: "speech_voice_live", avatar: null,
    }));
  });

  it.each([
    ["Azure Speech is unavailable", () => {
      mocks.getVoiceLiveConfig.mockResolvedValue({
        defaultProviderId: "azure_openai",
        enabledProviderIds: ["azure_openai"],
        providers: [...voiceProviderCatalog.providers],
      });
    }, /Azure Speech Voice Live/],
    ["avatar video is unsupported", () => { mocks.avatarVideoSupported = false; }, /can't play avatar video/],
    ["a voice session is active", () => { mocks.voiceActive = true; }, /End the current voice session/],
    ["a transcript is saving", () => { mocks.voiceSaving = true; }, /Finish saving/],
  ])("explains why the gallery cannot use an avatar when %s", async (_label, configure, explanation) => {
    enableVoice();
    configure();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Photo avatars" }));
    const use = await screen.findByRole("button", { name: "Use Office guide in Voice Live" });
    expect(use).toBeDisabled();
    expect(use).toHaveAccessibleDescription(explanation);
    await user.click(use);
    expect(mocks.startVoice).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Photo avatars" })).toBeInTheDocument();
  });

  it("does not fall back to voice only while a newly selected avatar is being refreshed", async () => {
    enableVoice();
    const user = userEvent.setup();
    render(<ChatApp />);
    await user.click(await screen.findByRole("button", { name: "Photo avatars" }));
    const use = await screen.findByRole("button", { name: "Use Office guide in Voice Live" });
    let finish!: (response: Response) => void;
    mocks.apiFetch.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input) === "/api/photo-avatars/config") {
        return json({ ...ENABLED, available: true, reason: "available" });
      }
      if (String(input) === "/api/photo-avatars") {
        return new Promise<Response>((resolve) => { finish = resolve; });
      }
      return json({ detail: "Not found." }, 404);
    });
    await user.click(use);
    const start = screen.getByRole("button", { name: "Start talking" });
    expect(start).toBeDisabled();
    expect(screen.getByRole("button", { name: "Start live voice conversation" })).toBeDisabled();
    await user.click(start);
    expect(mocks.startVoice).not.toHaveBeenCalled();
    await act(async () => { finish(json({ avatars: [READY] })); });
    await waitFor(() => expect(start).toBeEnabled());
    await user.click(start);
    expect(mocks.startVoice).toHaveBeenCalledTimes(1);
    expect(mocks.useInlineVoiceLive).toHaveBeenLastCalledWith(expect.objectContaining({
      avatar: { id: READY.id, label: "AI-generated" },
    }));
  });

  it("keeps an unavailable saved avatar explicit until the user chooses voice only", async () => {
    enableVoice();
    window.localStorage.setItem(VOICE_PREFERENCES_STORAGE_NAME, JSON.stringify({
      provider: "speech_voice_live", speechAvatarId: READY.id,
    }));
    serve({ ...ENABLED, available: true, reason: "available" }, []);
    const user = userEvent.setup();
    render(<ChatApp />);
    const start = await screen.findByRole("button", { name: "Start talking" });
    await waitFor(() => expect(screen.getByText(/no longer ready or available/)).toBeInTheDocument());
    expect(start).toBeDisabled();
    expect(screen.getByRole("button", { name: "Start live voice conversation" })).toBeDisabled();
    await user.click(start);
    expect(mocks.startVoice).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Voice only" }));
    await user.click(screen.getByRole("button", { name: "Start live voice conversation" }));
    expect(mocks.startVoice).toHaveBeenCalledTimes(1);
  });
});
