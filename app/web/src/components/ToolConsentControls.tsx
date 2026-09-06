"use client";

import { useId } from "react";
import type { Session, ToolConsentStatus, ToolConsentSummary } from "@/lib/types";
import { useSessionToolConsentMutation, useToolConsentActive } from "./useSessionToolConsent";

export const TOOL_CONSENT_WARNING =
  "Hostile retrieved content can influence later tool calls. Auto-approval skips individual approval prompts, not authorization, destination, or budget checks. Activity, traces, and execution receipts are still recorded.";

const STATUS_TEXT: Record<ToolConsentStatus | "checking", string> = {
  active: "Auto-approval on for this session",
  off: "Session auto-approval is off",
  expired: "Session auto-approval expired",
  revoked: "Session auto-approval revoked",
  changed: "Session auto-approval needs renewal — tools or permissions changed",
  disabled: "Session auto-approval disabled by the operator",
  unavailable: "Session auto-approval unverified — status unavailable",
  checking: "Checking session auto-approval status…",
};

function effectiveStatus(
  consent: ToolConsentSummary | null | undefined,
  available: boolean | null,
  active: boolean,
  status: ToolConsentStatus | null,
  unexpired: boolean,
): ToolConsentStatus | "checking" {
  if (status === null) return "checking";
  if (status !== "active") return Object.hasOwn(STATUS_TEXT, status) ? status : "unavailable";
  if (!active || available !== true || consent?.scope !== "session") return "unavailable";
  // The server verified this grant, but the clock can expire it before the next
  // refresh. A stored summary or availability flag alone is never verification.
  return unexpired ? "active" : "expired";
}

export function ToolConsentDetails({ consent }: { consent: ToolConsentSummary }) {
  return (
    <p className="tool-consent-details">
      {consent.toolCount} enabled tool contract{consent.toolCount === 1 ? "" : "s"} in this consent (at grant time).
      {" "}Expires <time dateTime={consent.expiresAt}>{new Date(consent.expiresAt).toLocaleString()}</time>.
      {" "}Granted <time dateTime={consent.grantedAt}>{new Date(consent.grantedAt).toLocaleString()}</time>.
    </p>
  );
}

export function SessionToolConsentControls({
  sessionId, consent, available, active = false, status = null, canEnable, pending, error, onChange, onRefresh,
}: {
  sessionId: string | null;
  consent: ToolConsentSummary | null | undefined;
  available: boolean | null;
  active?: boolean;
  status?: ToolConsentStatus | null;
  canEnable: boolean;
  pending: boolean;
  error: string | null;
  onChange: (enabled: boolean) => Promise<void>;
  onRefresh?: () => void;
}) {
  const id = useId();
  const unexpired = useToolConsentActive(consent);
  const state = effectiveStatus(consent, available, active, status, unexpired);
  const enabled = Boolean(sessionId && state === "active");
  const mayGrant = available === true && state !== "checking" && state !== "unavailable" && state !== "disabled";
  if (sessionId && available !== true && !consent && !pending && !error) {
    return <div>
      <p className="inspector-note">{available === false
        ? "Session tool auto-approval is not enabled on this deployment." : STATUS_TEXT[state]}</p>
      {onRefresh && state === "unavailable" ? <button type="button" onClick={onRefresh}>Refresh consent status</button> : null}
    </div>;
  }
  return (
    <div className="tool-consent-controls" aria-busy={pending}>
      <div className="tool-consent-option">
        <input
          id={id}
          type="checkbox"
          checked={enabled}
          disabled={pending || (!enabled && (!sessionId || !canEnable || !mayGrant))}
          aria-describedby={`${id}-warning ${id}-scope`}
          onChange={(event) => void onChange(event.target.checked)}
        />
        <label htmlFor={id}>Auto-approve enabled tools for this session</label>
      </div>
      <p id={`${id}-warning`} className="tool-consent-warning">{TOOL_CONSENT_WARNING}</p>
      <p id={`${id}-scope`} className="inspector-note">
        {sessionId
          ? "Only this session's current tool contracts are covered, for at most 8 hours. New or changed tools need renewed consent. Workflow runs require their own opt-in. Revoke at any time, including while a response is generating."
          : "Start the conversation first, then explicitly enable auto-approval here. Typing or changing defaults does not create a session or grant consent."}
      </p>
      {sessionId ? <p role="status" className="tool-consent-state">
        {enabled ? "Auto-approval is enabled for this session."
          : state === "expired" ? "Session auto-approval has expired." : STATUS_TEXT[state]}
      </p> : null}
      {consent ? <>
        <ToolConsentDetails consent={consent} />
        <button type="button" disabled={pending} onClick={() => void onChange(false)}>
          {pending ? "Updating auto-approval…" : "Revoke session auto-approval"}
        </button>
        {mayGrant ? <button type="button" disabled={pending || !canEnable} onClick={() => void onChange(true)}>
          Renew consent for current enabled tools
        </button> : null}
      </> : pending ? <p role="status">Updating auto-approval…</p> : null}
      {sessionId && onRefresh && state !== "active" ? <button type="button" disabled={pending || state === "checking"} onClick={onRefresh}>
        Refresh consent status
      </button> : null}
      {error ? <p role="alert" className="inspector-error">{error} Refresh session settings to confirm the current consent.</p> : null}
    </div>
  );
}

export function SessionToolConsentBanner({
  sessionId, consent, available, active = false, status = null, onUpdated, onVerificationInvalidated, onRefresh,
}: {
  sessionId: string;
  consent: ToolConsentSummary;
  available: boolean | null;
  active?: boolean;
  status?: ToolConsentStatus | null;
  onUpdated: (session: Session) => void;
  onVerificationInvalidated?: () => void;
  onRefresh?: () => void;
}) {
  const unexpired = useToolConsentActive(consent);
  const mutation = useSessionToolConsentMutation(sessionId, onUpdated, {
    onStart: onVerificationInvalidated,
    onSettled: onRefresh,
  });
  const state = effectiveStatus(consent, available, active, status, unexpired);
  return (
    <aside className="tool-consent-banner" aria-label="Session tool auto-approval">
      <div>
        <p className="tool-consent-state" role="status">{STATUS_TEXT[state]}</p>
        <ToolConsentDetails consent={consent} />
        <p className="tool-consent-details">New or changed tools need renewed consent in Inspector. The server rechecks scope before every call.</p>
      </div>
      <button type="button" disabled={mutation.pending} onClick={() => void mutation.change(false)}>
        {mutation.pending ? "Revoking…" : "Revoke auto-approval"}
      </button>
      {onRefresh && state !== "active" ? <button type="button" disabled={mutation.pending || state === "checking"} onClick={onRefresh}>
        Refresh consent status
      </button> : null}
      {mutation.error ? <p role="alert">{mutation.error} Consent has not been confirmed revoked.</p> : null}
    </aside>
  );
}
