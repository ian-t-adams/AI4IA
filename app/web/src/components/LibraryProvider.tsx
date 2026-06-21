"use client";

// Document-library config boundary. Mirrors the AuthProvider and
// VoiceLiveProvider pattern: the root layout (server) reads the runtime
// env and passes a plain config object in as a prop; this client provider exposes
// it via context to the chat UI. In the default (disabled) config this is an inert
// passthrough — no library control is ever rendered, so the app behaves exactly as
// before.
import { createContext, useContext } from "react";
import type { LibraryConfig } from "@/lib/library";

const DISABLED: LibraryConfig = { enabled: false };

const LibraryContext = createContext<LibraryConfig>(DISABLED);

export function LibraryProvider({
  config,
  children,
}: {
  config: LibraryConfig;
  children: React.ReactNode;
}) {
  return (
    <LibraryContext.Provider value={config}>{children}</LibraryContext.Provider>
  );
}

export function useLibraryConfig(): LibraryConfig {
  return useContext(LibraryContext);
}
