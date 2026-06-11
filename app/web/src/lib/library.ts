// Document library (Phase 11B-2) shared types. The runtime feature flag is
// surfaced to the browser via the Phase 9/10 pattern (server env -> provider
// prop), NOT a NEXT_PUBLIC_* var, so it is evaluated at request time.

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
  summary: string;
  chunkCount: number;
  visibility: string;
  createdAt: string;
  updatedAt: string;
}

// Mirrors the API's Analyzer (built-ins are merged in server-side).
export interface LibraryAnalyzer {
  id: string;
  name: string;
  description: string;
  kind: "builtin" | "custom";
  modalities: string[];
  baseAnalyzerId: string | null;
}

// Mirrors the API's SaveToMemoryResult (Phase 11E-1): how many memory items
// were stored when promoting a document's gist into durable memory.
export interface SaveToMemoryResult {
  saved: number;
}

// Mirrors the API's ForgetFromMemoryResult (Phase 11E-3): how many memory items
// were erased when forgetting a document's contributions to durable memory.
export interface ForgetFromMemoryResult {
  forgotten: number;
}

// Phase 11D deep-link player: one analyzed audio/video segment's scene grounding —
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
