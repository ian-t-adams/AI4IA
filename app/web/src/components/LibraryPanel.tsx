"use client";

// Document-library panel. Lets a signed-in user upload documents to
// their cross-session library, watch ingest status, pick an analyzer, and delete.
// Rendered only when the DOCUMENT_LIBRARY_ENABLED flag is on (gated by ChatApp),
// so it is inert by default.
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  deleteLibraryDocument,
  forgetLibraryDocumentFromMemory,
  getLibraryAnalysis,
  listLibraryAnalyzers,
  listLibraryDocuments,
  listSharedWithMe,
  saveLibraryDocumentToMemory,
  uploadLibraryDocument,
} from "@/lib/api";
import {
  formatBytes,
  keepMonotonicLibraryDocument,
  LIBRARY_STATUS_COLORS,
  LIBRARY_STATUS_LABELS,
  type LibraryAnalyzer,
  type LibraryAnalysisDetails,
  type LibraryDocument,
  type ShareVisibility,
} from "@/lib/library";
import { MediaPlayer } from "./MediaPlayer";
import AnnotationsPanel from "./AnnotationsPanel";
import { ModalShell } from "./ModalShell";
import SharePanel from "./SharePanel";
import { useModalFocus, useModalKeyDown } from "./useModalFocus";

// Per-document "save to memory" UI state, including forget.
// Keyed by document id.
type MemorySave =
  | { status: "saving" }
  | { status: "saved"; saved: number }
  | { status: "forgetting" }
  | { status: "forgotten"; forgotten: number }
  | { status: "error"; error: string };

// Documents in these states are still being ingested, so we poll for changes.
const IN_FLIGHT = new Set<LibraryDocument["status"]>([
  "pending",
  "stored",
  "analyzing",
]);

function analysisLabel(document: LibraryDocument): string | null {
  if (!document.analysisProvider) return null;
  const provider =
    document.analysisProvider === "content_understanding"
      ? "Content Understanding"
      : document.analysisProvider === "mistral"
        ? "Mistral"
        : document.analysisProvider;
  const model =
    document.analysisModel &&
    document.analysisModel !== "content-understanding"
      ? ` · ${document.analysisModel}`
      : "";
  const pages =
    document.analysisPages !== null &&
    document.analysisPages !== undefined
      ? ` · ${document.analysisPages} page${document.analysisPages === 1 ? "" : "s"}`
      : "";
  const location = document.analysisRegion
    ? ` · ${document.analysisRegion}${
        document.analysisResidency ? ` (${document.analysisResidency})` : ""
      }`
    : "";
  return `${provider}${model}${pages}${location}`;
}

function LibraryDocumentRow({
  document,
  extraDetails,
  children,
}: {
  document: LibraryDocument;
  extraDetails?: ReactNode;
  children?: ReactNode;
}) {
  const analysis = analysisLabel(document);
  return (
    <div
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
          title={document.filename}
        >
          {document.filename}
        </div>
        <div style={{ fontSize: "0.75em", color: "var(--fg-muted)" }}>
          {formatBytes(document.size)}
          {document.status === "ready" && document.chunkCount > 0
            ? ` · ${document.chunkCount} chunks`
            : ""}
          {analysis ? ` · ${analysis}` : ""}
          {extraDetails}
        </div>
      </div>
      <span
        style={{
          fontSize: "0.75em",
          fontWeight: 600,
          color: LIBRARY_STATUS_COLORS[document.status],
          whiteSpace: "nowrap",
        }}
      >
        {LIBRARY_STATUS_LABELS[document.status]}
      </span>
      {children}
    </div>
  );
}

export function LibraryPanel({ onClose }: { onClose: () => void }) {
  const [docs, setDocs] = useState<LibraryDocument[]>([]);
  const [analyzers, setAnalyzers] = useState<LibraryAnalyzer[]>([]);
  const [analyzerId, setAnalyzerId] = useState<string>("");
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [uploadErrors, setUploadErrors] = useState<
    { filename: string; message: string }[]
  >([]);
  const [error, setError] = useState<string | null>(null);
  const [memorySaves, setMemorySaves] = useState<Record<string, MemorySave>>(
    {},
  );
  // The audio/video document currently open in the deep-link player.
  const [playing, setPlaying] = useState<LibraryDocument | null>(null);
  const [annotating, setAnnotating] = useState<LibraryDocument | null>(null);
  // The document whose sharing dialog is open, and the documents
  // others have shared with this user.
  const [sharing, setSharing] = useState<LibraryDocument | null>(null);
  const [analysisDetails, setAnalysisDetails] = useState<{
    document: LibraryDocument;
    details: LibraryAnalysisDetails;
  } | null>(null);
  const analysisModalRef = useModalFocus<HTMLDivElement>(
    analysisDetails !== null,
  );
  const onAnalysisKeyDown = useModalKeyDown<HTMLDivElement>(
    () => setAnalysisDetails(null),
    analysisDetails !== null,
  );
  const [sharedWithMe, setSharedWithMe] = useState<LibraryDocument[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);
  const mountedRef = useRef(true);
  const refreshGenerationRef = useRef(0);
  const selectedAnalyzer = useMemo(
    () => analyzers.find((analyzer) => analyzer.id === analyzerId) ?? null,
    [analyzerId, analyzers],
  );

  const refresh = useCallback(async () => {
    const generation = ++refreshGenerationRef.current;
    try {
      const list = await listLibraryDocuments();
      if (
        mountedRef.current &&
        generation === refreshGenerationRef.current
      ) {
        setDocs((current) => {
          const currentById = new Map(current.map((document) => [document.id, document]));
          return list.map((incoming) => {
            const previous = currentById.get(incoming.id);
            return previous
              ? keepMonotonicLibraryDocument(previous, incoming)
              : incoming;
          });
        });
      }
    } catch (e) {
      if (
        mountedRef.current &&
        generation === refreshGenerationRef.current
      ) {
        setError((e as Error).message);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    const generation = ++refreshGenerationRef.current;
    (async () => {
      try {
        const [list, analyzerList, shared] = await Promise.all([
          listLibraryDocuments(),
          listLibraryAnalyzers().catch(() => [] as LibraryAnalyzer[]),
          listSharedWithMe().catch(() => [] as LibraryDocument[]),
        ]);
        if (!mountedRef.current) return;
        if (generation === refreshGenerationRef.current) {
          setDocs((current) => {
            const currentById = new Map(current.map((document) => [document.id, document]));
            return list.map((incoming) => {
              const previous = currentById.get(incoming.id);
              return previous
                ? keepMonotonicLibraryDocument(previous, incoming)
                : incoming;
            });
          });
        }
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
      refreshGenerationRef.current += 1;
    };
  }, []);

  // Poll while any document is still being ingested.
  useEffect(() => {
    if (!docs.some((d) => IN_FLIGHT.has(d.status))) return;
    let polling = false;
    const t = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        await refresh();
      } finally {
        polling = false;
      }
    }, 3000);
    return () => clearInterval(t);
  }, [docs, refresh]);

  async function onFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    setUploading(true);
    setError(null);
    setUploadErrors([]);
    const failures: { filename: string; message: string }[] = [];
    try {
      for (const file of Array.from(files)) {
        try {
          const document = await uploadLibraryDocument(file, analyzerId || null);
          refreshGenerationRef.current += 1;
          if (mountedRef.current) {
            setDocs((current) => [
              ...current.filter((item) => item.id !== document.id),
              document,
            ]);
          }
        } catch (reason) {
          failures.push({
            filename: file.name,
            message: (reason as Error).message,
          });
        }
      }
      if (mountedRef.current) setUploadErrors(failures);
    } finally {
      if (mountedRef.current) setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function onDelete(doc: LibraryDocument) {
    if (
      !confirm(
        `Permanently delete "${doc.filename}"? This can't be undone — it removes the file and its extracted content from your library.`,
      )
    )
      return;
    setError(null);
    try {
      await deleteLibraryDocument(doc.id);
      refreshGenerationRef.current += 1;
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

  // Reflect a sharing change on the document's badge without refetch.
  function onShareChanged(docId: string, visibility: ShareVisibility) {
    setDocs((prev) =>
      prev.map((d) => (d.id === docId ? { ...d, visibility } : d)),
    );
  }

  return (
    <>
      <ModalShell
        ariaLabel="Document library"
        title="Document library"
        closeLabel="Close library"
        onClose={onClose}
        width="min(560px, 94vw)"
        zIndex={50}
        headingFontSize="1.2em"
        headerGap={0}
      >
        <p style={{ margin: 0, fontSize: "0.85em", color: "var(--fg-muted)" }}>
          Upload documents to your personal library. Once ready, the assistant
          can reference and cite them across all your chats. Use 🧠 to save a
          document&apos;s gist to durable memory for recall even when the library
          isn&apos;t queried.
        </p>

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {analyzers.length > 0 && (
            <div
              style={{ fontSize: "0.8em", color: "var(--fg-muted)", display: "flex", flexDirection: "column", gap: 4 }}
            >
              <label htmlFor="library-analyzer">Analyzer</label>
              <select
                id="library-analyzer"
                aria-describedby="library-analyzer-description"
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
                <option value="">Automatic · Content Understanding</option>
                <optgroup label="Azure Content Understanding">
                  {analyzers
                    .filter(
                      (analyzer) =>
                        analyzer.provider !== "mistral" && !analyzer.preview,
                    )
                    .map((analyzer) => (
                      <option key={analyzer.id} value={analyzer.id}>
                        {analyzer.name}
                        {analyzer.kind === "custom" ? " (custom)" : ""}
                      </option>
                    ))}
                </optgroup>
                <optgroup label="Azure Content Understanding preview">
                  {analyzers
                    .filter(
                      (analyzer) =>
                        analyzer.provider !== "mistral" && analyzer.preview,
                    )
                    .map((analyzer) => (
                      <option key={analyzer.id} value={analyzer.id}>
                        {analyzer.name}
                      </option>
                    ))}
                </optgroup>
                <optgroup label="Mistral">
                  {analyzers
                    .filter((analyzer) => analyzer.provider === "mistral")
                    .map((analyzer) => (
                      <option key={analyzer.id} value={analyzer.id}>
                        {analyzer.name}
                      </option>
                    ))}
                </optgroup>
              </select>
              <span id="library-analyzer-description">
                {selectedAnalyzer
                  ? selectedAnalyzer.description
                  : "Recommended default. Chooses the Azure Content Understanding analyzer that fits the file type, including audio and video."}
                {selectedAnalyzer?.provider === "mistral"
                  ? " PDF and image files only; maximum 30 pages and 30 MB. Page-based list-price usage is recorded after analysis."
                  : ""}
                {selectedAnalyzer?.preview
                  ? " Preview capability: no SLA; use the GA automatic analyzer for production-critical ingestion."
                  : ""}
                {selectedAnalyzer?.operation === "synchronous"
                  ? " Returns a terminal result in this upload request; maximum 10 MB and five PDF pages."
                  : ""}
              </span>
            </div>
          )}
          <input
            id="library-file-upload"
            ref={fileRef}
            type="file"
            multiple
            disabled={uploading}
            accept={
              selectedAnalyzer?.provider === "mistral"
                ? ".pdf,image/jpeg,image/png,image/webp"
                : undefined
            }
            onChange={(e) => onFiles(e.target.files)}
            style={{ fontSize: "0.85em" }}
          />
          <label className="visually-hidden" htmlFor="library-file-upload">
            Upload library documents
          </label>
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
              color: "var(--danger)",
              border: "1px solid var(--danger)",
              borderRadius: 8,
              padding: "8px 10px",
            }}
          >
            {error}
          </div>
        )}
        {uploadErrors.length > 0 && (
          <div role="alert" style={{ fontSize: "0.8em", color: "var(--danger)" }}>
            <p style={{ margin: "0 0 4px" }}>
              Some documents could not be uploaded:
            </p>
            <ul style={{ margin: 0, paddingLeft: 20 }}>
              {uploadErrors.map((failure) => (
                <li key={`${failure.filename}:${failure.message}`}>
                  {failure.filename}: {failure.message}
                </li>
              ))}
            </ul>
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
              <LibraryDocumentRow
                key={doc.id}
                document={doc}
                extraDetails={
                  <>
                    {doc.confidenceCount
                      ? ` · confidence avg ${(100 * (doc.averageConfidence ?? 0)).toFixed(1)}% (${doc.confidenceCount})`
                      : ""}
                    {doc.groundedFieldCount
                      ? ` · ${doc.groundedFieldCount} grounded`
                      : ""}
                    {(() => {
                      const memory = memorySaves[doc.id];
                      if (!memory) return null;
                      if (memory.status === "saving")
                        return (
                          <span style={{ color: "var(--info)" }}>
                            {" "}
                            · saving to memory…
                          </span>
                        );
                      if (memory.status === "saved")
                        return (
                          <span style={{ color: "var(--success)" }}>
                            {" "}
                            · saved {memory.saved} to memory ✓
                          </span>
                        );
                      if (memory.status === "forgetting")
                        return (
                          <span style={{ color: "var(--info)" }}>
                            {" "}
                            · forgetting…
                          </span>
                        );
                      if (memory.status === "forgotten")
                        return (
                          <span style={{ color: "var(--success)" }}>
                            {" "}
                            · forgot {memory.forgotten} from memory ✓
                          </span>
                        );
                      return (
                        <span style={{ color: "var(--danger)" }}>
                          {" "}
                          · {memory.error}
                        </span>
                      );
                    })()}
                  </>
                }
              >
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
                {doc.analysisDetailsAvailable && (
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        setAnalysisDetails({
                          document: doc,
                          details: await getLibraryAnalysis(doc.id),
                        });
                      } catch (reason) {
                        setError((reason as Error).message);
                      }
                    }}
                    aria-label={`Analysis details for ${doc.filename}`}
                    title="View confidence, grounding, signatures, metadata, usage, and filter details"
                    style={{
                      border: "none",
                      background: "transparent",
                      color: "var(--fg-muted)",
                      cursor: "pointer",
                    }}
                  >
                    Evidence
                  </button>
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
                  aria-label={`Permanently delete ${doc.filename}`}
                  title="Permanently delete this document and its extracted content"
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
              </LibraryDocumentRow>
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
              <LibraryDocumentRow
                key={doc.id}
                document={doc}
                extraDetails={
                  <span style={{ color: "var(--accent)" }}>
                    {" "}
                    · shared with you
                  </span>
                }
              >
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
              </LibraryDocumentRow>
            ))}
          </div>
        )}
      </ModalShell>
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
          // Remount per document: SharePanel's load effect deliberately does not
          // reset state in its body, so a fresh instance is what guarantees a
          // second document never inherits the first one's loaded ACL.
          key={sharing.id}
          documentId={sharing.id}
          filename={sharing.filename}
          onClose={() => setSharing(null)}
          onChanged={(visibility) => onShareChanged(sharing.id, visibility)}
        />
      )}
      {analysisDetails && (
        <div
          ref={analysisModalRef}
          onKeyDown={onAnalysisKeyDown}
          role="dialog"
          aria-modal="true"
          aria-label={`Analysis details for ${analysisDetails.document.filename}`}
          className="analysis-details-dialog"
        >
          <section>
            <div className="analysis-details-header">
              <h3>Analysis evidence</h3>
              <button
                type="button"
                onClick={() => setAnalysisDetails(null)}
                aria-label="Close analysis details"
              >
                Close
              </button>
            </div>
            <p className="inspector-note">
              {analysisDetails.document.analysisProvider} ·{" "}
              {analysisDetails.document.analysisCompletionModel ??
                analysisDetails.document.analysisModel} ·{" "}
              {analysisDetails.document.analysisOperation}
              {analysisDetails.document.analysisWorkflow
                ? ` · ${analysisDetails.document.analysisWorkflow} workflow`
                : ""}
            </p>
            {analysisDetails.details.detailsTruncated ? (
              <p className="inspector-note">
                Detailed field and element evidence exceeded the safe response
                cap. Usage, warnings, and filter records are preserved.
              </p>
            ) : null}
            <dl className="usage-grid">
              <div>
                <dt>Confidence fields</dt>
                <dd>{analysisDetails.document.confidenceCount ?? 0}</dd>
              </div>
              <div>
                <dt>Grounded fields</dt>
                <dd>{analysisDetails.document.groundedFieldCount ?? 0}</dd>
              </div>
              <div>
                <dt>Average confidence</dt>
                <dd>
                  {analysisDetails.document.averageConfidence == null
                    ? "Unavailable"
                    : `${(analysisDetails.document.averageConfidence * 100).toFixed(1)}%`}
                </dd>
              </div>
              <div>
                <dt>Content filter records</dt>
                <dd>{analysisDetails.document.contentFilterCount ?? 0}</dd>
              </div>
            </dl>
            <details>
              <summary>Structured fields</summary>
              <pre>{JSON.stringify(analysisDetails.details.fields, null, 2)}</pre>
            </details>
            <details>
              <summary>Usage</summary>
              <pre>{JSON.stringify(analysisDetails.details.usage, null, 2)}</pre>
            </details>
            <details>
              <summary>Document elements</summary>
              <pre>{JSON.stringify(analysisDetails.details.contents, null, 2)}</pre>
            </details>
          </section>
        </div>
      )}
    </>
  );
}
