"use client";

// Phase 11B-2 document-library panel. Lets a signed-in user upload documents to
// their cross-session library, watch ingest status, pick an analyzer, and delete.
// Rendered only when the DOCUMENT_LIBRARY_ENABLED flag is on (gated by ChatApp),
// so it is inert by default.
import { useCallback, useEffect, useRef, useState } from "react";
import {
  deleteLibraryDocument,
  forgetLibraryDocumentFromMemory,
  listLibraryAnalyzers,
  listLibraryDocuments,
  listSharedWithMe,
  saveLibraryDocumentToMemory,
  uploadLibraryDocument,
} from "@/lib/api";
import type {
  LibraryAnalyzer,
  LibraryDocument,
  ShareVisibility,
} from "@/lib/library";
import { MediaPlayer } from "./MediaPlayer";
import AnnotationsPanel from "./AnnotationsPanel";
import SharePanel from "./SharePanel";

// Per-document "save to memory" UI state (Phase 11E-1, forget added 11E-3).
// Keyed by document id.
type MemorySave =
  | { status: "saving" }
  | { status: "saved"; saved: number }
  | { status: "forgetting" }
  | { status: "forgotten"; forgotten: number }
  | { status: "error"; error: string };

const STATUS_LABEL: Record<LibraryDocument["status"], string> = {
  pending: "Pending",
  stored: "Stored",
  analyzing: "Analyzing…",
  ready: "Ready",
  failed: "Failed",
};

const STATUS_COLOR: Record<LibraryDocument["status"], string> = {
  pending: "var(--fg-muted)",
  stored: "var(--fg-muted)",
  analyzing: "#0e7490",
  ready: "#15803d",
  failed: "#b91c1c",
};

// Documents in these states are still being ingested, so we poll for changes.
const IN_FLIGHT = new Set<LibraryDocument["status"]>([
  "pending",
  "stored",
  "analyzing",
]);

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function LibraryPanel({ onClose }: { onClose: () => void }) {
  const [docs, setDocs] = useState<LibraryDocument[]>([]);
  const [analyzers, setAnalyzers] = useState<LibraryAnalyzer[]>([]);
  const [analyzerId, setAnalyzerId] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [memorySaves, setMemorySaves] = useState<Record<string, MemorySave>>(
    {},
  );
  // Phase 11D: the audio/video document currently open in the deep-link player.
  const [playing, setPlaying] = useState<LibraryDocument | null>(null);
  const [annotating, setAnnotating] = useState<LibraryDocument | null>(null);
  // Phase 11F: the document whose sharing dialog is open, and the documents
  // others have shared with this user.
  const [sharing, setSharing] = useState<LibraryDocument | null>(null);
  const [sharedWithMe, setSharedWithMe] = useState<LibraryDocument[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);

  const refresh = useCallback(async () => {
    try {
      const list = await listLibraryDocuments();
      if (mountedRef.current) setDocs(list);
    } catch (e) {
      if (mountedRef.current) setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    (async () => {
      try {
        const [list, analyzerList, shared] = await Promise.all([
          listLibraryDocuments(),
          listLibraryAnalyzers().catch(() => [] as LibraryAnalyzer[]),
          listSharedWithMe().catch(() => [] as LibraryDocument[]),
        ]);
        if (!mountedRef.current) return;
        setDocs(list);
        setAnalyzers(analyzerList);
        setSharedWithMe(shared);
      } catch (e) {
        if (mountedRef.current) setError((e as Error).message);
      } finally {
        if (mountedRef.current) setLoading(false);
      }
    })();
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Poll while any document is still being ingested.
  useEffect(() => {
    if (!docs.some((d) => IN_FLIGHT.has(d.status))) return;
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [docs, refresh]);

  async function onFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    try {
      for (const file of Array.from(files)) {
        await uploadLibraryDocument(file, analyzerId || null);
      }
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      if (mountedRef.current) setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function onDelete(doc: LibraryDocument) {
    if (!confirm(`Delete "${doc.filename}" from your library?`)) return;
    setError(null);
    try {
      await deleteLibraryDocument(doc.id);
      setDocs((prev) => prev.filter((d) => d.id !== doc.id));
    } catch (e) {
      setError((e as Error).message);
    }
  }

  async function onSaveToMemory(doc: LibraryDocument) {
    setMemorySaves((prev) => ({ ...prev, [doc.id]: { status: "saving" } }));
    try {
      const { saved } = await saveLibraryDocumentToMemory(doc.id);
      if (mountedRef.current)
        setMemorySaves((prev) => ({
          ...prev,
          [doc.id]: { status: "saved", saved },
        }));
    } catch (e) {
      if (mountedRef.current)
        setMemorySaves((prev) => ({
          ...prev,
          [doc.id]: { status: "error", error: (e as Error).message },
        }));
    }
  }

  async function onForgetFromMemory(doc: LibraryDocument) {
    if (!confirm(`Forget "${doc.filename}" from the assistant's memory?`)) return;
    setMemorySaves((prev) => ({ ...prev, [doc.id]: { status: "forgetting" } }));
    try {
      const { forgotten } = await forgetLibraryDocumentFromMemory(doc.id);
      if (mountedRef.current)
        setMemorySaves((prev) => ({
          ...prev,
          [doc.id]: { status: "forgotten", forgotten },
        }));
    } catch (e) {
      if (mountedRef.current)
        setMemorySaves((prev) => ({
          ...prev,
          [doc.id]: { status: "error", error: (e as Error).message },
        }));
    }
  }

  // Phase 11F: reflect a sharing change on the document's badge without refetch.
  function onShareChanged(docId: string, visibility: ShareVisibility) {
    setDocs((prev) =>
      prev.map((d) => (d.id === docId ? { ...d, visibility } : d)),
    );
  }

  return (
    <>
      <div
        role="dialog"
        aria-label="Document library"
        aria-modal="true"
        style={{
          position: "fixed",
          inset: 0,
          background: "rgba(0,0,0,0.45)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          zIndex: 50,
        }}
        onClick={onClose}
      >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(560px, 94vw)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
          maxHeight: "90vh",
          overflowY: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <h2 style={{ margin: 0, fontSize: "1.2em" }}>Document library</h2>
          <button
            onClick={onClose}
            aria-label="Close library"
            style={{
              border: "none",
              background: "transparent",
              color: "var(--fg)",
              fontSize: "1.2em",
            }}
          >
            ✕
          </button>
        </div>

        <p style={{ margin: 0, fontSize: "0.85em", color: "var(--fg-muted)" }}>
          Upload documents to your personal library. Once ready, the assistant
          can reference and cite them across all your chats. Use 🧠 to save a
          document&apos;s gist to durable memory for recall even when the library
          isn&apos;t queried.
        </p>

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {analyzers.length > 0 && (
            <label
              style={{ fontSize: "0.8em", color: "var(--fg-muted)", display: "flex", flexDirection: "column", gap: 4 }}
            >
              Analyzer
              <select
                value={analyzerId}
                onChange={(e) => setAnalyzerId(e.target.value)}
                style={{
                  padding: "8px 10px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                  color: "var(--fg)",
                }}
              >
                <option value="">Automatic (by file type)</option>
                {analyzers.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name}
                    {a.kind === "custom" ? " (custom)" : ""}
                  </option>
                ))}
              </select>
            </label>
          )}
          <input
            ref={fileRef}
            type="file"
            multiple
            disabled={uploading}
            onChange={(e) => onFiles(e.target.files)}
            style={{ fontSize: "0.85em" }}
          />
          {uploading && (
            <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
              Uploading…
            </span>
          )}
        </div>

        {error && (
          <div
            role="alert"
            style={{
              fontSize: "0.8em",
              color: "#b91c1c",
              border: "1px solid #b91c1c",
              borderRadius: 8,
              padding: "8px 10px",
            }}
          >
            {error}
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {loading ? (
            <span style={{ fontSize: "0.85em", color: "var(--fg-muted)" }}>
              Loading…
            </span>
          ) : docs.length === 0 ? (
            <span style={{ fontSize: "0.85em", color: "var(--fg-muted)" }}>
              No documents yet.
            </span>
          ) : (
            docs.map((doc) => (
              <div
                key={doc.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "10px 12px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontWeight: 600,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={doc.filename}
                  >
                    {doc.filename}
                  </div>
                  <div style={{ fontSize: "0.75em", color: "var(--fg-muted)" }}>
                    {formatSize(doc.size)}
                    {doc.status === "ready" && doc.chunkCount > 0
                      ? ` · ${doc.chunkCount} chunks`
                      : ""}
                    {(() => {
                      const m = memorySaves[doc.id];
                      if (!m) return null;
                      if (m.status === "saving")
                        return (
                          <span style={{ color: "#0e7490" }}> · saving to memory…</span>
                        );
                      if (m.status === "saved")
                        return (
                          <span style={{ color: "#15803d" }}>
                            {" "}
                            · saved {m.saved} to memory ✓
                          </span>
                        );
                      if (m.status === "forgetting")
                        return (
                          <span style={{ color: "#0e7490" }}> · forgetting…</span>
                        );
                      if (m.status === "forgotten")
                        return (
                          <span style={{ color: "#15803d" }}>
                            {" "}
                            · forgot {m.forgotten} from memory ✓
                          </span>
                        );
                      return (
                        <span style={{ color: "#b91c1c" }}> · {m.error}</span>
                      );
                    })()}
                  </div>
                </div>
                <span
                  style={{
                    fontSize: "0.75em",
                    fontWeight: 600,
                    color: STATUS_COLOR[doc.status],
                    whiteSpace: "nowrap",
                  }}
                >
                  {STATUS_LABEL[doc.status]}
                </span>
                {doc.visibility && doc.visibility !== "private" && (
                  <span
                    title={
                      doc.visibility === "public"
                        ? "Shared with anyone in your organization"
                        : "Shared with specific people"
                    }
                    style={{
                      fontSize: "0.68em",
                      fontWeight: 600,
                      color: "var(--accent)",
                      border: "1px solid var(--accent)",
                      borderRadius: 999,
                      padding: "1px 7px",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {doc.visibility === "public" ? "Org" : "Shared"}
                  </span>
                )}
                <button
                  onClick={() => setSharing(doc)}
                  aria-label={`Share ${doc.filename}`}
                  title="Share this document with specific people or your whole organization"
                  style={{
                    border: "none",
                    background: "transparent",
                    color: "var(--fg-muted)",
                    fontSize: "1em",
                    cursor: "pointer",
                  }}
                >
                  🔗
                </button>
                {doc.status === "ready" &&
                  (doc.modality === "audio" || doc.modality === "video") && (
                    <button
                      onClick={() => setPlaying(doc)}
                      aria-label={`Play ${doc.filename}`}
                      title="Play this media and jump to detected scenes & keyframes"
                      style={{
                        border: "none",
                        background: "transparent",
                        color: "var(--fg-muted)",
                        fontSize: "1em",
                        cursor: "pointer",
                      }}
                    >
                      ▶️
                    </button>
                  )}
                {doc.status === "ready" && (
                  <button
                    onClick={() => onSaveToMemory(doc)}
                    disabled={
                      memorySaves[doc.id]?.status === "saving" ||
                      memorySaves[doc.id]?.status === "forgetting"
                    }
                    aria-label={`Save ${doc.filename} to memory`}
                    title="Save this document's gist to durable memory so the assistant can recall it across chats"
                    style={{
                      border: "none",
                      background: "transparent",
                      color: "var(--fg-muted)",
                      fontSize: "1em",
                      cursor:
                        memorySaves[doc.id]?.status === "saving" ||
                        memorySaves[doc.id]?.status === "forgetting"
                          ? "default"
                          : "pointer",
                    }}
                  >
                    🧠
                  </button>
                )}
                {doc.status === "ready" && (
                  <button
                    onClick={() => onForgetFromMemory(doc)}
                    disabled={
                      memorySaves[doc.id]?.status === "saving" ||
                      memorySaves[doc.id]?.status === "forgetting"
                    }
                    aria-label={`Forget ${doc.filename} from memory`}
                    title="Forget everything this document contributed to durable memory"
                    style={{
                      border: "none",
                      background: "transparent",
                      color: "var(--fg-muted)",
                      fontSize: "1em",
                      cursor:
                        memorySaves[doc.id]?.status === "saving" ||
                        memorySaves[doc.id]?.status === "forgetting"
                          ? "default"
                          : "pointer",
                    }}
                  >
                    🧽
                  </button>
                )}
                <button
                  onClick={() => setAnnotating(doc)}
                  aria-label={`Notes for ${doc.filename}`}
                  title="Private notes pinned to this document (not shared with the assistant)"
                  style={{
                    border: "none",
                    background: "transparent",
                    color: "var(--fg-muted)",
                    fontSize: "1em",
                    cursor: "pointer",
                  }}
                >
                  📝
                </button>
                <button
                  onClick={() => onDelete(doc)}
                  aria-label={`Delete ${doc.filename}`}
                  style={{
                    border: "none",
                    background: "transparent",
                    color: "var(--fg-muted)",
                    fontSize: "1em",
                    cursor: "pointer",
                  }}
                >
                  🗑
                </button>
              </div>
            ))
          )}
        </div>

        {sharedWithMe.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <h3
              style={{
                margin: 0,
                fontSize: "0.95em",
                borderTop: "1px solid var(--border)",
                paddingTop: 14,
              }}
            >
              Shared with you
            </h3>
            <p
              style={{ margin: 0, fontSize: "0.78em", color: "var(--fg-muted)" }}
            >
              Documents other people have shared with you. Your assistant can read
              and cite them too.
            </p>
            {sharedWithMe.map((doc) => (
              <div
                key={doc.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 12,
                  padding: "10px 12px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                }}
              >
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div
                    style={{
                      fontWeight: 600,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={doc.filename}
                  >
                    {doc.filename}
                  </div>
                  <div style={{ fontSize: "0.75em", color: "var(--fg-muted)" }}>
                    {formatSize(doc.size)}
                    {doc.status === "ready" && doc.chunkCount > 0
                      ? ` · ${doc.chunkCount} chunks`
                      : ""}
                    <span style={{ color: "var(--accent)" }}> · shared with you</span>
                  </div>
                </div>
                <span
                  style={{
                    fontSize: "0.75em",
                    fontWeight: 600,
                    color: STATUS_COLOR[doc.status],
                    whiteSpace: "nowrap",
                  }}
                >
                  {STATUS_LABEL[doc.status]}
                </span>
                {doc.status === "ready" &&
                  (doc.modality === "audio" || doc.modality === "video") && (
                    <button
                      onClick={() => setPlaying(doc)}
                      aria-label={`Play ${doc.filename}`}
                      title="Play this shared media and jump to detected scenes & keyframes"
                      style={{
                        border: "none",
                        background: "transparent",
                        color: "var(--fg-muted)",
                        fontSize: "1em",
                        cursor: "pointer",
                      }}
                    >
                      ▶️
                    </button>
                  )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
      {playing && (
        <MediaPlayer doc={playing} onClose={() => setPlaying(null)} />
      )}
      {annotating && (
        <AnnotationsPanel
          documentId={annotating.id}
          filename={annotating.filename}
          onClose={() => setAnnotating(null)}
        />
      )}
      {sharing && (
        <SharePanel
          documentId={sharing.id}
          filename={sharing.filename}
          onClose={() => setSharing(null)}
          onChanged={(visibility) => onShareChanged(sharing.id, visibility)}
        />
      )}
    </>
  );
}
