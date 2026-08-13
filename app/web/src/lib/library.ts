// Document library shared types. The runtime feature flag is surfaced to the
// browser via the server-env -> provider-prop pattern, NOT a NEXT_PUBLIC_* var,
// so it is evaluated at request time.

export interface LibraryConfig {
  // When false (default) the library UI is never rendered and nothing changes.
  enabled: boolean;
}

// Mirrors the API's UserDocumentSummary (no body, no acl).
export interface LibraryDocument {
  id: string;
  filename: string;
  contentType: string;
  size: number;
  modality: string;
  status: "pending" | "stored" | "analyzing" | "ready" | "failed";
  analyzerId: string | null;
  analysisProvider?: string | null;
  analysisModel?: string | null;
  analysisVersion?: string | null;
  analysisPages?: number | null;
  analysisDeployment?: string | null;
  analysisRegion?: string | null;
  analysisSku?: string | null;
  analysisDataZone?: string | null;
  analysisResidency?: string | null;
  analysisApiVersion?: string | null;
  analysisOperation?: string | null;
  analysisWorkflow?: string | null;
  analysisCompletionModel?: string | null;
  analysisUsage?: Record<string, unknown>;
  confidenceCount?: number;
  groundedFieldCount?: number;
  averageConfidence?: number | null;
  minimumConfidence?: number | null;
  contentFilterCount?: number;
  analysisDetailsAvailable?: boolean;
  summary: string;
  chunkCount: number;
  error?: string | null;
  citationReady?: boolean;
  visibility: string;
  createdAt: string;
  updatedAt: string;
}

const LIBRARY_STATUS_RANK: Record<LibraryDocument["status"], number> = {
  pending: 0,
  stored: 1,
  analyzing: 2,
  ready: 3,
  failed: 3,
};

export function keepMonotonicLibraryDocument(
  current: LibraryDocument,
  incoming: LibraryDocument,
): LibraryDocument {
  const currentUpdated = Date.parse(current.updatedAt);
  const incomingUpdated = Date.parse(incoming.updatedAt);
  if (Number.isFinite(currentUpdated) && Number.isFinite(incomingUpdated)) {
    return incomingUpdated > currentUpdated ? incoming : current;
  }
  return LIBRARY_STATUS_RANK[incoming.status] < LIBRARY_STATUS_RANK[current.status]
    ? current
    : incoming;
}

// Mirrors the API's Analyzer (built-ins are merged in server-side).
export interface LibraryAnalyzer {
  id: string;
  name: string;
  description: string;
  kind: "builtin" | "custom";
  provider?: "content_understanding" | "mistral";
  modelId?: string | null;
  modelVersion?: string | null;
  serviceAnalyzerId?: string | null;
  apiVersion?: string | null;
  operation?: "asynchronous" | "synchronous";
  preview?: boolean;
  modalities: string[];
  baseAnalyzerId: string | null;
}

export interface LibraryAnalysisDetails {
  analyzerId: string;
  fields: Record<string, unknown>;
  contents: Array<Record<string, unknown>>;
  warnings: unknown[];
  usage: Record<string, unknown>;
  contentFilters: unknown[];
  detailsTruncated?: boolean;
}

// Mirrors the API's SaveToMemoryResult: how many memory items
// were stored when promoting a document's gist into durable memory.
export interface SaveToMemoryResult {
  saved: number;
}

// Mirrors the API's ForgetFromMemoryResult: how many memory items
// were erased when forgetting a document's contributions to durable memory.
export interface ForgetFromMemoryResult {
  forgotten: number;
}

// Deep-link player: one analyzed audio/video segment's scene grounding —
// its time span plus the analyzer's keyframe and camera-shot boundaries (ms).
export interface MediaTimelineSegment {
  index: number;
  startMs: number | null;
  endMs: number | null;
  keyframes: number[];
  shots: number[];
}

// Mirrors the API's MediaTimeline: the scene timeline for an audio/video document,
// consumed by the player to render clickable scene/keyframe markers. segments is
// empty when the analyzer surfaced no scene detail (media still plays).
export interface MediaTimeline {
  documentId: string;
  modality: string;
  durationMs: number | null;
  segments: MediaTimelineSegment[];
}

// Mirrors the API's AnnotationView: an owner-private note pinned to
// a library document, optionally anchored to a location (page label, "mm:ss", a
// short quote). Presentation metadata only — never part of the model context.
export interface DocumentAnnotation {
  id: string;
  body: string;
  anchor: string;
  createdAt: string;
  updatedAt: string;
}

// A document's visibility. "private" = owner only; "shared" =
// owner + the emails in the grant list; "public" = owner + any signed-in user in
// the tenant (no unauthenticated access).
export type ShareVisibility = "private" | "shared" | "public";

// Mirrors the API's ShareState: a document's sharing posture, surfaced
// to its owner. grantees are normalized grantee emails (only meaningful when
// visibility === "shared"). Owner-private artifacts (notes, saved memories) never
// travel with a shared document.
export interface ShareState {
  documentId: string;
  visibility: ShareVisibility;
  grantees: string[];
}
