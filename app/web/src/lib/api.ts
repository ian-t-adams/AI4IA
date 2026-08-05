// Browser-side API client. All calls are same-origin to the Next.js proxy
// (src/app/api/[...path]/route.ts), which forwards to the backend API.
import type {
  ActivityStep,
  AgentSummary,
  ChatParams,
  DocumentSummary,
  ImageRequest,
  ImageResponse,
  Message,
  ModelCatalog,
  PendingToolApprovalPrompt,
  Session,
  ToolApprovalDecision,
  ToolOverrides,
  UserAgent,
  UserAgentCreate,
  UserAgentUpdate,
  ToolCatalogItem,
  AttachmentCapabilities,
  VoiceTurnInput,
  Workflow,
  WorkflowCreate,
  WorkflowListResult,
  WorkflowRunAccepted,
  WorkflowRunOutcome,
  WorkflowRunResult,
  WorkflowRunStatus,
  WorkflowUpdate,
} from "./types";
import type { VoiceLiveProviderCatalogResponse } from "./voiceLive";
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

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: string,
  ) {
    super(`${status}: ${detail}`);
    this.name = "ApiError";
  }
}

export function apiErrorDetail(reason: unknown): string {
  return reason instanceof ApiError
    ? reason.detail
    : reason instanceof Error
      ? reason.message
      : "Something went wrong.";
}

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      const body = await resp.json();
      detail = body?.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(resp.status, String(detail));
  }
  return (await resp.json()) as T;
}

export async function listModels(): Promise<ModelCatalog> {
  return jsonOrThrow(await apiFetch("/api/models", { cache: "no-store" }));
}

export async function getVoiceLiveConfig(): Promise<VoiceLiveProviderCatalogResponse> {
  return jsonOrThrow(
    await apiFetch("/api/voice/live/config", {
      cache: "no-store",
    }),
  );
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

// --- Studio: user-defined agents & workflows ---

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

export async function listWorkflows(): Promise<WorkflowListResult> {
  const data = await jsonOrThrow<{
    workflows: Workflow[];
    durableAvailable?: boolean;
  }>(await apiFetch("/api/workflows", { cache: "no-store" }));
  // Default false, not true: an older API that does not send the field cannot
  // honour a durable request either, and offering the control anyway would turn
  // a missing capability into a 422 in the user's face.
  return {
    workflows: data.workflows,
    durableAvailable: data.durableAvailable === true,
  };
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
//
// `durable` opts THIS run into orchestrated execution so it survives a replica
// restart, scale-in, or crash. It is a per-request opt-in rather than a mode the
// server flips, so the response shape only changes when the caller asked for it:
// a durable run answers 202 with a run id and no message, because the assistant
// turn genuinely does not exist yet. Asking for durability on a deployment that
// cannot provide it is a 422, never a silent downgrade.
export async function runWorkflow(
  name: string,
  input: {
    sessionId: string;
    input: string;
    model?: string | null;
    durable?: boolean;
  },
): Promise<WorkflowRunOutcome> {
  const resp = await apiFetch(`/api/workflows/${encodeURIComponent(name)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(input),
  });
  if (resp.status === 202) {
    return { scheduled: true, run: await jsonOrThrow<WorkflowRunAccepted>(resp) };
  }
  return { scheduled: false, result: await jsonOrThrow<WorkflowRunResult>(resp) };
}

// Terminal orchestration states, upper-cased to match the durabletask runtime
// status names the API forwards verbatim. An UNRECOGNISED status is treated as
// still-running on purpose: the caller's poll is attempt-bounded, so an unknown
// state degrades to "took too long" rather than reporting a run finished when it
// has not.
const TERMINAL_RUN_STATUSES = new Set(["COMPLETED", "FAILED", "TERMINATED"]);

export function isTerminalRunStatus(status: string): boolean {
  return TERMINAL_RUN_STATUSES.has(status.trim().toUpperCase());
}

// Polls a durable run started with `durable: true`. Distinguishes "still running"
// from "finished and failed" without diffing the transcript.
export async function getWorkflowRun(runId: string): Promise<WorkflowRunStatus> {
  return jsonOrThrow(
    await apiFetch(`/api/workflows/runs/${encodeURIComponent(runId)}`, {
      cache: "no-store",
    }),
  );
}

// --- Custom tools: bring-your-own remote MCP servers ---
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

// The curated **official** MCP servers (APIM-fronted, app-global). Read-only and
// always present: the endpoint returns an empty list when the plane is off, so
// the agent builder can call this unconditionally and simply render nothing.
export async function listOfficialMcpServers(): Promise<UserMcpServer[]> {
  const data = await jsonOrThrow<{ servers: UserMcpServer[] }>(
    await apiFetch("/api/agents/official-mcp-servers", { cache: "no-store" }),
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

// --- Voice ---

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
//
// A 200 response is not necessarily playable audio: a misconfigured gateway/
// proxy hop, an auth interstitial, or a truncated stream can all return
// `resp.ok === true` with an HTML/JSON body or an empty payload. Left
// unchecked, that blob gets handed straight to an <audio> element, which fails
// with an opaque browser decode error surfaced to users as "Couldn't play the
// synthesized audio." with no way to tell a bad response from a real playback
// problem. Validating content-type + size here, before any object URL or
// <audio> element exists, gives an actionable, specific error instead.
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
  const contentType = (resp.headers.get("content-type") ?? "")
    .split(";")[0]
    .trim()
    .toLowerCase();
  if (!contentType.startsWith("audio/")) {
    throw new Error(
      contentType
        ? `Speech synthesis returned ${contentType} instead of audio. Try again.`
        : "Speech synthesis returned an unrecognized response. Try again.",
    );
  }
  const blob = await resp.blob();
  if (blob.size === 0) {
    throw new Error("Speech synthesis returned an empty audio clip. Try again.");
  }
  return blob;
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

// Deep-link player: fetches the ORIGINAL audio/video bytes of a ready
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

// The scene/keyframe timeline for an audio/video document, used to draw
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

export async function createSession(
  input: {
    title?: string;
    model?: string | null;
    systemPrompt?: string | null;
    agentName?: string | null;
    toolOverrides?: ToolOverrides;
    libraryDocumentIds?: string[] | null;
  },
  signal?: AbortSignal,
): Promise<Session> {
  return jsonOrThrow(
    await apiFetch("/api/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(input),
      signal,
    }),
  );
}

export async function updateSession(
  id: string,
  patch: {
    title?: string;
    model?: string | null;
    systemPrompt?: string | null;
    agentName?: string | null;
    toolOverrides?: ToolOverrides;
    libraryDocumentIds?: string[] | null;
  },
): Promise<Session> {
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function listTools(sessionId?: string | null): Promise<ToolCatalogItem[]> {
  const query = sessionId ? `?sessionId=${encodeURIComponent(sessionId)}` : "";
  const result = await jsonOrThrow<{ tools: ToolCatalogItem[] }>(
    await apiFetch(`/api/tools${query}`, { cache: "no-store" }),
  );
  return result.tools;
}

export async function getToolCatalog(
  sessionId?: string | null,
  agentName?: string | null,
): Promise<{ tools: ToolCatalogItem[]; inheritedTools: string[] }> {
  const query = new URLSearchParams();
  if (sessionId) query.set("sessionId", sessionId);
  if (agentName) query.set("agentName", agentName);
  const suffix = query.size ? `?${query.toString()}` : "";
  return jsonOrThrow(
    await apiFetch(`/api/tools${suffix}`, { cache: "no-store" }),
  );
}

export async function getAttachmentCapabilities(): Promise<AttachmentCapabilities> {
  return jsonOrThrow(
    await apiFetch("/api/attachments/capabilities", { cache: "no-store" }),
  );
}

export async function associateLibraryDocument(
  sessionId: string,
  documentId: string,
): Promise<Session> {
  return jsonOrThrow(
    await apiFetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/library-documents/${encodeURIComponent(documentId)}`,
      { method: "POST" },
    ),
  );
}

export async function disassociateLibraryDocument(
  sessionId: string,
  documentId: string,
): Promise<Session> {
  return jsonOrThrow(
    await apiFetch(
      `/api/sessions/${encodeURIComponent(sessionId)}/library-documents/${encodeURIComponent(documentId)}`,
      { method: "DELETE" },
    ),
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
// text chat and live voice share one conversation. The per-cycle conversation id
// makes a retry idempotent. Returns the created (or previously created) messages.
export async function appendVoiceTurns(
  sessionId: string,
  conversationId: string,
  turns: VoiceTurnInput[],
): Promise<Message[]> {
  return jsonOrThrow(
    await apiFetch(`/api/sessions/${sessionId}/voice-turns`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ conversationId, turns }),
    }),
  );
}

// --- Documents ---

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
  const resp = await apiFetch(
    `/api/sessions/${sessionId}/documents/${documentId}`,
    { method: "DELETE" },
  );
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete document`);
  }
}

// --- Document library: the user's cross-session library. These go
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

// Explicitly promote a ready document's gist into the caller's
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

// The explicit undo of saveLibraryDocumentToMemory. Erases exactly
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

// Owner-private annotations pinned to a library document. These notes
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

// --- Document-level sharing. Grants are keyed on grantee EMAIL; the
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

export interface StreamHandlers {
  onDelta: (text: string) => void;
  onDone: () => void;
  onError: (
    message: string,
    info: {
      accepted: boolean;
      persistenceFailed: boolean;
      definitePreAcceptance?: boolean;
    },
  ) => void;
  onMetadata: (metadata: {
    userMessageId: string | null;
    assistantMessageId: string;
  }) => void;
  // Called for each live activity event during an agentic (tool-using) turn, so
  // the UI can show "Searching the web..." while it runs. Ignored by callers that
  // don't render activity.
  onStep?: (step: ActivityStep) => void;
  // Called when the server held one or more tool calls pending a per-invocation
  // human approval. Each prompt carries its one-time grant, delivered here and
  // nowhere else — it is not on the persisted message. Ignored by callers that
  // don't render approvals, which simply means those calls stay unexecuted.
  onApprovals?: (prompts: PendingToolApprovalPrompt[]) => void;
  // Called when the caller aborts the stream (e.g. Stop button). Lets the UI
  // reconcile with the server, which persists a `cancelled` assistant message.
  onAbort?: (info?: {
    accepted: boolean;
    persistenceFailed: boolean;
    definitePreAcceptance?: boolean;
  }) => void;
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
    // Per-invocation tool approvals redeemed on this turn. Opaque to the client:
    // the server re-derives what each one authorizes from its own record.
    approvals?: ToolApprovalDecision[];
  },
  handlers: StreamHandlers,
): () => void {
  const controller = new AbortController();

  (async () => {
    let sawDone = false;
    let sawMetadata = false;
    try {
      const resp = await apiFetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...input, stream: true }),
        signal: controller.signal,
      });
      if (!resp.ok || !resp.body) {
        const detail = await resp.text().catch(() => resp.statusText);
        handlers.onError(`${resp.status}: ${detail}`, {
          accepted: false,
          persistenceFailed: false,
          definitePreAcceptance: resp.status >= 400 && resp.status < 500,
        });
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
            if (!sawMetadata) {
              handlers.onError("Stream completed without message metadata.", {
                accepted: false,
                persistenceFailed: false,
                definitePreAcceptance: false,
              });
              return;
            }
            sawDone = true;
            handlers.onDone();
            return;
          }
          try {
            const obj = JSON.parse(payload);
            if (obj.metadata) {
              const userMessageId = obj.metadata.userMessageId;
              const assistantMessageId = obj.metadata.assistantMessageId;
              if (
                (typeof userMessageId !== "string" && userMessageId !== null) ||
                typeof assistantMessageId !== "string"
              ) {
                handlers.onError("Stream returned invalid message metadata.", {
                  accepted: false,
                  persistenceFailed: false,
                  definitePreAcceptance: false,
                });
                return;
              }
              sawMetadata = true;
              handlers.onMetadata({ userMessageId, assistantMessageId });
              continue;
            }
            if (obj.error) {
              handlers.onError(String(obj.error), {
                accepted: sawMetadata,
                persistenceFailed: obj.persistenceFailed === true,
                definitePreAcceptance: false,
              });
              return;
            }
            if (obj.step) {
              handlers.onStep?.(obj.step as ActivityStep);
              continue;
            }
            if (Array.isArray(obj.approvals)) {
              handlers.onApprovals?.(
                obj.approvals as PendingToolApprovalPrompt[],
              );
              continue;
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
      else {
        handlers.onError("Stream ended unexpectedly.", {
          accepted: sawMetadata,
          persistenceFailed: false,
          definitePreAcceptance: false,
        });
      }
    } catch (err) {
      if ((err as Error).name === "AbortError") {
        handlers.onAbort?.({
          accepted: sawMetadata,
          persistenceFailed: false,
          definitePreAcceptance: false,
        });
        return;
      }
      handlers.onError((err as Error).message, {
        accepted: sawMetadata,
        persistenceFailed: false,
        definitePreAcceptance: false,
      });
    }
  })();

  return () => controller.abort();
}
