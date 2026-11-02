// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { UserAgent } from "@/lib/types";
import { AgentBuilder } from "./AgentBuilder";
import { MemoryPreferenceProvider, type MemoryPreferenceOwner } from "./MemoryPreferenceProvider";

// The external account snapshot can change before the provider is notified and
// re-renders. Every mutation and late result must re-check the live owner.

const mocks = vi.hoisted(() => ({
  listMyAgents: vi.fn(),
  listOfficialMcpServers: vi.fn(),
  createAgent: vi.fn(),
  updateAgent: vi.fn(),
  deleteAgent: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("@/lib/api")>(),
  ...mocks,
}));
vi.mock("./PublicationControls", () => ({ PublicationControls: () => null }));

const saved: UserAgent = {
  id: "helper", userId: "owner-a", name: "helper", displayName: "Owner A helper",
  description: "", revision: 1, incarnation: "a".repeat(32),
  systemPrompt: "Owner A instructions", defaultModel: null,
  tools: [], links: [], enabled: true, createdAt: "", updatedAt: "",
};
const ACCOUNT_CHANGED = /signed-in account changed\. Wait for your agents to reload/;
const OFFICIAL_ERROR = /Official tools could not be loaded/;

let key: string | null;
const listeners = new Set<() => void>();
const owner: MemoryPreferenceOwner = {
  getSnapshot: () => key,
  subscribe: (listener) => { listeners.add(listener); return () => listeners.delete(listener); },
};
const onChanged = vi.fn();

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function switchSilently(next: string | null) { key = next; }
function switchAndNotify(next: string | null) {
  act(() => { key = next; listeners.forEach((l) => l()); });
}

beforeEach(() => {
  key = "owner-a";
  listeners.clear();
  mocks.listMyAgents.mockResolvedValue([]);
  mocks.listOfficialMcpServers.mockResolvedValue([]);
  mocks.createAgent.mockImplementation(() => new Promise(() => {}));
  mocks.updateAgent.mockImplementation(() => new Promise(() => {}));
  mocks.deleteAgent.mockImplementation(() => new Promise(() => {}));
  onChanged.mockResolvedValue(undefined);
});
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.restoreAllMocks(); });

function openEditor() {
  render(
    <MemoryPreferenceProvider owner={owner}>
      <AgentBuilder agents={[]} models={[]} onChanged={onChanged} />
    </MemoryPreferenceProvider>,
  );
}

async function fillNewDraft() {
  await waitFor(() => expect(screen.queryByText("Loading your agents...")).toBeNull());
  fireEvent.change(screen.getByLabelText("What should this agent do?"), { target: { value: "A private draft" } });
  fireEvent.change(screen.getByLabelText("Agent name"), { target: { value: "A helper" } });
}

describe("dispatch re-checks the live owner", () => {
  it("creates while the owner is unchanged (control)", async () => {
    openEditor();
    await fillNewDraft();
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect(mocks.createAgent).toHaveBeenCalledTimes(1);
  });

  it("does not create a stale owner's draft before the provider rerenders", async () => {
    openEditor();
    await fillNewDraft();
    switchSilently("owner-b");
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect(mocks.createAgent).not.toHaveBeenCalled();
    expect(await screen.findByText(ACCOUNT_CHANGED)).toBeTruthy();
  });

  it("does not create after a sign-out that has not rerendered yet", async () => {
    openEditor();
    await fillNewDraft();
    switchSilently(null);
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect(mocks.createAgent).not.toHaveBeenCalled();
  });

  it("updates the selected agent while the owner is unchanged (control)", async () => {
    mocks.listMyAgents.mockResolvedValue([saved]);
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Owner A helper" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(mocks.updateAgent).toHaveBeenCalledWith("helper", expect.objectContaining({ expectedRevision: 1 }));
  });

  it("does not update the prior owner's selected agent before the provider rerenders", async () => {
    mocks.listMyAgents.mockResolvedValue([saved]);
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Owner A helper" }));
    switchSilently("owner-b");
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    expect(mocks.updateAgent).not.toHaveBeenCalled();
  });

  it("deletes after confirmation while the owner is unchanged (control)", async () => {
    mocks.listMyAgents.mockResolvedValue([saved]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Delete helper" }));
    expect(mocks.deleteAgent).toHaveBeenCalledWith("helper");
  });

  it("rechecks the current owner after the delete confirmation", async () => {
    mocks.listMyAgents.mockResolvedValue([saved]);
    vi.spyOn(window, "confirm").mockImplementation(() => { key = "owner-b"; return true; });
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Delete helper" }));
    expect(mocks.deleteAgent).not.toHaveBeenCalled();
    expect(await screen.findByText(ACCOUNT_CHANGED)).toBeTruthy();
  });

  it("does not offer the delete confirmation once the owner is stale", async () => {
    mocks.listMyAgents.mockResolvedValue([saved]);
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    openEditor();
    const button = await screen.findByRole("button", { name: "Delete helper" });
    switchSilently(null);
    fireEvent.click(button);
    expect(confirm).not.toHaveBeenCalled();
    expect(mocks.deleteAgent).not.toHaveBeenCalled();
  });
});

describe("late results are dropped after an owner change", () => {
  it("applies a list response while the owner is unchanged (control)", async () => {
    const list = deferred<UserAgent[]>();
    mocks.listMyAgents.mockImplementationOnce(() => list.promise);
    openEditor();
    await act(async () => { list.resolve([saved]); });
    expect(screen.getByRole("button", { name: "Owner A helper" })).toBeTruthy();
  });

  it("ignores an old list response if the account changed before notification", async () => {
    const list = deferred<UserAgent[]>();
    mocks.listMyAgents.mockImplementationOnce(() => list.promise);
    openEditor();
    switchSilently("owner-b");
    await act(async () => { list.resolve([saved]); });
    expect(screen.queryByRole("button", { name: "Owner A helper" })).toBeNull();
  });

  it("ignores a first-epoch list response after switching A to B and back to A", async () => {
    const list = deferred<UserAgent[]>();
    mocks.listMyAgents.mockImplementationOnce(() => list.promise);
    openEditor();
    switchAndNotify("owner-b");
    switchAndNotify("owner-a");
    await waitFor(() => expect(screen.queryByText("Loading your agents...")).toBeNull());
    await act(async () => { list.resolve([saved]); });
    expect(screen.queryByRole("button", { name: "Owner A helper" })).toBeNull();
  });

  it("does not fetch while signed out", async () => {
    openEditor();
    await waitFor(() => expect(mocks.listMyAgents).toHaveBeenCalledTimes(1));
    switchAndNotify(null);
    await act(async () => {});
    expect(mocks.listMyAgents).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Sign in to load your agents.")).toBeTruthy();
  });

  it("reports an official tools failure while the owner is unchanged (control)", async () => {
    const servers = deferred<never>();
    mocks.listOfficialMcpServers.mockImplementationOnce(() => servers.promise);
    openEditor();
    await act(async () => { servers.reject(new Error("offline")); });
    expect(screen.getByText(OFFICIAL_ERROR)).toBeTruthy();
  });

  it("drops a late official tools failure after the account changed", async () => {
    const servers = deferred<never>();
    mocks.listOfficialMcpServers.mockImplementationOnce(() => servers.promise);
    openEditor();
    switchSilently("owner-b");
    await act(async () => { servers.reject(new Error("offline")); });
    expect(screen.queryByText(OFFICIAL_ERROR)).toBeNull();
  });

  it("drops a late create response after the account changed", async () => {
    const created = deferred<UserAgent>();
    mocks.createAgent.mockImplementationOnce(() => created.promise);
    openEditor();
    await fillNewDraft();
    fireEvent.click(screen.getByRole("button", { name: "Create agent" }));
    expect(mocks.createAgent).toHaveBeenCalledTimes(1);
    switchSilently("owner-b");
    await act(async () => { created.resolve({ ...saved, name: "a-helper", displayName: "A helper" }); });
    expect(screen.queryByText(/^Saved\./)).toBeNull();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("drops a late update response after a sign-out", async () => {
    mocks.listMyAgents.mockResolvedValue([saved]);
    const updated = deferred<UserAgent>();
    mocks.updateAgent.mockImplementationOnce(() => updated.promise);
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Owner A helper" }));
    fireEvent.click(screen.getByRole("button", { name: "Save changes" }));
    switchAndNotify(null);
    await act(async () => { updated.resolve({ ...saved, revision: 2 }); });
    expect(screen.queryByText(/^Saved\./)).toBeNull();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("drops a late delete response after the account changed", async () => {
    mocks.listMyAgents.mockResolvedValue([saved]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const removed = deferred<void>();
    mocks.deleteAgent.mockImplementationOnce(() => removed.promise);
    openEditor();
    fireEvent.click(await screen.findByRole("button", { name: "Delete helper" }));
    expect(mocks.deleteAgent).toHaveBeenCalledTimes(1);
    const listCalls = mocks.listMyAgents.mock.calls.length;
    switchSilently("owner-b");
    await act(async () => { removed.resolve(); });
    expect(screen.queryByText(/^Deleted "/)).toBeNull();
    expect(mocks.listMyAgents).toHaveBeenCalledTimes(listCalls);
    expect(onChanged).not.toHaveBeenCalled();
  });
});
