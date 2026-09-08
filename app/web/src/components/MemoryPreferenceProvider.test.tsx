// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryPreferenceControl } from "./MemoryPreferenceControl";
import { EntraMemoryPreferenceProvider } from "./MemoryPreferenceProvider";
import { createMemoryPreferenceStore } from "@/lib/memoryPreferenceState";
import type { MemoryPreference } from "@/lib/inspector";

const mocks = vi.hoisted(() => ({
  get: vi.fn(), update: vi.fn(), owner: "alice",
  listeners: new Map<string, () => void>(),
  instance: {
    getActiveAccount: vi.fn(), getAllAccounts: vi.fn(),
    addEventCallback: vi.fn(), removeEventCallback: vi.fn(),
  },
}));
vi.mock("@/lib/inspector", () => ({
  getMemoryPreference: mocks.get, updateMemoryPreference: mocks.update,
}));
vi.mock("@azure/msal-react", () => ({
  useMsal: () => ({ instance: mocks.instance }),
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

const off = { automaticMemoryEnabled: false, etag: '"preference-1"' };
const on = { automaticMemoryEnabled: true, etag: '"preference-2"' };

function selectOwner(owner: string) {
  act(() => {
    mocks.owner = owner;
    for (const notify of mocks.listeners.values()) notify();
  });
}

beforeEach(() => {
  mocks.owner = "alice";
  mocks.instance.getActiveAccount.mockImplementation(() => ({
    homeAccountId: mocks.owner, localAccountId: mocks.owner, tenantId: "tenant",
  }));
  mocks.instance.getAllAccounts.mockReturnValue([]);
  mocks.instance.addEventCallback.mockImplementation((notify: () => void) => {
    const id = String(mocks.listeners.size);
    mocks.listeners.set(id, notify);
    return id;
  });
  mocks.instance.removeEventCallback.mockImplementation((id: string) => mocks.listeners.delete(id));
});
afterEach(() => { cleanup(); mocks.listeners.clear(); vi.resetAllMocks(); });

describe("owner-scoped memory preference lifetime", () => {
  it.each([false, true])("keeps Alice's pending mutation isolated from Bob (return before commit: %s)", async (returnEarly) => {
    const values: Record<string, MemoryPreference> = { alice: off, bob: off };
    const readOwners: string[] = [];
    mocks.get.mockImplementation(async () => {
      readOwners.push(mocks.owner);
      return values[mocks.owner];
    });
    const pending = deferred<MemoryPreference>();
    mocks.update.mockReturnValue(pending.promise);
    render(<EntraMemoryPreferenceProvider><MemoryPreferenceControl /></EntraMemoryPreferenceProvider>);
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    await userEvent.setup().click(screen.getByRole("switch"));
    selectOwner("bob");
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    expect(screen.getByRole("switch")).not.toBeChecked();
    if (returnEarly) {
      selectOwner("alice");
      expect(screen.getByRole("switch")).toBeDisabled();
      expect(screen.getByRole("status")).not.toHaveTextContent("Automatic memory is off.");
    }
    const readsBeforeCommit = readOwners.length;
    values.alice = on;
    await act(async () => pending.resolve(on));
    if (!returnEarly) {
      expect(readOwners).toHaveLength(readsBeforeCommit);
      expect(screen.getByRole("switch")).not.toBeChecked();
      expect(screen.getByRole("status")).toHaveTextContent("Automatic memory is off.");
      selectOwner("alice");
    }
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    expect(screen.getByRole("switch")).toBeChecked();
    expect(screen.getByRole("status")).toHaveTextContent("Automatic memory is on.");
  });

  it("fences an old GET across an in-flight PATCH and waits for settlement reconciliation", async () => {
    const oldRead = deferred<MemoryPreference>();
    const pending = deferred<MemoryPreference>();
    const reconcile = deferred<MemoryPreference>();
    mocks.get.mockReturnValueOnce(oldRead.promise).mockResolvedValueOnce(off)
      .mockReturnValueOnce(reconcile.promise);
    mocks.update.mockReturnValue(pending.promise);
    const store = createMemoryPreferenceStore(() => true);
    const older = store.refresh();
    await store.refresh();
    expect(store.getSnapshot().phase).toBe("ready");
    const mutation = store.change(true);
    const count = mocks.get.mock.calls.length;
    await store.refresh();
    expect(mocks.get).toHaveBeenCalledTimes(count);
    oldRead.resolve(off);
    await older;
    expect(store.getSnapshot().phase).toBe("saving");
    pending.resolve(on);
    await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(count + 1));
    expect(store.getSnapshot().phase).toBe("loading");
    reconcile.resolve(on);
    await mutation;
    expect(store.getSnapshot()).toEqual({ preference: on, phase: "ready", error: null });
  });

  it("never applies a former owner's late GET to the active owner", async () => {
    const alice = deferred<MemoryPreference>();
    mocks.get.mockImplementation(() => mocks.owner === "alice" ? alice.promise : Promise.resolve(off));
    render(<EntraMemoryPreferenceProvider><MemoryPreferenceControl /></EntraMemoryPreferenceProvider>);
    selectOwner("bob");
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    await act(async () => alice.resolve(on));
    expect(screen.getByRole("switch")).not.toBeChecked();
  });
});
