// Browser-side API client. All calls are same-origin to the Next.js proxy
// (src/app/api/[...path]/route.ts), which forwards to the backend API.
import type {
  AgentSummary,
  ChatParams,
  Message,
  ModelCatalog,
  Session,
} from "./types";

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
  return jsonOrThrow(await fetch("/api/models", { cache: "no-store" }));
}

export async function listAgents(): Promise<AgentSummary[]> {
  const data = await jsonOrThrow<{ agents: AgentSummary[] }>(
    await fetch("/api/agents", { cache: "no-store" }),
  );
  return data.agents;
}

export async function listSessions(): Promise<Session[]> {
  return jsonOrThrow(await fetch("/api/sessions", { cache: "no-store" }));
}

export async function createSession(input: {
  title?: string;
  model?: string | null;
  systemPrompt?: string | null;
}): Promise<Session> {
  return jsonOrThrow(
    await fetch("/api/sessions", {
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
    await fetch(`/api/sessions/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    }),
  );
}

export async function deleteSession(id: string): Promise<void> {
  const resp = await fetch(`/api/sessions/${id}`, { method: "DELETE" });
  if (!resp.ok && resp.status !== 204) {
    throw new Error(`${resp.status}: failed to delete session`);
  }
}

export async function listMessages(sessionId: string): Promise<Message[]> {
  return jsonOrThrow(
    await fetch(`/api/sessions/${sessionId}/messages`, { cache: "no-store" }),
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
      const resp = await fetch("/api/chat", {
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
