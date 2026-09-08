import {
  getMemoryPreference,
  updateMemoryPreference,
  type MemoryPreference,
} from "./inspector";

export interface MemoryPreferenceState {
  preference: MemoryPreference | null;
  phase: "loading" | "ready" | "saving" | "error";
  error: string | null;
}

export function createMemoryPreferenceStore(isCurrentOwner: () => boolean) {
  let state: MemoryPreferenceState = { preference: null, phase: "loading", error: null };
  let generation = 0;
  let writing = false;
  const listeners = new Set<() => void>();
  const publish = (next: MemoryPreferenceState) => {
    state = next;
    for (const notify of listeners) notify();
  };

  async function read(writeError: string | null = null) {
    const current = ++generation;
    publish({ ...state, phase: "loading", error: writeError });
    try {
      const preference = await getMemoryPreference();
      if (generation !== current) return;
      publish({ preference, phase: "ready", error: writeError });
    } catch (reason) {
      if (generation !== current) return;
      publish({
        ...state, phase: "error",
        error: reason instanceof Error ? reason.message : "Memory preference is unavailable.",
      });
    }
  }

  return {
    getSnapshot: () => state,
    subscribe: (notify: () => void) => {
      listeners.add(notify);
      return () => { listeners.delete(notify); };
    },
    refresh: async () => {
      if (!isCurrentOwner() || writing) return;
      await read();
    },
    change: async (enabled: boolean) => {
      if (!isCurrentOwner() || writing || state.phase !== "ready" || !state.preference) return;
      const previous = state.preference;
      writing = true;
      ++generation;
      publish({
        preference: { ...previous, automaticMemoryEnabled: enabled },
        phase: "saving", error: null,
      });
      let preference = previous;
      let error: string | null = null;
      try {
        preference = await updateMemoryPreference(enabled, previous.etag);
      } catch (reason) {
        error = reason instanceof Error ? reason.message : "The memory preference could not be saved.";
      }
      writing = false;
      publish({ preference, phase: "loading", error });
      // A lost PATCH response can still have committed. Reconcile only as the
      // same authenticated owner, never using the next account's credentials.
      if (isCurrentOwner()) await read(error);
    },
  };
}

export type MemoryPreferenceStore = ReturnType<typeof createMemoryPreferenceStore>;

export function createMemoryPreferenceRegistry(currentOwner: () => string | null) {
  const stores = new Map<string, MemoryPreferenceStore>();
  let mounted = true;
  return {
    setMounted(value: boolean) { mounted = value; },
    get(key: string) {
      let store = stores.get(key);
      if (!store) {
        store = createMemoryPreferenceStore(() => mounted && currentOwner() === key);
        stores.set(key, store);
      }
      return store;
    },
  };
}
