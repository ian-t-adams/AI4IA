// Server-only: resolves the document-library runtime configuration
// from environment variables. Read in the protected route-group layout (a server component) and
// passed as a plain prop into the client LibraryProvider, exactly like the auth
// and voice-live configs — so the value is evaluated at
// request time in the container, NOT inlined at build time the way NEXT_PUBLIC_*
// vars are.
//
// Default OFF: unless DOCUMENT_LIBRARY_ENABLED is truthy this returns a disabled
// config and the browser never surfaces any library UI, so the app's default
// behavior is unchanged. The library API itself goes through the existing Next
// HTTP proxy (no public URL needed, unlike the live-voice WebSocket).
import type { LibraryConfig } from "./library";
import { parseEnabledFlag } from "./envFlags";

const DISABLED: LibraryConfig = { enabled: false };

export function getLibraryConfig(): LibraryConfig {
  const enabled = parseEnabledFlag(process.env.DOCUMENT_LIBRARY_ENABLED);
  return enabled ? { enabled: true } : DISABLED;
}
