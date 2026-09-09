"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { apiErrorDetail, deleteSession, getSessionDeletion, listSessionDeletions, reconcileSessionDeletion } from "@/lib/api";
import type { DeletionPage, DeletionStatus } from "@/lib/types";
import { useCurrentOwner } from "./MemoryPreferenceProvider";
import { ModalShell } from "./ModalShell";
import { IncompleteConversationCreations } from "./IncompleteConversationCreations";

const buttonStyle: CSSProperties = {
  minHeight: 44, padding: "8px 12px", borderRadius: 8,
  border: "1px solid var(--border)", background: "var(--bg)", color: "var(--fg)",
};
const mutedStyle: CSSProperties = { margin: 0, color: "var(--fg-muted)", fontSize: "0.85em" };
const noPendingIds: ReadonlySet<string> = new Set();
const retryReasons: Record<NonNullable<DeletionStatus["retryReason"]>, string> = {
  storage_unavailable: "Storage is unavailable. Try Resume cleanup when it is available again.",
  cleanup_timeout: "The cleanup pass reached its time limit. Resume cleanup to request another pass.",
  concurrent_change: "Data changed during the last pass. Resume cleanup to check again.",
  integrity_mismatch: "The last pass found inconsistent deletion records. Contact your administrator.",
  uploads_unresolved: "Some uploads have no confirmed outcome. Cleanup cannot be verified yet.",
  artifact_store_required: "Required original-file storage is not configured. Contact your administrator.",
};

function Timestamp({ value }: { value: string }) {
  return <time dateTime={value}>{new Date(value).toLocaleString()}</time>;
}

function DeletionProgress({ status }: { status: DeletionStatus }) {
  if (status.state === "cleanup_verified") {
    return <>Cleanup last verified: {status.lastVerifiedAt
      ? <Timestamp value={status.lastVerifiedAt} /> : "timestamp unavailable"}.</>;
  }
  return <>{status.state === "retryable" ? "Retry needed" : "Cleanup pending"}.</>;
}

export function ConversationDeletionNotice({ status, onOpen, onDismiss }: {
  status: DeletionStatus; onOpen: () => void; onDismiss: () => void;
}) {
  return (
    <div style={{ padding: "10px max(16px, 6%)", borderBottom: "1px solid var(--border)", background: "var(--bg-elevated)" }}>
      <p role="status" style={{ margin: "0 0 8px", color: "var(--info)", fontSize: "0.9em" }}>
        Conversation removed from chats. <DeletionProgress status={status} />
      </p>
      <p style={{ ...mutedStyle, marginBottom: 8 }}>Last observed: <Timestamp value={status.updatedAt} />. Open deletion status for current progress.</p>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <button type="button" style={buttonStyle} onClick={onOpen}>View deletion status</button>
        <button type="button" style={buttonStyle} onClick={onDismiss} aria-label="Dismiss deletion notice">Dismiss</button>
      </div>
    </div>
  );
}

type RowAction = { error?: string; finished?: boolean };

function DeletionRow({ status, action, busy, reading, onResume }: {
  status: DeletionStatus; action?: RowAction; busy: boolean; reading: boolean; onResume: () => void;
}) {
  const uploadsUnresolved = status.pendingUploads.length > 0 || status.pendingUploadsTruncated;
  return (
    <li style={{ padding: "16px 0", borderTop: "1px solid var(--border)", overflowWrap: "anywhere" }}>
      <h3 style={{ margin: "0 0 6px", fontSize: "0.95em" }}>Conversation {status.sessionId}</h3>
      <p role="status" style={{ margin: "0 0 6px", color: status.state === "cleanup_verified" ? "var(--info)" : "var(--warn)" }}>
        Removed from chats. <DeletionProgress status={status} />
      </p>
      <p style={mutedStyle}>Last observed: <Timestamp value={status.updatedAt} />.</p>
      {status.retryReason && <p style={{ ...mutedStyle, marginTop: 6 }}>{retryReasons[status.retryReason]}</p>}
      {action?.error && <p role="alert" style={{ margin: "8px 0", color: "var(--danger)" }}>{action.error}</p>}
      {action?.finished && <p role="status" style={{ ...mutedStyle, marginTop: 8 }}>Cleanup pass finished. No automatic cleanup will follow.</p>}
      {status.state !== "cleanup_verified" && (
        <button
          type="button"
          disabled={reading || busy}
          aria-label={`Resume cleanup for ${status.sessionId}`}
          onClick={onResume}
          style={{ ...buttonStyle, marginTop: 10, background: "var(--accent)", color: "var(--accent-fg)" }}
        >
          {busy ? "Resuming cleanup..." : "Resume cleanup"}
        </button>
      )}
      <details style={{ marginTop: 8 }}>
        <summary tabIndex={0} style={{ minHeight: 44, padding: "10px 0", cursor: "pointer" }}>Progress details</summary>
        <p style={mutedStyle}>Requested: <Timestamp value={status.requestedAt} />. Phase: {status.phase}. Passes attempted: {status.attempts}.</p>
        <p style={mutedStyle}>
          Messages: {status.messagesVerified ? "verified" : "not verified"}; conversation documents: {status.documentsVerified ? "verified" : "not verified"}; inline originals: {status.attachmentsVerified ? "verified" : "not verified"}.
        </p>
        {status.state !== "cleanup_verified" && (
          <p style={mutedStyle}>Last verified: {status.lastVerifiedAt ? <Timestamp value={status.lastVerifiedAt} /> : "not yet verified"}.</p>
        )}
      </details>
      {uploadsUnresolved && (
        <details>
          <summary tabIndex={0} style={{ minHeight: 44, padding: "10px 0", cursor: "pointer" }}>Unresolved uploads ({status.pendingUploads.length}{status.pendingUploadsTruncated ? "+" : ""})</summary>
          <p style={mutedStyle}>These uploads have no confirmed outcome. They do not expire into verified cleanup.</p>
          <ul style={{ margin: "8px 0", paddingLeft: 20 }}>
            {status.pendingUploads.map((upload) => (
              <li key={upload.id} style={{ marginBottom: 8 }}>
                <div>Upload ID: <code>{upload.id}</code></div>
                <div>Document ID: <code>{upload.documentId}</code></div>
                <div>Started: <Timestamp value={upload.startedAt} /></div>
              </li>
            ))}
          </ul>
          {status.pendingUploadsTruncated && <p style={mutedStyle}>Showing a sample; more unresolved uploads remain.</p>}
        </details>
      )}
    </li>
  );
}

function DeletionRequests({ sessionId, isOpen, pendingIds, onResume, onDiscard, onShowAll }: {
  sessionId: string | null;
  isOpen: () => boolean;
  pendingIds: ReadonlySet<string>;
  onResume: (id: string) => Promise<DeletionStatus | undefined>;
  onDiscard: (id: string) => Promise<DeletionStatus | undefined>;
  onShowAll: () => void;
}) {
  const [page, setPage] = useState<DeletionPage | null>(null);
  const [reading, setReading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actions, setActions] = useState<Record<string, RowAction>>({});
  const [knownDiscards, setKnownDiscards] = useState<ReadonlyMap<string, {
    status: DeletionStatus; generation: number;
  }>>(new Map());
  const mutationRef = useRef(0);
  const mountedRef = useRef(false);
  const readRef = useRef<symbol | null>(null);
  const resumesRef = useRef(new Map<string, symbol>());
  const displayedItems = [
    ...[...knownDiscards.values()].map((entry) => entry.status),
    ...(page?.items ?? []).filter((item) => !knownDiscards.has(item.sessionId)),
  ];

  useLayoutEffect(() => {
    mountedRef.current = true;
    const resumes = resumesRef.current;
    return () => { mountedRef.current = false; readRef.current = null; resumes.clear(); };
  }, []);

  const load = useCallback(async (request: symbol, cursor: string | null) => {
    const isCurrent = () => mountedRef.current && isOpen() && readRef.current === request;
    if (!isCurrent()) return;
    const mutationAtStart = mutationRef.current;
    try {
      const result = sessionId
        ? { items: [await getSessionDeletion(sessionId)], hasMore: false, nextCursor: null }
        : await listSessionDeletions(cursor);
      if (!isCurrent()) return;
      if (result.hasMore && !result.nextCursor) throw new Error("The next deletion-status page is unavailable.");
      // An older page cannot erase a newly accepted discard. A subsequent read
      // may update that retained job, while its id still fences reservation reads.
      setKnownDiscards((current) => {
        const next = new Map(current);
        for (const status of result.items) {
          const previous = next.get(status.sessionId);
          if (previous && previous.generation <= mutationAtStart) {
            next.set(status.sessionId, { ...previous, status });
          }
        }
        return next;
      });
      setPage((current) => ({
        ...result,
        items: cursor && current
          ? [...new Map([...current.items, ...result.items].map((item) => [item.sessionId, item])).values()]
          : result.items,
      }));
    } catch (reason) {
      if (isCurrent()) setError(`Couldn't ${cursor ? "load more deletion requests" : "load deletion status"}. ${apiErrorDetail(reason)} Use Refresh status to try again; any progress shown is last observed.`);
    } finally {
      if (isCurrent()) { readRef.current = null; setReading(false); }
    }
  }, [isOpen, sessionId]);

  useEffect(() => {
    const request = Symbol();
    readRef.current = request;
    void load(request, null);
    return () => { if (readRef.current === request) readRef.current = null; };
  }, [load]);

  const read = (cursor: string | null) => {
    if (!isOpen() || pendingIds.size > 0 || resumesRef.current.size > 0) return;
    const request = Symbol();
    readRef.current = request;
    setReading(true);
    setError(null);
    if (!cursor) setActions({});
    void load(request, cursor);
  };

  const observeMutation = (status: DeletionStatus, discarded = false) => {
    const generation = ++mutationRef.current;
    setKnownDiscards((current) => discarded || current.has(status.sessionId)
      ? new Map(current).set(status.sessionId, { status, generation }) : current);
    setPage((current) => current && ({ ...current, items: current.items.map((item) => item.sessionId === status.sessionId ? status : item) }));
  };

  const discard = async (id: string): Promise<DeletionStatus | undefined> => {
    const status = await onDiscard(id);
    if (!mountedRef.current || !isOpen()) return status;
    if (!status || status.sessionId !== id) {
      throw new Error("The server did not confirm deletion status for this creation. Refresh both lists to check its state.");
    }
    observeMutation(status, true);
    return status;
  };

  const resume = async (id: string) => {
    if (!isOpen() || readRef.current || pendingIds.has(id) || resumesRef.current.has(id)) return;
    const request = Symbol();
    resumesRef.current.set(id, request);
    setActions((current) => ({ ...current, [id]: {} }));
    const isCurrent = () => mountedRef.current && isOpen() && resumesRef.current.get(id) === request;
    try {
      const status = await onResume(id);
      if (!status || !isCurrent()) return;
      observeMutation(status);
      setActions((current) => ({ ...current, [id]: { finished: true } }));
    } catch (reason) {
      if (isCurrent()) setActions((current) => ({
        ...current, [id]: { error: `Couldn't resume cleanup. ${apiErrorDetail(reason)} Use Refresh status to read current progress before trying again.` },
      }));
    } finally {
      if (isCurrent()) resumesRef.current.delete(id);
    }
  };

  return (
    <>
      <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
        <button type="button" style={buttonStyle} disabled={pendingIds.size > 0} onClick={() => read(null)}>Refresh status</button>
        {sessionId && <button type="button" style={buttonStyle} onClick={onShowAll}>All deletion requests</button>}
      </div>
      {reading && <p role="status" style={mutedStyle}>Loading deletion status...</p>}
      {error && <p role="alert" style={{ margin: 0, color: "var(--danger)" }}>{error}</p>}
      {!reading && !error && page && displayedItems.length === 0 && <p style={mutedStyle}>No resumable deletion requests found. Older deletions may not have retained status.</p>}
      {(page || knownDiscards.size > 0) && (
        <ul aria-label="Deletion requests" aria-busy={reading} style={{ listStyle: "none", margin: 0, padding: 0 }}>
          {displayedItems.map((status) => <DeletionRow key={status.sessionId} status={status} action={actions[status.sessionId]} busy={pendingIds.has(status.sessionId)} reading={reading} onResume={() => void resume(status.sessionId)} />)}
        </ul>
      )}
      {page?.hasMore && (
        <button type="button" style={buttonStyle} disabled={reading || pendingIds.size > 0} onClick={() => read(page.nextCursor)}>Load more deletion requests</button>
      )}
      {!sessionId && (
        <IncompleteConversationCreations isOpen={isOpen} pendingIds={pendingIds}
          discardedIds={new Set(knownDiscards.keys())} onDiscard={discard} />
      )}
    </>
  );
}

export function ConversationDeletionPanel({ open = true, sessionId = null, onShowAll, onClose }: {
  open?: boolean; sessionId?: string | null; onShowAll: () => void; onClose: () => void;
}) {
  const owner = useCurrentOwner();
  const activeRef = useRef(open);
  const mountedRef = useRef(false);
  // Keep only in-flight identifiers across close/reopen, not status or content.
  const requestsRef = useRef(new Map<string, symbol>());
  const [pending, setPending] = useState<ReadonlyMap<string, ReadonlySet<string>>>(new Map());
  useLayoutEffect(() => {
    mountedRef.current = true;
    const requests = requestsRef.current;
    return () => { mountedRef.current = false; requests.clear(); };
  }, []);
  useLayoutEffect(() => {
    activeRef.current = open;
    return () => { activeRef.current = false; };
  }, [open, owner]);
  const isOpen = useCallback(() => activeRef.current && owner.isCurrent(), [owner]);
  const run = useCallback(async (id: string, action: "resume" | "discard"): Promise<DeletionStatus | undefined> => {
    const ownerKey = owner.key;
    const key = JSON.stringify([ownerKey, id]);
    if (!isOpen() || ownerKey === null || requestsRef.current.has(key)) return undefined;
    const request = Symbol();
    requestsRef.current.set(key, request);
    setPending((current) => new Map(current).set(ownerKey, new Set([...(current.get(ownerKey) ?? []), id])));
    try {
      return await (action === "discard" ? deleteSession(id) : reconcileSessionDeletion(id));
    } finally {
      if (mountedRef.current && requestsRef.current.get(key) === request) {
        requestsRef.current.delete(key);
        setPending((current) => {
          const next = new Map(current);
          const ids = new Set(current.get(ownerKey));
          ids.delete(id);
          if (ids.size) next.set(ownerKey, ids);
          else next.delete(ownerKey);
          return next;
        });
      }
    }
  }, [isOpen, owner]);
  if (!open || owner.key === null) return null;

  return (
    <ModalShell ariaLabel="Conversation deletion status" title="Deletion status" closeLabel="Close deletion status" onClose={() => { activeRef.current = false; onClose(); }}>
      <p style={mutedStyle}>Deletion requests below are for conversations removed from chats. Status is last observed progress, not proof of physical erasure.</p>
      <p style={mutedStyle}>There is no automatic cleanup. Resume cleanup requests one bounded pass; opening or refreshing this panel only reads status. Closing it does not undo a request already sent.</p>
      <details>
        <summary tabIndex={0} style={{ minHeight: 44, padding: "10px 0", cursor: "pointer" }}>What this status covers</summary>
        <p style={mutedStyle}>Verification covers conversation content and inline originals only. It does not cover backups, provider sandboxes, library documents, memories, or generated and processed media. Minimal deletion records and write fences are retained indefinitely.</p>
      </details>
      <DeletionRequests key={JSON.stringify([owner.key, sessionId])} sessionId={sessionId} isOpen={isOpen}
        pendingIds={pending.get(owner.key) ?? noPendingIds} onResume={(id) => run(id, "resume")}
        onDiscard={(id) => run(id, "discard")} onShowAll={onShowAll} />
    </ModalShell>
  );
}
