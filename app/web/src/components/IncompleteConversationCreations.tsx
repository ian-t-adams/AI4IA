"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { apiErrorDetail, listSessionInitializations } from "@/lib/api";
import type { DeletionStatus, InitializationPage } from "@/lib/types";

const buttonStyle: CSSProperties = {
  minHeight: 44, padding: "8px 12px", borderRadius: 8,
  border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)",
};
const mutedStyle: CSSProperties = { margin: 0, color: "var(--fg-muted)", fontSize: "0.85em" };

interface Props {
  isOpen: () => boolean;
  pendingIds: ReadonlySet<string>;
  discardedIds: ReadonlySet<string>;
  onDiscard: (id: string) => Promise<DeletionStatus | undefined>;
}

function InitializationReservations({ isOpen, pendingIds, discardedIds, onDiscard }: Props) {
  const [page, setPage] = useState<InitializationPage | null>(null);
  const [reading, setReading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const mountedRef = useRef(false);
  const readRef = useRef<symbol | null>(null);
  const discardsRef = useRef(new Set<string>());
  const refreshRef = useRef<HTMLButtonElement>(null);
  const items = page?.items.filter((item) => !discardedIds.has(item.sessionId)) ?? [];

  useLayoutEffect(() => {
    mountedRef.current = true;
    const discards = discardsRef.current;
    return () => { mountedRef.current = false; readRef.current = null; discards.clear(); };
  }, []);

  const load = useCallback(async (request: symbol, cursor: string | null) => {
    const isCurrent = () => mountedRef.current && isOpen() && readRef.current === request;
    if (!isCurrent()) return;
    try {
      const result = await listSessionInitializations(cursor);
      if (!isCurrent()) return;
      if (result.hasMore && !result.nextCursor) throw new Error("The next incomplete-creation page is unavailable.");
      setPage((current) => ({
        ...result,
        items: cursor && current
          ? [...new Map([...current.items, ...result.items].map((item) => [item.sessionId, item])).values()]
          : result.items,
      }));
    } catch (reason) {
      if (isCurrent()) setError(`Couldn't load incomplete conversation creations. ${apiErrorDetail(reason)} Use Refresh incomplete creations to try again.`);
    } finally {
      if (isCurrent()) { readRef.current = null; setReading(false); }
    }
  }, [isOpen]);

  useEffect(() => {
    const request = Symbol();
    readRef.current = request;
    void load(request, null);
    return () => { if (readRef.current === request) readRef.current = null; };
  }, [load]);

  const read = (cursor: string | null) => {
    if (!isOpen()) return;
    const request = Symbol();
    readRef.current = request;
    setReading(true);
    setError(null);
    void load(request, cursor);
  };

  const discard = async (id: string) => {
    if (!isOpen() || pendingIds.has(id) || discardedIds.has(id) || discardsRef.current.has(id)) return;
    if (!window.confirm(`Discard incomplete creation "${id}"? This cancels this creation and prevents it from publishing. If creation has already finished, this deletes that same conversation instead. Cleanup may remain pending and need Resume cleanup.`)) return;
    const trigger = document.activeElement;
    discardsRef.current.add(id);
    setErrors((current) => ({ ...current, [id]: "" }));
    const isCurrent = () => mountedRef.current && isOpen() && discardsRef.current.has(id);
    try {
      const status = await onDiscard(id);
      if (!isCurrent() || !status) return;
      if (document.activeElement === trigger || document.activeElement === document.body) refreshRef.current?.focus();
    } catch (reason) {
      if (isCurrent()) setErrors((current) => ({
        ...current, [id]: `Couldn't discard this incomplete creation. ${apiErrorDetail(reason)}`,
      }));
    } finally {
      if (isCurrent()) discardsRef.current.delete(id);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <p style={mutedStyle}>These reservations may still be in progress and are not usable chats. No title, instructions, or transcript is available here. Nothing is resumed or discarded automatically.</p>
      <button ref={refreshRef} type="button" style={buttonStyle} onClick={() => read(null)}>Refresh incomplete creations</button>
      {reading && <p role="status" style={mutedStyle}>Loading incomplete conversation creations...</p>}
      {error && <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>{error}</p>}
      {!reading && !error && page && items.length === 0 && (
        <p style={mutedStyle}>No incomplete creations reported. This observation is not completion evidence. If a creation failed, refresh later or ask an operator to investigate.</p>
      )}
      {page && (
        <ul aria-label="Incomplete conversation creations" aria-busy={reading} style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {items.map((item) => (
            <li key={item.sessionId} style={{ borderTop: "1px solid var(--border)", padding: "12px 0", overflowWrap: "anywhere" }}>
              <h3 style={{ fontSize: "0.95em", margin: "0 0 6px" }}>Creation {item.sessionId}</h3>
              <p style={mutedStyle}>Reserved: <time dateTime={item.createdAt}>{new Date(item.createdAt).toLocaleString()}</time>. May still be in progress.</p>
              {errors[item.sessionId] && <p role="alert" style={{ color: "var(--danger)", margin: "8px 0" }}>{errors[item.sessionId]}</p>}
              <button type="button" disabled={pendingIds.has(item.sessionId)} aria-label={`Discard incomplete creation ${item.sessionId}`}
                onClick={() => void discard(item.sessionId)} style={{ ...buttonStyle, color: "var(--danger)", marginTop: 8 }}>
                {pendingIds.has(item.sessionId) ? "Discarding..." : "Discard incomplete creation"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {page?.hasMore && <button type="button" style={buttonStyle} disabled={reading} onClick={() => read(page.nextCursor)}>Load more incomplete creations</button>}
    </div>
  );
}

export function IncompleteConversationCreations(props: Props) {
  const [expanded, setExpanded] = useState(false);
  return (
    <details onToggle={(event) => setExpanded(event.currentTarget.open)}>
      <summary tabIndex={0} style={{ minHeight: 44, padding: "10px 0", cursor: "pointer" }}>Incomplete conversation creation</summary>
      {expanded && <InitializationReservations {...props} />}
    </details>
  );
}
