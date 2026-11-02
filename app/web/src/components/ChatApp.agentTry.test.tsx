// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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
  streamChat: vi.fn(), toolCatalog: [] as ToolCatalogItem[], getToolCatalog: vi.fn(),
  updateSession: vi.fn(), getInspector: vi.fn(), listMemories: vi.fn(),
  getLibrarySummary: vi.fn(), createMemory: vi.fn(), updateMemory: vi.fn(),
  deleteMemory: vi.fn(), appendVoiceTurns: vi.fn(), getImageOptions: vi.fn(),
  voiceActive: false, saving: false, exitLocked: false,
  refreshError: vi.fn(),
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
vi.mock("./InlineVoiceLive", () => ({
  InlineVoiceLiveStatus: () => null,
  mergeDisplayMessages: (messages: unknown[]) => messages,
  voiceMessagesForSession: () => [],
  useInlineVoiceLive: () => ({
    active: mocks.voiceActive, supported: false, phase: "idle", saving: mocks.saving,
    persistenceError: null, error: null, start: vi.fn(), stop: vi.fn(),
    exitLocked: mocks.exitLocked, messages: [], boundSessionId: null,
  }),
}));
vi.mock("./AgentBuilder", () => ({
  AgentBuilder: ({ onTry, tryDisabledReason, onChanged }: {
    onTry?: (name: string) => void; tryDisabledReason?: string | null; onChanged: () => Promise<void>;
  }) => (
    <>
      <button disabled={!!tryDisabledReason} onClick={() => onTry?.("notes-helper")}>Try saved agent</button>
      <button onClick={() => { void onChanged().catch(mocks.refreshError); }}>Refresh saved agents</button>
      {tryDisabledReason && <p>{tryDisabledReason}</p>}
    </>
  ),
}));

beforeEach(() => {
  resetChatAppMocks(mocks);
  mocks.voiceActive = false;
  mocks.saving = false;
  mocks.exitLocked = false;
  mocks.getImageOptions.mockResolvedValue({
    maxSelectedModels: 3, currency: "USD", priceVersion: null, models: [],
  });
  window.history.replaceState(null, "", "/");
  window.localStorage.clear();
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe("trying an agent from Studio", () => {
  it("returns catalog refresh errors to the editor instead of swallowing them in hidden chat", async () => {
    const user = userEvent.setup();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await screen.findByRole("button", { name: "Session A" });
    await waitFor(() => expect(mocks.listAgents).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: /Agents & workflows/ }));
    mocks.listAgents.mockRejectedValueOnce(new Error("Catalog temporarily unavailable."));
    await user.click(screen.getByRole("button", { name: "Refresh saved agents" }));
    await waitFor(() => expect(mocks.refreshError).toHaveBeenCalledWith(
      expect.objectContaining({ message: "Catalog temporarily unavailable." }),
    ));
  });

  it("prepares the existing composer and focuses it, without sending, saving settings or replacing a draft", async () => {
    const user = userEvent.setup();
    render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: "Session A" }));
    const composer = await screen.findByRole("combobox", { name: "Message" });
    await waitFor(() => expect(composer).toBeEnabled());
    await user.type(composer, "Summarize these notes");
    await user.click(screen.getByRole("button", { name: /Agents & workflows/ }));
    await user.click(screen.getByRole("button", { name: "Try saved agent" }));
    await waitFor(() => expect(window.location.hash).toBe(""));
    expect(composer).toHaveValue("@notes-helper Summarize these notes");
    expect(composer).toHaveFocus();
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(mocks.updateSession).not.toHaveBeenCalled();
    expect(mocks.streamChat).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Send" }));
    await waitFor(() => expect(mocks.streamChat).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: "A", content: "@notes-helper Summarize these notes" }),
      expect.any(Object),
    ));
    expect(mocks.createSession).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Agents & workflows/ })).toHaveAttribute("aria-disabled", "true");
  });

  it.each(["voiceActive", "saving", "exitLocked"] as const)("keeps the handoff unavailable during %s", async (key) => {
    const user = userEvent.setup();
    const view = render(<ChatApp />, { wrapper: MemoryPreferenceProvider });
    await user.click(await screen.findByRole("button", { name: /Agents & workflows/ }));
    mocks[key] = true;
    view.rerender(<ChatApp />);
    const tryIt = screen.getByRole("button", { name: "Try saved agent" });
    expect(tryIt).toBeDisabled();
    expect(screen.getByText(/Finish your live session and save its transcript/)).toBeVisible();
    await user.click(tryIt);
    expect(window.location.hash).toBe("#/studio");
    expect(mocks.streamChat).not.toHaveBeenCalled();
  });
});
