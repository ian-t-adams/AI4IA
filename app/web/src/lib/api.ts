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
  Workflow,
  WorkflowCreate,
  WorkflowRunResult,
  WorkflowUpdate,
} from "./types";
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
