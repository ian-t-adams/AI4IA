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
