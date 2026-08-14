"use client";

// Owner-private notes pinned to a library document. These annotations
// are presentation-only — the API deliberately keeps them out of the model's
// retrieval/prompt context, so they are a safe, private place for the owner to jot
// reminders, page references, or timestamps. Rendered as its own modal launched
// from LibraryPanel; inert unless the document library flag is on.
import { useCallback, useEffect, useRef, useState } from "react";

import {
  createLibraryAnnotation,
  deleteLibraryAnnotation,
  listLibraryAnnotations,
  updateLibraryAnnotation,
} from "@/lib/api";
import type { DocumentAnnotation } from "@/lib/library";
import { ModalShell } from "./ModalShell";

interface AnnotationsPanelProps {
  documentId: string;
  filename: string;
  onClose: () => void;
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleString();
}

export default function AnnotationsPanel({
  documentId,
  filename,
  onClose,
}: AnnotationsPanelProps) {
  const [annotations, setAnnotations] = useState<DocumentAnnotation[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [draftBody, setDraftBody] = useState("");
  const [draftAnchor, setDraftAnchor] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editBody, setEditBody] = useState("");
  const [editAnchor, setEditAnchor] = useState("");
  const bodyRef = useRef<HTMLTextAreaElement>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setAnnotations(await listLibraryAnnotations(documentId));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load notes");
    } finally {
      setLoading(false);
    }
  }, [documentId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch-on-mount; setState only runs after the awaited call resolves
    void refresh();
  }, [refresh]);

  const handleAdd = useCallback(async () => {
    const body = draftBody.trim();
    if (!body || busy) return;
    setBusy(true);
    setError(null);
    try {
      await createLibraryAnnotation(documentId, body, draftAnchor.trim());
      setDraftBody("");
      setDraftAnchor("");
      await refresh();
      bodyRef.current?.focus();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to add note");
    } finally {
      setBusy(false);
    }
  }, [busy, documentId, draftAnchor, draftBody, refresh]);

  const startEdit = useCallback((note: DocumentAnnotation) => {
    setEditingId(note.id);
    setEditBody(note.body);
    setEditAnchor(note.anchor);
  }, []);

  const handleSaveEdit = useCallback(async () => {
    if (!editingId || busy) return;
    const body = editBody.trim();
    if (!body) return;
    setBusy(true);
    setError(null);
    try {
      await updateLibraryAnnotation(documentId, editingId, {
        body,
        anchor: editAnchor.trim(),
      });
      setEditingId(null);
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save note");
    } finally {
      setBusy(false);
    }
  }, [busy, documentId, editAnchor, editBody, editingId, refresh]);

  const handleDelete = useCallback(
    async (id: string) => {
      if (busy) return;
      const note = annotations.find((item) => item.id === id);
      const preview = note?.body.trim().slice(0, 80) || "this note";
      if (
        !window.confirm(
          `Permanently delete "${preview}"? This can't be undone.`,
        )
      ) {
        return;
      }
      setBusy(true);
      setError(null);
      try {
        await deleteLibraryAnnotation(documentId, id);
        if (editingId === id) setEditingId(null);
        await refresh();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to delete note");
      } finally {
        setBusy(false);
      }
    },
    [annotations, busy, documentId, editingId, refresh],
  );

  return (
    <ModalShell
      ariaLabel={`Notes for ${filename}`}
      title="📝 Notes"
      filename={filename}
      closeLabel="Close notes"
      onClose={onClose}
    >
      <p style={{ margin: 0, fontSize: "0.8em", color: "var(--fg-muted)" }}>
        Private notes pinned to this document. They&apos;re only visible to you and
        are never shared with the assistant.
      </p>

      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <label className="visually-hidden" htmlFor="annotation-body">
            Note
          </label>
          <textarea
            id="annotation-body"
            ref={bodyRef}
            value={draftBody}
            onChange={(e) => setDraftBody(e.target.value)}
            placeholder="Add a note…"
            rows={3}
            maxLength={4000}
            style={{
              padding: "8px 10px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "var(--bg)",
              color: "var(--fg)",
              resize: "vertical",
              fontFamily: "inherit",
              fontSize: "0.9em",
            }}
          />
          <label className="visually-hidden" htmlFor="annotation-anchor">
            Note anchor (optional)
          </label>
          <input
            id="annotation-anchor"
            value={draftAnchor}
            onChange={(e) => setDraftAnchor(e.target.value)}
            placeholder="Anchor (optional) — e.g. p.3, 02:15, a quote"
            maxLength={200}
            style={{
              padding: "8px 10px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "var(--bg)",
              color: "var(--fg)",
              fontSize: "0.85em",
            }}
          />
          <button
            onClick={() => void handleAdd()}
            disabled={busy || draftBody.trim().length === 0}
            style={{
              alignSelf: "flex-start",
              padding: "6px 14px",
              borderRadius: 8,
              border: "none",
              background:
                busy || draftBody.trim().length === 0
                  ? "var(--border)"
                  : "var(--accent)",
              color: "var(--accent-fg)",
              fontSize: "0.85em",
              cursor:
                busy || draftBody.trim().length === 0 ? "default" : "pointer",
            }}
          >
            Add note
          </button>
      </div>

      {error && (
        <p style={{ margin: 0, fontSize: "0.8em", color: "var(--danger)" }}>{error}</p>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
          {loading ? (
            <p style={{ margin: 0, fontSize: "0.85em", color: "var(--fg-muted)" }}>
              Loading…
            </p>
          ) : annotations.length === 0 ? (
            <p style={{ margin: 0, fontSize: "0.85em", color: "var(--fg-muted)" }}>
              No notes yet.
            </p>
          ) : (
            annotations.map((note) => (
              <div
                key={note.id}
                style={{
                  border: "1px solid var(--border)",
                  borderRadius: 8,
                  padding: 12,
                  display: "flex",
                  flexDirection: "column",
                  gap: 6,
                }}
              >
                {editingId === note.id ? (
                  <>
                    <label className="visually-hidden" htmlFor={`annotation-edit-body-${note.id}`}>
                      Edit note
                    </label>
                    <textarea
                      id={`annotation-edit-body-${note.id}`}
                      value={editBody}
                      onChange={(e) => setEditBody(e.target.value)}
                      rows={3}
                      maxLength={4000}
                      style={{
                        padding: "8px 10px",
                        borderRadius: 8,
                        border: "1px solid var(--border)",
                        background: "var(--bg)",
                        color: "var(--fg)",
                        resize: "vertical",
                        fontFamily: "inherit",
                        fontSize: "0.9em",
                      }}
                    />
                    <label className="visually-hidden" htmlFor={`annotation-edit-anchor-${note.id}`}>
                      Edit note anchor (optional)
                    </label>
                    <input
                      id={`annotation-edit-anchor-${note.id}`}
                      value={editAnchor}
                      onChange={(e) => setEditAnchor(e.target.value)}
                      placeholder="Anchor (optional)"
                      maxLength={200}
                      style={{
                        padding: "6px 10px",
                        borderRadius: 8,
                        border: "1px solid var(--border)",
                        background: "var(--bg)",
                        color: "var(--fg)",
                        fontSize: "0.85em",
                      }}
                    />
                    <div style={{ display: "flex", gap: 8 }}>
                      <button
                        onClick={() => void handleSaveEdit()}
                        disabled={busy || editBody.trim().length === 0}
                        style={{
                          padding: "4px 12px",
                          borderRadius: 8,
                          border: "none",
                          background: "var(--accent)",
                          color: "var(--accent-fg)",
                          fontSize: "0.8em",
                          cursor:
                            busy || editBody.trim().length === 0
                              ? "default"
                              : "pointer",
                        }}
                      >
                        Save
                      </button>
                      <button
                        onClick={() => setEditingId(null)}
                        style={{
                          padding: "4px 12px",
                          borderRadius: 8,
                          border: "1px solid var(--border)",
                          background: "transparent",
                          color: "var(--fg)",
                          fontSize: "0.8em",
                          cursor: "pointer",
                        }}
                      >
                        Cancel
                      </button>
                    </div>
                  </>
                ) : (
                  <>
                    {note.anchor && (
                      <span
                        style={{
                          fontSize: "0.7em",
                          color: "var(--accent)",
                          fontWeight: 600,
                        }}
                      >
                        @ {note.anchor}
                      </span>
                    )}
                    <p
                      style={{
                        margin: 0,
                        fontSize: "0.9em",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                    >
                      {note.body}
                    </p>
                    <div
                      style={{
                        display: "flex",
                        justifyContent: "space-between",
                        alignItems: "center",
                      }}
                    >
                      <span
                        style={{ fontSize: "0.7em", color: "var(--fg-muted)" }}
                      >
                        {formatTimestamp(note.updatedAt)}
                      </span>
                      <div style={{ display: "flex", gap: 6 }}>
                        <button
                          onClick={() => startEdit(note)}
                          disabled={busy}
                          aria-label="Edit note"
                          title="Edit note"
                          style={{
                            border: "none",
                            background: "transparent",
                            color: "var(--fg-muted)",
                            fontSize: "0.95em",
                            cursor: busy ? "default" : "pointer",
                          }}
                        >
                          ✏️
                        </button>
                        <button
                          onClick={() => void handleDelete(note.id)}
                          disabled={busy}
                          aria-label="Delete note"
                          title="Delete note"
                          style={{
                            border: "none",
                            background: "transparent",
                            color: "var(--fg-muted)",
                            fontSize: "0.95em",
                            cursor: busy ? "default" : "pointer",
                          }}
                        >
                          🗑
                        </button>
                      </div>
                    </div>
                  </>
                )}
              </div>
            ))
          )}
      </div>
    </ModalShell>
  );
}
