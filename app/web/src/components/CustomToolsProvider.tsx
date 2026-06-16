"use client";

// Phase 12B custom-tools config boundary. Mirrors the Phase 9 AuthProvider /
// Phase 10 VoiceLiveProvider / Phase 11B-2 LibraryProvider pattern: the root layout
// (server) reads the runtime env and passes a plain config object in as a prop; this
// client provider exposes it via context to the chat UI. In the default (disabled)
// config this is an inert passthrough — no custom-tools control is ever rendered, so
// the app behaves exactly as before.
import { createContext, useContext } from "react";
import type { CustomToolsConfig } from "@/lib/customTools";

const DISABLED: CustomToolsConfig = { enabled: false };

const CustomToolsContext = createContext<CustomToolsConfig>(DISABLED);

export function CustomToolsProvider({
  config,
  children,
}: {
  config: CustomToolsConfig;
  children: React.ReactNode;
}) {
  return (
    <CustomToolsContext.Provider value={config}>
      {children}
    </CustomToolsContext.Provider>
  );
}

export function useCustomToolsConfig(): CustomToolsConfig {
  return useContext(CustomToolsContext);
}
