// Server-only: resolves the custom-tools / bring-your-own-MCP (Phase 12B) runtime
// configuration from environment variables. Read in the root layout (a server
// component) and passed as a plain prop into the client CustomToolsProvider,
// exactly like the Phase 9 auth config, the Phase 10 voice-live config, and the
// Phase 11B-2 library config — so the value is evaluated at request time in the
// container, NOT inlined at build time the way NEXT_PUBLIC_* vars are.
//
// Default OFF: unless CUSTOM_TOOLS_ENABLED is truthy this returns a disabled config
// and the browser never surfaces any custom-tools UI, so the app's default behavior
// is unchanged. The MCP-server API itself goes through the existing same-origin Next
// HTTP proxy (no public URL needed). Drive this from the same infra flag as the
// API's AI4IA_CUSTOM_TOOLS_ENABLED.
import type { CustomToolsConfig } from "./customTools";
import { parseEnabledFlag } from "./customTools";

const DISABLED: CustomToolsConfig = { enabled: false };

export function getCustomToolsConfig(): CustomToolsConfig {
  return parseEnabledFlag(process.env.CUSTOM_TOOLS_ENABLED)
    ? { enabled: true }
    : DISABLED;
}
