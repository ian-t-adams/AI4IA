"use client";

import { createContext, useContext, useLayoutEffect, useMemo, useSyncExternalStore, type ReactNode } from "react";
import { useMsal } from "@azure/msal-react";
import { createMemoryPreferenceRegistry, type MemoryPreferenceStore } from "@/lib/memoryPreferenceState";

export interface MemoryPreferenceOwner {
  getSnapshot: () => string | null;
  subscribe: (notify: () => void) => () => void;
}

export interface CurrentOwner {
  key: string | null;
  isCurrent: () => boolean;
}

const OwnerContext = createContext<CurrentOwner | undefined>(undefined);
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
  const currentOwner = useMemo<CurrentOwner>(() => ({
    key: ownerKey,
    isCurrent: () => ownerKey !== null && owner.getSnapshot() === ownerKey,
  }), [owner, ownerKey]);
  const registry = useMemo(() => createMemoryPreferenceRegistry(owner.getSnapshot), [owner]);
  useLayoutEffect(() => {
    registry.setMounted(true);
    return () => { registry.setMounted(false); };
  }, [registry]);
  return (
    <OwnerContext value={currentOwner}>
      <PreferenceContext value={ownerKey === null ? null : registry.get(ownerKey)}>
        {children}
      </PreferenceContext>
    </OwnerContext>
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

export function useCurrentOwner(): CurrentOwner {
  const owner = useContext(OwnerContext);
  if (owner === undefined) throw new Error("Current user requires an owner-scoped provider.");
  return owner;
}

export function useMemoryPreferenceStore() {
  const store = useContext(PreferenceContext);
  if (store === undefined) {
    throw new Error("Memory preferences require an owner-scoped provider.");
  }
  return store;
}
