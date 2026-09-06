"use client";

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import * as api from "@/lib/api";
import { isCurrentSessionGeneration } from "@/lib/sessionMutation";
import type { Session, ToolConsentSummary } from "@/lib/types";

// The clock is an external store: SSR starts conservatively off, hydration reads
// the real expiry, and a timer/focus notification updates even an idle chat.
export function useToolConsentActive(consent: ToolConsentSummary | null | undefined): boolean {
  const expiresAt = consent?.expiresAt;
  const subscribe = useCallback((notify: () => void) => {
    if (!expiresAt) return () => {};
    const delay = Date.parse(expiresAt) - Date.now();
    const timer = Number.isFinite(delay) && delay > 0
      ? window.setTimeout(notify, Math.min(delay + 1, 2_147_483_647))
      : undefined;
    window.addEventListener("focus", notify);
    document.addEventListener("visibilitychange", notify);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("focus", notify);
      document.removeEventListener("visibilitychange", notify);
    };
  }, [expiresAt]);
  const getSnapshot = useCallback(
    () => Boolean(expiresAt && Date.parse(expiresAt) > Date.now()),
    [expiresAt],
  );
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}

interface MutationState {
  sessionId: string | null;
  pending: boolean;
  error: string | null;
}

export function useSessionToolConsentMutation(
  sessionId: string | null,
  onUpdated: (session: Session) => void,
  { onStart, onSettled }: { onStart?: () => void; onSettled?: () => void } = {},
) {
  const [state, setState] = useState<MutationState>({ sessionId, pending: false, error: null });
  // Reset keyed UI state during render, rather than displaying another session's
  // busy/error state for a frame or clearing a newer request in an effect.
  if (state.sessionId !== sessionId) {
    setState({ sessionId, pending: false, error: null });
  }
  const owner = useRef({ sessionId, generation: 0, active: true, pending: false });
  useEffect(() => {
    const binding = { sessionId, generation: owner.current.generation + 1, active: true, pending: false };
    owner.current = binding;
    return () => { binding.active = false; };
  }, [sessionId]);

  const change = useCallback(async (enabled: boolean) => {
    const captured = owner.current;
    if (!sessionId || captured.sessionId !== sessionId || !captured.active || captured.pending) return;
    captured.pending = true;
    const isCurrent = () => captured.active && isCurrentSessionGeneration(
      sessionId, captured.generation, owner.current.sessionId, owner.current.generation,
    );
    setState({ sessionId, pending: true, error: null });
    try {
      onStart?.();
      const updated = await api.setSessionToolConsent(sessionId, enabled);
      // A -> B -> A and unmounts invalidate the old owner, not just the id.
      if (!isCurrent() || updated.id !== sessionId) return;
      onUpdated(updated);
    } catch (reason) {
      if (isCurrent()) {
        setState({ sessionId, pending: true, error: api.apiErrorDetail(reason) });
      }
    } finally {
      captured.pending = false;
      if (isCurrent()) {
        setState((current) => ({ ...current, pending: false }));
        onSettled?.();
      }
    }
  }, [sessionId, onUpdated, onStart, onSettled]);

  return { change, pending: state.sessionId === sessionId && state.pending, error: state.sessionId === sessionId ? state.error : null };
}
