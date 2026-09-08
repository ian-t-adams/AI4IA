"use client";

import { createContext, useContext, useLayoutEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { useMsal } from "@azure/msal-react";
import { createMemoryPreferenceRegistry, type MemoryPreferenceStore } from "@/lib/memoryPreferenceState";

export interface MemoryPreferenceOwner {
  getSnapshot: () => string | null;
  subscribe: (notify: () => void) => () => void;
}

const localOwner: MemoryPreferenceOwner = {
  getSnapshot: () => "dev",
  subscribe: () => () => {},
};
const serverOwner = () => null;
const PreferenceContext = createContext<MemoryPreferenceStore | null | undefined>(undefined);

export function MemoryPreferenceProvider({
  owner = localOwner, children,
}: {
  owner?: MemoryPreferenceOwner;
  children: ReactNode;
}) {
  const ownerKey = useSyncExternalStore(owner.subscribe, owner.getSnapshot, serverOwner);
  const registry = useMemo(() => createMemoryPreferenceRegistry(owner.getSnapshot), [owner]);
  useLayoutEffect(() => {
    registry.setMounted(true);
    return () => { registry.setMounted(false); };
  }, [registry]);
  return (
    <PreferenceContext value={ownerKey === null ? null : registry.get(ownerKey)}>
      {children}
    </PreferenceContext>
  );
}

export function EntraMemoryPreferenceProvider({ children }: { children: ReactNode }) {
  const { instance } = useMsal();
  const owner = useMemo<MemoryPreferenceOwner>(() => ({
    getSnapshot: () => {
      const account = instance.getActiveAccount() ?? instance.getAllAccounts()[0];
      return account
        ? JSON.stringify([account.homeAccountId, account.localAccountId, account.tenantId])
        : null;
    },
    subscribe: (notify) => {
      const callback = instance.addEventCallback(notify);
      return () => { if (callback) instance.removeEventCallback(callback); };
    },
  }), [instance]);
  return <MemoryPreferenceProvider owner={owner}>{children}</MemoryPreferenceProvider>;
}

export function useMemoryPreferenceStore() {
  const store = useContext(PreferenceContext);
  if (store === undefined) {
    throw new Error("Memory preferences require an owner-scoped provider.");
  }
  return store;
}
