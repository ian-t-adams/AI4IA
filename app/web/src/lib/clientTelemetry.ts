// Lightweight, best-effort browser telemetry beacon. Bridges genuinely-uncaught
// script errors, unhandled promise rejections, and React render-boundary errors
// (see app/error.tsx) to the existing backend Application Insights pipeline via
// POST /api/client-events -> emit_custom_event(...) (see
// app/api/src/ai4ia_api/routers/client_events.py). Before this, those failures
// were only observable via user reports.
//
// This module never throws: every reporting path is wrapped so a telemetry
// failure can't break the app it's trying to observe.
//
// Content-free by construction, not by sanitization. Earlier versions of this
// module sent free-text `message`/`route`/`component` fields through a regex
// redaction pass (percent-decoding, JSON-unescaping, auth-scheme matching,
// etc.) to strip credentials/PII before the beacon left the browser. Across
// several review rounds that sanitizer kept getting bypassed by a new
// encoding/nesting/quoting shape (URL-encoded delimiters, doubly-encoded
// delimiters, JSON-nested credentials, unterminated quotes, standalone
// scheme+credential pairs...) -- an unwinnable arms race, because the set of
// ways to obscure a substring from a regex is unbounded. The fix is to never
// accept free text at all: every field below is either a small fixed enum
// (`event`, `code`, `severity`) or a plain boolean. There is no string field
// wide enough to carry a credential, a URL, an email, or any other
// user/DOM-derived content, so there is nothing left for a sanitizer to have
// to catch. The backend (client_events.py) independently re-validates the
// same shape and rejects (422) anything outside it -- a modified/compromised
// client cannot smuggle a free-text field past that model no matter how it
// encodes it, which is what actually keeps this safe (not this file, which a
// compromised client could skip entirely).
import { apiFetch } from "./auth";

// Mirrors the backend's ClientEventType literal
// (app/api/src/ai4ia_api/routers/client_events.py). Keep in sync.
export type ClientTelemetryEvent =
  | "render_error"
  | "window_error"
  | "unhandled_rejection"
  | "media_playback_error"
  | "microphone_error"
  | "voice_playback_rebuffer";

// Mirrors the backend's ClientEventSeverity literal. Keep in sync. Every
// current call site reports a genuine failure, so "error" is both the
// default and, today, the only value actually sent -- "warning"/"info" exist
// so a future caller (e.g. a recovered-after-retry playback hiccup) has
// somewhere to report a non-fatal event without inventing a new event kind.
export type ClientEventSeverity = "error" | "warning" | "info";

// Mirrors the backend's _KNOWN_CODES allowlist (client_events.py). Keep in
// sync. Anything outside this set is normalized to "unknown" by
// `normalizeCode` below -- this field must never carry free text, since it's
// meant to be safe to slice/dice in App Insights without any redaction risk.
const KNOWN_CODES = new Set([
  "Error",
  "TypeError",
  "RangeError",
  "ReferenceError",
  "SyntaxError",
  "URIError",
  "EvalError",
  "AbortError",
  "NetworkError",
  "TimeoutError",
  "QuotaExceededError",
  "NotAllowedError",
  "NotFoundError",
  "NotSupportedError",
  "SecurityError",
  "DOMException",
  "string_rejection",
  "non_error_rejection",
]);

// Read-only export of the allowlist above, solely so tests can generate
// distinct-but-valid `code` fixtures (e.g. for de-dupe/cap tests) without
// duplicating this list as a second literal that could silently drift out of
// sync with `KNOWN_CODES`.
export const KNOWN_EVENT_CODES: readonly string[] = Object.freeze([...KNOWN_CODES]);

export interface ClientEventDetails {
  /** Stable JS/DOM error name (e.g. `error.name`). Normalized to "unknown"
   * unless it matches `KNOWN_CODES` -- never pass free text here. */
  code?: string | null;
  /** Defaults to "error" -- every current call site is a genuine failure. */
  severity?: ClientEventSeverity;
  /** Whether a correlatable id (e.g. Next.js's `error.digest`) was present
   * -- deliberately just the boolean, never the id value itself, since an
   * arbitrary string field is exactly what this schema avoids. */
  hasDigest?: boolean;
}

// Hard cap on reports per page load so a tight retry loop (e.g. a render
// error that keeps re-throwing) can't flood Application Insights or the rate
// limiter.
const MAX_REPORTS_PER_PAGE_LOAD = 20;

function normalizeCode(code: string | null | undefined): string {
  return code && KNOWN_CODES.has(code) ? code : "unknown";
}

const reportedKeys = new Set<string>();
let reportCount = 0;

/**
 * Reports one client-side event to the backend telemetry bridge. Best-effort:
 * swallows all failures (network errors, auth not ready, backend down) so a
 * reporting failure never surfaces to the user or throws inside a catch/error
 * handler that's already handling a failure. De-dupes identical
 * (event, code, severity, hasDigest) tuples and caps total reports per page
 * load -- since every field is now a small enum/boolean, that tuple fully
 * identifies the report's "shape"; there's no free-text detail left to
 * distinguish two reports of the same shape, which is the intended effect of
 * this schema, not a loss of information a sanitizer could have preserved
 * safely anyway.
 */
export function reportClientEvent(
  event: ClientTelemetryEvent,
  details: ClientEventDetails = {},
): void {
  const code = normalizeCode(details.code);
  const severity = details.severity ?? "error";
  const hasDigest = details.hasDigest ?? false;
  const key = `${event}|${code}|${severity}|${hasDigest}`;
  if (reportedKeys.has(key) || reportCount >= MAX_REPORTS_PER_PAGE_LOAD) return;
  reportedKeys.add(key);
  reportCount += 1;

  const body = { event, code, severity, hasDigest };

  apiFetch("/api/client-events", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {
    // Best-effort beacon: nothing to do if this fails.
  });
}

let installed = false;

/**
 * Installs window-level listeners that report genuinely-uncaught script
 * errors and unhandled promise rejections. Idempotent -- safe to call more
 * than once (e.g. from a React effect that re-runs) since only the first call
 * attaches listeners. No-ops outside the browser so it's safe to call
 * unconditionally from a client component's effect.
 *
 * Note: this does not retroactively cover errors that a component already
 * intercepts internally (e.g. `MediaRecorder.onerror` / `HTMLAudioElement.onerror`
 * handlers in the voice pipeline) -- those already-caught failures need to call
 * `reportClientEvent("microphone_error" | "media_playback_error", ...)`
 * themselves from within their existing handlers to be observable here.
 */
export function installGlobalClientTelemetry(): void {
  if (installed || typeof window === "undefined") return;
  installed = true;

  window.addEventListener("error", (event: ErrorEvent) => {
    const err = event.error instanceof Error ? event.error : undefined;
    reportClientEvent("window_error", { code: err?.name });
  });

  window.addEventListener("unhandledrejection", (event: PromiseRejectionEvent) => {
    const { reason } = event;
    const code =
      reason instanceof Error
        ? reason.name
        : typeof reason === "string"
          ? "string_rejection"
          : "non_error_rejection";
    reportClientEvent("unhandled_rejection", { code });
  });
}
