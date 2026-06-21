"use client";

// Voice Live config boundary. Mirrors the AuthProvider pattern:
// the root layout (server) reads the runtime env and passes a plain config object
// in as a prop; this client provider exposes it via context to the chat UI. In the
// default (disabled) config this is an inert passthrough — no live-voice control is
// ever rendered, so the app behaves exactly as before.
import { createContext, useContext } from "react";
import type { VoiceLiveConfig } from "@/lib/voiceLive";

const DISABLED: VoiceLiveConfig = {
  enabled: false,
  wsUrl: "",
  devUser: "",
  toolsAvailable: false,
};

const VoiceLiveContext = createContext<VoiceLiveConfig>(DISABLED);

export function VoiceLiveProvider({
  config,
  children,
}: {
  config: VoiceLiveConfig;
  children: React.ReactNode;
}) {
  return (
    <VoiceLiveContext.Provider value={config}>
      {children}
    </VoiceLiveContext.Provider>
  );
}

export function useVoiceLiveConfig(): VoiceLiveConfig {
  return useContext(VoiceLiveContext);
}
