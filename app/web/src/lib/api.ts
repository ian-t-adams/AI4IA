// Browser-side API client. All calls are same-origin to the Next.js proxy
// (src/app/api/[...path]/route.ts), which forwards to the backend API.
import type {
  AgentSummary,
  ChatParams,
  DocumentSummary,
  ImageRequest,
  ImageResponse,
  Message,
  ModelCatalog,
  Session,
  UserAgent,
  UserAgentCreate,
  UserAgentUpdate,
  VoiceTurnInput,
  Workflow,
  WorkflowCreate,
  WorkflowRunResult,
  WorkflowUpdate,
} from "./types";
import type {
  DocumentAnnotation,
  ForgetFromMemoryResult,
  LibraryAnalyzer,
  LibraryDocument,
  MediaTimeline,
  SaveToMemoryResult,
  ShareState,
  ShareVisibility,
} from "./library";
import type {
  UserMcpServer,
  UserMcpServerCreate,
  UserMcpServerTest,
  UserMcpServerUpdate,
} from "./customTools";
import { apiFetch } from "./auth";

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body?.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return (await resp.json()) as T;
}

export async function listModels(): Promise<ModelCatalog> {
  return jsonOrThrow(await apiFetch("/api/models", { cache: "no-store" }));
}

// Generates an image through the backend gateway (gpt-image-2 etc.). Returns
// base64 image data (no data-URL prefix); callers wrap it as needed.
export async function generateImage(
  input: ImageRequest,
): Promise<ImageResponse> {
  return jsonOrThrow(
    await apiFetch("/api/images/generations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function listAgents(): Promise<AgentSummary[]> {
  const data = await jsonOrThrow<{ agents: AgentSummary[] }>(
    await apiFetch("/api/agents", { cache: "no-store" }),
  );
  return data.agents;
}

// --- Studio: user-defined agents & workflows (Phase 8) ---

export async function listMyAgents(): Promise<UserAgent[]> {
  const data = await jsonOrThrow<{ agents: UserAgent[] }>(
    await apiFetch("/api/agents/mine", { cache: "no-store" }),
  );
  return data.agents;
}

export async function createAgent(input: UserAgentCreate): Promise<UserAgent> {
  return jsonOrThrow(
    await apiFetch("/api/agents", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateAgent(
  name: string,
  patch: UserAgentUpdate,
): Promise<UserAgent> {
  return jsonOrThrow(
    await apiFetch(`/api/agents/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function deleteAgent(name: string): Promise<void> {
  const resp = await apiFetch(`/api/agents/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete agent`);
  }
}

export async function listWorkflows(): Promise<Workflow[]> {
  const data = await jsonOrThrow<{ workflows: Workflow[] }>(
    await apiFetch("/api/workflows", { cache: "no-store" }),
  );
  return data.workflows;
}

export async function createWorkflow(input: WorkflowCreate): Promise<Workflow> {
  return jsonOrThrow(
    await apiFetch("/api/workflows", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateWorkflow(
  name: string,
  patch: WorkflowUpdate,
): Promise<Workflow> {
  return jsonOrThrow(
    await apiFetch(`/api/workflows/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function deleteWorkflow(name: string): Promise<void> {
  const resp = await apiFetch(`/api/workflows/${encodeURIComponent(name)}`, {
    method: "DELETE",
  });
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete workflow`);
  }
}

// Runs a saved workflow against a chat session. The backend persists the user
// input + the pipeline's assistant result to the session like a normal turn.
export async function runWorkflow(
  name: string,
  input: { sessionId: string; input: string; model?: string | null },
): Promise<WorkflowRunResult> {
  return jsonOrThrow(
    await apiFetch(`/api/workflows/${encodeURIComponent(name)}/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

// --- Custom tools: bring-your-own remote MCP servers (Phase 12B) ---
//
// All endpoints are flag-gated server-side: when custom tools are disabled the
// whole surface 404s, so these are only ever called from the (inert-when-off)
// custom-tools UI. They go through the same-origin Next proxy like every other call.

export async function listMcpServers(): Promise<UserMcpServer[]> {
  const data = await jsonOrThrow<{ servers: UserMcpServer[] }>(
    await apiFetch("/api/agents/mcp-servers", { cache: "no-store" }),
  );
  return data.servers;
}

export async function createMcpServer(
  input: UserMcpServerCreate,
): Promise<UserMcpServer> {
  return jsonOrThrow(
    await apiFetch("/api/agents/mcp-servers", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateMcpServer(
  name: string,
  patch: UserMcpServerUpdate,
): Promise<UserMcpServer> {
  return jsonOrThrow(
    await apiFetch(`/api/agents/mcp-servers/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function deleteMcpServer(name: string): Promise<void> {
  const resp = await apiFetch(
    `/api/agents/mcp-servers/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete MCP server`);
  }
}

// Re-connects to a saved server and refreshes its cached tools / lastError. An
// authed server may re-use its stored secret; pass one only to override it.
export async function testMcpServer(
  name: string,
  payload?: UserMcpServerTest,
): Promise<UserMcpServer> {
  return jsonOrThrow(
    await apiFetch(`/api/agents/mcp-servers/${encodeURIComponent(name)}/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    }),
  );
}

// --- Voice (Phase 7B) ---

export interface TranscriptionResult {
  text: string;
  model: string;
  deployment: string;
}

// Transcribes a recorded audio blob (speech-to-text via whisper). The browser
// sets the multipart boundary, so we never set Content-Type ourselves.
export async function transcribeAudio(
  audio: Blob,
  opts?: { model?: string; language?: string; filename?: string },
): Promise<TranscriptionResult> {
  const form = new FormData();
  form.append("file", audio, opts?.filename ?? "recording.webm");
  if (opts?.model) form.append("model", opts.model);
  if (opts?.language) form.append("language", opts.language);
  return jsonOrThrow(
    await apiFetch("/api/voice/transcriptions", { method: "POST", body: form }),
  );
}

// Synthesizes speech (text-to-speech via gpt-4o-mini-tts). Returns the raw audio
// blob; callers wrap it in an object URL for playback.
export async function synthesizeSpeech(
  text: string,
  opts?: { model?: string; voice?: string; format?: string },
): Promise<Blob> {
  const resp = await apiFetch("/api/voice/speech", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ input: text, ...opts }),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body?.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return await resp.blob();
}

// Fetches a tool-generated image's bytes from the authenticated serve endpoint.
// A raw <img src> would not carry the bearer token, so (like synthesizeSpeech) we
// fetch via apiFetch and hand back a Blob the caller wraps in an object URL.
export async function fetchImageArtifact(artifactId: string): Promise<Blob> {
  const resp = await apiFetch(`/api/images/artifacts/${artifactId}`, {
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`${resp.status}: failed to load image`);
  }
  return await resp.blob();
}

// Fetches a tool-generated video's MP4 bytes from the authenticated serve
// endpoint (mirrors fetchImageArtifact). The caller wraps the Blob in an object
// URL for a <video> element, since a raw src would not carry the bearer token.
export async function fetchVideoArtifact(artifactId: string): Promise<Blob> {
  const resp = await apiFetch(`/api/videos/artifacts/${artifactId}`, {
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`${resp.status}: failed to load video`);
  }
  return await resp.blob();
}

// Fetches an over-cap process_document result's markdown text from the
// authenticated serve endpoint (mirrors fetchImageArtifact). Returns text rather
// than a Blob since the result is rendered inline as Markdown.
export async function fetchDocumentArtifact(artifactId: string): Promise<string> {
  const resp = await apiFetch(`/api/documents/artifacts/${artifactId}`, {
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`${resp.status}: failed to load document`);
  }
  return await resp.text();
}

// Phase 11D deep-link player: fetches the ORIGINAL audio/video bytes of a ready
// library document (owner + ready gated server-side). Fetched via apiFetch so the
// bearer token rides along — a raw <video src> URL could not carry it — then the
// caller wraps the Blob in an object URL, which also gives client-side seeking.
export async function fetchLibraryMedia(documentId: string): Promise<Blob> {
  const resp = await apiFetch(`/api/library/documents/${documentId}/media`, {
    cache: "no-store",
  });
  if (!resp.ok) {
    throw new Error(`${resp.status}: failed to load media`);
  }
  return await resp.blob();
}

// Phase 11D: the scene/keyframe timeline for an audio/video document, used to draw
// clickable deep-link markers on the player. A missing analyzer sidecar degrades to
// an empty segments list server-side rather than erroring.
export async function fetchLibraryTimeline(
  documentId: string,
): Promise<MediaTimeline> {
  return jsonOrThrow(
    await apiFetch(`/api/library/documents/${documentId}/timeline`, {
      cache: "no-store",
    }),
  );
}

export async function listSessions(): Promise<Session[]> {
  return jsonOrThrow(await apiFetch("/api/sessions", { cache: "no-store" }));
}

export async function createSession(input: {
  title?: string;
  model?: string | null;
  systemPrompt?: string | null;
}): Promise<Session> {
  return jsonOrThrow(
    await apiFetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
    }),
  );
}

export async function updateSession(
  id: string,
  patch: { title?: string; model?: string | null; systemPrompt?: string | null },
): Promise<Session> {
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function deleteSession(id: string): Promise<void> {
  const resp = await apiFetch(`/api/sessions/${id}`, { method: "DELETE" });
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete session`);
  }
}

export async function listMessages(sessionId: string): Promise<Message[]> {
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${sessionId}/messages`, { cache: "no-store" }),
  );
}

// Persists a finalized Voice Live exchange back into a session's transcript so
// text chat and live voice share one conversation. Returns the created messages.
export async function appendVoiceTurns(
  sessionId: string,
  turns: VoiceTurnInput[],
): Promise<Message[]> {
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${sessionId}/voice-turns`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ turns }),
    }),
  );
}

// --- Documents (Phase 7C) ---

// Uploads a file to a session. The browser sets the multipart boundary, so we
// never set Content-Type ourselves. The backend extracts plain text locally and
// returns a summary (no full text). Large/binary/empty files are rejected
// (4xx with a `detail` message surfaced by jsonOrThrow).
export async function uploadDocument(
  sessionId: string,
  file: File,
): Promise<DocumentSummary> {
  const form = new FormData();
  form.append("file", file, file.name);
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${sessionId}/documents`, {
      method: "POST",
      body: form,
    }),
  );
}

export async function listDocuments(
  sessionId: string,
): Promise<DocumentSummary[]> {
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${sessionId}/documents`, { cache: "no-store" }),
  );
}

export async function deleteDocument(
  sessionId: string,
  documentId: string,
): Promise<void> {
  const resp = await fetch(
    `/api/sessions/${sessionId}/documents/${documentId}`,
    { method: "DELETE" },
  );
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete document`);
  }
}

// --- Document library (Phase 11B-2): the user's cross-session library. These go
// through the same Next HTTP proxy as the rest of the API; they 404 when the
// feature is disabled (the UI is hidden in that case, so they are never called).

export async function listLibraryDocuments(): Promise<LibraryDocument[]> {
  return jsonOrThrow(
    await apiFetch("/api/library/documents", { cache: "no-store" }),
  );
}

export async function uploadLibraryDocument(
  file: File,
  analyzerId?: string | null,
): Promise<LibraryDocument> {
  const form = new FormData();
  form.append("file", file, file.name);
  if (analyzerId) form.append("analyzerId", analyzerId);
  return jsonOrThrow(
    await apiFetch("/api/library/documents", { method: "POST", body: form }),
  );
}

export async function deleteLibraryDocument(documentId: string): Promise<void> {
  const resp = await apiFetch(`/api/library/documents/${documentId}`, {
    method: "DELETE",
  });
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete document`);
  }
}

export async function listLibraryAnalyzers(): Promise<LibraryAnalyzer[]> {
  return jsonOrThrow(
    await apiFetch("/api/library/analyzers", { cache: "no-store" }),
  );
}

// Phase 11E-1: explicitly promote a ready document's gist into the caller's
// durable memory so the assistant can recall it across sessions. 409 when the
// memory store is disabled, 409 when the document is not yet ready.
export async function saveLibraryDocumentToMemory(
  documentId: string,
): Promise<SaveToMemoryResult> {
  return jsonOrThrow(
    await apiFetch(`/api/library/documents/${documentId}/memory`, {
      method: "POST",
    }),
  );
}

// Phase 11E-3: the explicit undo of saveLibraryDocumentToMemory. Erases exactly
// the memories saved from this document, leaving chat-sourced and other
// documents' memories intact. 404 when the library is off, 409 when the memory
// store is disabled. Idempotent — forgetting a document with nothing saved
// returns { forgotten: 0 }.
export async function forgetLibraryDocumentFromMemory(
  documentId: string,
): Promise<ForgetFromMemoryResult> {
  return jsonOrThrow(
    await apiFetch(`/api/library/documents/${documentId}/memory`, {
      method: "DELETE",
    }),
  );
}

// Phase 11E-2: owner-private annotations pinned to a library document. These notes
// are presentation-only metadata — they are deliberately never surfaced to the
// model's retrieval/prompt context, and every operation is owner-only.
export async function listLibraryAnnotations(
  documentId: string,
): Promise<DocumentAnnotation[]> {
  return jsonOrThrow(
    await apiFetch(`/api/library/documents/${documentId}/annotations`, {
      cache: "no-store",
    }),
  );
}

export async function createLibraryAnnotation(
  documentId: string,
  body: string,
  anchor?: string,
): Promise<DocumentAnnotation> {
  return jsonOrThrow(
    await apiFetch(`/api/library/documents/${documentId}/annotations`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body, anchor: anchor ?? "" }),
    }),
  );
}

export async function updateLibraryAnnotation(
  documentId: string,
  annotationId: string,
  changes: { body?: string; anchor?: string },
): Promise<DocumentAnnotation> {
  return jsonOrThrow(
    await apiFetch(
      `/api/library/documents/${documentId}/annotations/${annotationId}`,
      {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(changes),
      },
    ),
  );
}

export async function deleteLibraryAnnotation(
  documentId: string,
  annotationId: string,
): Promise<void> {
  const resp = await apiFetch(
    `/api/library/documents/${documentId}/annotations/${annotationId}`,
    { method: "DELETE" },
  );
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete annotation`);
  }
}

// --- Document-level sharing (Phase 11F). Grants are keyed on grantee EMAIL; the
// owner-only endpoints below set/read/revoke who a document is shared with. A
// non-owner gets a generic 404 from the API (never leaks existence). Annotations
// and saved memories deliberately do NOT travel with a shared document.

// Documents explicitly shared *with* the caller (by their email). Tenant-public
// documents are openable by id but intentionally not listed here.
export async function listSharedWithMe(): Promise<LibraryDocument[]> {
  return jsonOrThrow(
    await apiFetch("/api/library/shared", { cache: "no-store" }),
  );
}

// Read a document's sharing posture (owner-only). 404 if not owned.
export async function getDocumentShares(
  documentId: string,
): Promise<ShareState> {
  return jsonOrThrow(
    await apiFetch(`/api/library/documents/${documentId}/shares`, {
      cache: "no-store",
    }),
  );
}

// Replace a document's sharing posture (owner-only). grantees only take effect
// for visibility === "shared"; they are normalized/validated/de-duped/capped
// server-side (422 on a malformed email or over the cap). 404 if not owned.
export async function setDocumentShares(
  documentId: string,
  visibility: ShareVisibility,
  grantees: string[],
): Promise<ShareState> {
  return jsonOrThrow(
    await apiFetch(`/api/library/documents/${documentId}/shares`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ visibility, grantees }),
    }),
  );
}

// Revoke one grantee's access (owner-only, idempotent). Returns the new state.
export async function revokeDocumentShare(
  documentId: string,
  email: string,
): Promise<ShareState> {
  return jsonOrThrow(
    await apiFetch(
      `/api/library/documents/${documentId}/shares/${encodeURIComponent(email)}`,
      { method: "DELETE" },
    ),
  );
}

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onDone: () => void;
  onError: (message: string) => void;
  // Called when the caller aborts the stream (e.g. Stop button). Lets the UI
  // reconcile with the server, which persists a `cancelled` assistant message.
  onAbort?: () => void;
}

// Streams a chat completion. Returns an abort function the caller can invoke
// to cancel the in-flight request (the backend persists a cancelled status).
export function streamChat(
  input: {
    sessionId: string;
    content: string;
    model?: string | null;
    region?: string | null;
    dataZone?: string | null;
    params?: ChatParams;
  },
  handlers: StreamHandlers,
): () => void {
  const controller = new AbortController();

  (async () => {
    let sawDone = false;
    try {
      const resp = await apiFetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...input, stream: true }),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        const detail = await resp.text().catch(() => resp.statusText);
        handlers.onError(`${resp.status}: ${detail}`);
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const events = buffer.split("\n\n");
        buffer = events.pop() ?? "";
        for (const evt of events) {
          const line = evt.trim();
          if (!line.startsWith("data:")) continue;
          const payload = line.slice("data:".length).trim();
          if (payload === "[DONE]") {
            sawDone = true;
            handlers.onDone();
            return;
          }
          try {
            const obj = JSON.parse(payload);
            if (obj.error) {
              handlers.onError(String(obj.error));
              return;
            }
            const delta: string =
              obj?.choices?.[0]?.delta?.content ?? "";
            if (delta) handlers.onDelta(delta);
          } catch {
            /* skip non-JSON keepalive lines */
          }
        }
      }
      // Reached EOF without a terminating [DONE]: treat as a truncated stream
      // rather than a clean completion so the UI can surface/reconcile it.
      if (sawDone) handlers.onDone();
      else handlers.onError("Stream ended unexpectedly.");
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        handlers.onAbort?.();
        return;
      }
      handlers.onError((err as Error).message);
    }
  })();

  return () => controller.abort();
}
