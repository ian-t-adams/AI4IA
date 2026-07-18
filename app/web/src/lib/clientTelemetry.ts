// Lightweight, best-effort browser telemetry beacon. Bridges genuinely-uncaught
// script errors, unhandled promise rejections, and React render-boundary errors
// (see app/error.tsx) to the existing backend Application Insights pipeline via
// POST /api/client-events -> emit_custom_event(...) (see
// app/api/src/ai4ia_api/routers/client_events.py). Before this, those failures
// were only observable via user reports.
//
// This module never throws: every reporting path is wrapped so a telemetry
// failure can't break the app it's trying to observe. It intentionally sends
// only a short, capped message plus route/component labels -- no stack traces
// or arbitrary payloads -- so it can never leak PII/tokens into logs. The
// backend independently enforces the same field caps and rate-limits per user.
import { apiFetch } from "./auth";

// Mirrors the backend's ClientEventType literal
// (app/api/src/ai4ia_api/routers/client_events.py). Keep in sync.
export type ClientTelemetryEvent =
  | "render_error"
  | "unhandled_error"
  | "unhandled_rejection"
  | "media_playback_error"
  | "microphone_error";

export interface ClientEventDetails {
  message?: string | null;
  route?: string | null;
  component?: string | null;
}

const MAX_MESSAGE_LENGTH = 300;
const MAX_ROUTE_LENGTH = 200;
const MAX_COMPONENT_LENGTH = 100;
// Hard cap on reports per page load so a tight retry loop (e.g. a render error
// that keeps re-throwing) can't flood Application Insights or the rate limiter.
const MAX_REPORTS_PER_PAGE_LOAD = 20;

function truncate(value: string | null | undefined, max: number): string | undefined {
  const trimmed = value?.trim();
  if (!trimmed) return undefined;
  return trimmed.length > max ? `${trimmed.slice(0, max - 1)}…` : trimmed;
}

const reportedKeys = new Set<string>();
let reportCount = 0;

/**
 * Reports one client-side event to the backend telemetry bridge. Best-effort:
 * swallows all failures (network errors, auth not ready, backend down) so a
 * reporting failure never surfaces to the user or throws inside a catch/error
 * handler that's already handling a failure. De-dupes identical
 * (event, message) pairs and caps total reports per page load.
 */
export function reportClientEvent(
  event: ClientTelemetryEvent,
  details: ClientEventDetails = {},
): void {
  const message = truncate(details.message, MAX_MESSAGE_LENGTH);
  const key = `${event}|${message ?? ""}`;
  if (reportedKeys.has(key) || reportCount >= MAX_REPORTS_PER_PAGE_LOAD) return;
  reportedKeys.add(key);
  reportCount += 1;

  const body = {
    event,
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
    reportClientEvent("unhandled_error", {
      message: event.message || (event.error instanceof Error ? event.error.message : undefined),
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
    reportClientEvent("unhandled_rejection", {
      message,
      route: window.location.pathname,
    });
  });
}
