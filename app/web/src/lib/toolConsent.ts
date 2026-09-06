import type { InspectorSnapshot } from "./inspector";
import type { ToolConsentStatus, ToolConsentSummary } from "./types";

// Browser verification state, separate from the server-owned stored summary.
// null status means a refresh is in progress; unavailable means no verification.
export interface SessionConsentView {
  available: boolean | null;
  active: boolean;
  status: ToolConsentStatus | null;
  consent: ToolConsentSummary | null | undefined;
}

export type ToolConsentInspection =
  | { requestId: symbol; phase: "loading" }
  | { requestId: symbol; phase: "error" }
  | { requestId: symbol; phase: "ready"; value: InspectorSnapshot };

const STATUSES = new Set<string>(["off", "active", "expired", "revoked", "changed", "disabled", "unavailable"]);

export function unverifiedSessionConsent(status: "unavailable" | null = null): SessionConsentView {
  return { available: null, active: false, status, consent: undefined };
}

export function inspectedSessionConsent(snapshot: InspectorSnapshot): SessionConsentView {
  const status = snapshot.toolConsentStatus && STATUSES.has(snapshot.toolConsentStatus)
    ? snapshot.toolConsentStatus : "unavailable";
  const available = snapshot.toolAutoApproveAvailable === true;
  return {
    available,
    active: available && snapshot.toolConsentActive === true && status === "active" &&
      snapshot.toolConsent?.scope === "session",
    status,
    // Explicit null clears a prior grant; undefined preserves audit-only fallback
    // from an older server, but can never make active true.
    consent: snapshot.toolConsent,
  };
}
