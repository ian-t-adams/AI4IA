// @vitest-environment jsdom
import { cleanup, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { PhotoAvatarConfig } from "@/lib/photoAvatars";
import type { ToolCatalogItem } from "@/lib/types";
import { ChatApp } from "./ChatApp";
import { MemoryPreferenceProvider } from "./MemoryPreferenceProvider";
import { resetChatAppMocks } from "./chatTestFixtures";

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
vi.mock("./Composer", () => ({ Composer: () => null }));
vi.mock("./MessageList", () => ({ MessageList: () => null }));
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

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function serve(config: PhotoAvatarConfig | Error) {
  mocks.apiFetch.mockImplementation(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/photo-avatars/config") {
      if (config instanceof Error) throw config;
      return json(config);
    }
    if (path === "/api/photo-avatars") return json({ avatars: [] });
    return json({ detail: "Not found.", code: "not_found" }, 404);
  });
}

function configReads() {
  return mocks.apiFetch.mock.calls.filter(([input]) => String(input) === "/api/photo-avatars/config");
}

beforeEach(() => {
  resetChatAppMocks(mocks);
  mocks.apiFetch.mockReset();
});

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
