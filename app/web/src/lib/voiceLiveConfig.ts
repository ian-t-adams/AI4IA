// Server-only: resolves the Voice Live runtime configuration from
// environment variables. Read in the protected route-group layout (a server component) and passed
// as plain props into the client VoiceLiveProvider, exactly like the auth config,
// so values are evaluated at request time in the container, NOT inlined at build
// time the way NEXT_PUBLIC_* vars are.
//
// Default OFF: unless VOICE_LIVE_ENABLED is truthy AND API_PUBLIC_URL is set, this
// returns a disabled config and the browser never surfaces any live-voice UI, so
// the app's default behavior is unchanged.
import type { VoiceLiveConfig } from "./voiceLive";

const DISABLED: VoiceLiveConfig = {
  enabled: false,
  wsUrl: "",
  devUser: "",
  toolsAvailable: false,
};

const TRUTHY = new Set(["1", "true", "yes", "on"]);

// The browser opens the live-voice WebSocket directly against the API's external
// ingress (the Next.js HTTP proxy can't proxy WebSockets), so we publish the API's
// public origin as a ws(s) URL. https -> wss, http -> ws.
function toWsUrl(apiPublicUrl: string): string {
  let u = apiPublicUrl.trim().replace(/\/+$/, "");
  if (u.startsWith("https://")) u = `wss://${u.slice("https://".length)}`;
  else if (u.startsWith("http://")) u = `ws://${u.slice("http://".length)}`;
  return `${u}/api/voice/live`;
}

export function getVoiceLiveConfig(): VoiceLiveConfig {
  const enabled = TRUTHY.has((process.env.VOICE_LIVE_ENABLED || "").toLowerCase());
  const apiPublicUrl = process.env.API_PUBLIC_URL || "";
  // Fail closed: a half-config (flag on but no public URL) stays disabled.
  if (!enabled || !apiPublicUrl) return DISABLED;
  return {
    enabled: true,
    wsUrl: toWsUrl(apiPublicUrl),
    // Only meaningful under the dev auth provider (the browser-direct WS can't be
    // proxy-stamped with X-Dev-User, so it carries the dev id in a subprotocol).
    // Ignored under Entra, where a real bearer token is used instead.
    devUser: process.env.DEV_USER || "",
    // Whether the API advertises governed tools for live sessions (mirrors the
    // API's AI4IA_REALTIME_TOOLS_ENABLED; infra emits both from one param). Default
    // OFF: when unset the panel never offers the tools opt-in.
    toolsAvailable: TRUTHY.has(
      (process.env.VOICE_LIVE_TOOLS_ENABLED || "").toLowerCase(),
    ),
  };
}
