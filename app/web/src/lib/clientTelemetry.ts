// Lightweight, best-effort browser telemetry beacon. Bridges genuinely-uncaught
// script errors, unhandled promise rejections, and React render-boundary errors
// (see app/error.tsx) to the existing backend Application Insights pipeline via
// POST /api/client-events -> emit_custom_event(...) (see
// app/api/src/ai4ia_api/routers/client_events.py). Before this, those failures
// were only observable via user reports.
//
// This module never throws: every reporting path is wrapped so a telemetry
// failure can't break the app it's trying to observe. `code` is a small,
// stable, allowlisted classification (e.g. "TypeError", "NotAllowedError")
// derived from the underlying JS/DOM error where available -- it can never
// carry free text. The free-text fields (`message`/`route`/`component`) are
// redacted for common secret/PII shapes (tokens, URLs, emails, GUIDs) before
// they ever leave the browser, then bounded/capped. This is defense-in-depth,
// not the only layer: the backend (client_events.py) independently
// re-applies equivalent redaction and the same field caps, since a
// modified/compromised client could skip the redaction done here -- and
// enforces the per-user rate limit itself.
import { apiFetch } from "./auth";

// Mirrors the backend's ClientEventType literal
// (app/api/src/ai4ia_api/routers/client_events.py). Keep in sync.
export type ClientTelemetryEvent =
  | "render_error"
  | "unhandled_error"
  | "unhandled_rejection"
  | "media_playback_error"
  | "microphone_error";

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

export interface ClientEventDetails {
  message?: string | null;
  /** Stable JS/DOM error name (e.g. `error.name`). Normalized to "unknown"
   * unless it matches `KNOWN_CODES` -- never pass free text here. */
  code?: string | null;
  route?: string | null;
  component?: string | null;
}

const MAX_MESSAGE_LENGTH = 300;
const MAX_ROUTE_LENGTH = 200;
const MAX_COMPONENT_LENGTH = 100;
// Hard cap on reports per page load so a tight retry loop (e.g. a render error
// that keeps re-throwing) can't flood Application Insights or the rate limiter.
const MAX_REPORTS_PER_PAGE_LOAD = 20;

// Redacts common secret/PII shapes from free text. Mirrors the backend's
// _REDACTIONS list (client_events.py's _sanitize) -- keep the two in sync.
// Order matters: broader patterns (JWTs, URLs) run before the generic
// long-opaque-token catch-all so a match isn't partially double-redacted.
const REDACTIONS: Array<[RegExp, string]> = [
  [/\b[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b/g, "[redacted-token]"],
  [
    // The optional (?:scheme\s+)? group consumes an HTTP auth scheme word
    // (e.g. "Authorization: Basic <credential>") together with the
    // credential that follows it, as ONE match. Without it, `[^\s"&,]+`
    // alone greedily stops at the first whitespace and matches just the
    // scheme word -- redacting "Basic"/"Bearer" while leaving the actual
    // credential completely untouched afterward.
    //
    // Two more shapes handled here (regression coverage for a follow-up
    // review round): a leading/trailing `"?` around the label so a
    // JSON-serialized key like `{"Authorization":"Basic <cred>"}` -- where a
    // closing key-quote sits between the label and the `:` -- still matches
    // and both quotes are consumed (not left dangling in the output); and a
    // `"[^"]*"` alternative tried before the bare-token one so a *quoted*
    // credential is matched as one atomic unit even when the scheme word
    // right before it is unquoted, e.g. `Authorization: Basic "<cred>"`
    // (previously the bare-token alternative excludes `"`, so backtracking
    // gave up on the optional scheme-word match and matched only "Basic",
    // leaving the quoted credential completely exposed).
    /"?\b(authorization|bearer|token|api[_-]?key|secret|password|access[_-]?key|sas)\b"?\s*[:=]\s*(?:(?:basic|bearer|digest|negotiate|ntlm|oauth)\s+)?(?:"[^"]*"|[^\s"&,]+)/gi,
    "$1=[redacted]",
  ],
  [
    // Standalone scheme+credential with no "Authorization"/"token"-style
    // label at all (e.g. bare `Bearer <cred>`, `Basic: <cred>`, a
    // punctuation-adjacent `(Bearer "<cred>")`, or a JSON-nested
    // `["Bearer <cred>"]`) -- distinct from the pattern above, which requires
    // a label word before the scheme. `Basic`/`Digest`/`Negotiate`/`NTLM`/
    // `OAuth` are never label words above, so without this they pass through
    // untouched whenever they're not preceded by "Authorization:" et al.
    // Gated on a `:`/`=`/quote signal (or, failing that, at least a 2+ char
    // unquoted token after mandatory whitespace) so an incidental trailing
    // punctuation mark isn't mistaken for a credential; this can still
    // over-redact rare prose like "Bearer bonds", an accepted tradeoff for a
    // security sanitizer where false negatives (a leaked credential) are far
    // costlier than false positives (a little lost diagnostic text).
    /\b(basic|bearer|digest|negotiate|ntlm|oauth)\b(?:\s*[:=]\s*(?:"[^"]*"|[^\s"&,]+)|\s+(?:"[^"]*"|[^\s"&,]{2,}))/gi,
    "$1=[redacted]",
  ],
  [/https?:\/\/\S+/gi, "[redacted-url]"],
  [/[\w.+-]+@[\w-]+\.[\w.-]+/g, "[redacted-email]"],
  [/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "[redacted-id]"],
  [/\b[A-Za-z0-9+/_-]{24,}\b/g, "[redacted-token]"],
];

function sanitize(value: string): string {
  let result = value.replace(/[\r\n\t]+/g, " ");
  for (const [pattern, replacement] of REDACTIONS) {
    result = result.replace(pattern, replacement);
  }
  return result.trim();
}

function truncate(value: string | null | undefined, max: number): string | undefined {
  const trimmed = value?.trim();
  if (!trimmed) return undefined;
  // Sanitize before truncating: cutting a long value first could leave a
  // dangling half-redacted secret that no longer matches a full pattern.
  const sanitized = sanitize(trimmed);
  if (!sanitized) return undefined;
  return sanitized.length > max ? `${sanitized.slice(0, max - 1)}…` : sanitized;
}

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
 * (event, code, message) tuples and caps total reports per page load.
 */
export function reportClientEvent(
  event: ClientTelemetryEvent,
  details: ClientEventDetails = {},
): void {
  const message = truncate(details.message, MAX_MESSAGE_LENGTH);
  const code = normalizeCode(details.code);
  const key = `${event}|${code}|${message ?? ""}`;
  if (reportedKeys.has(key) || reportCount >= MAX_REPORTS_PER_PAGE_LOAD) return;
  reportedKeys.add(key);
  reportCount += 1;

  const body = {
    event,
    code,
    message,
    route: truncate(details.route, MAX_ROUTE_LENGTH),
    component: truncate(details.component, MAX_COMPONENT_LENGTH),
  };

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
    reportClientEvent("unhandled_error", {
      message: event.message || err?.message,
      code: err?.name,
      route: window.location.pathname,
    });
  });

  window.addEventListener("unhandledrejection", (event: PromiseRejectionEvent) => {
    const { reason } = event;
    const message =
      reason instanceof Error
        ? reason.message
        : typeof reason === "string"
          ? reason
          : "Unhandled promise rejection";
    const code =
      reason instanceof Error
        ? reason.name
        : typeof reason === "string"
          ? "string_rejection"
          : "non_error_rejection";
    reportClientEvent("unhandled_rejection", {
      message,
      code,
      route: window.location.pathname,
    });
  });
}
