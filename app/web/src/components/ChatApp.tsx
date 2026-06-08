"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "@/lib/api";
import type { AgentSummary, ChatParams, Message, ModelEntry, Session } from "@/lib/types";
import { Sidebar } from "./Sidebar";
import { ModelPicker } from "./ModelPicker";
import { ParamControls } from "./ParamControls";
import { SystemPromptEditor } from "./SystemPromptEditor";
import { SettingsPanel } from "./SettingsPanel";
import { MessageList, type DisplayMessage } from "./MessageList";
import { Composer } from "./Composer";

function pickDefaultModel(models: ModelEntry[]): string | null {
  return models.find((m) => m.category === "chat")?.id ?? models[0]?.id ?? null;
}

export function ChatApp() {
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);

  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [params, setParams] = useState<ChatParams>({
    temperature: 0.7,
    top_p: 1,
    max_tokens: 1024,
  });
  const [systemPrompt, setSystemPrompt] = useState("");

  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const abortRef = useRef<(() => void) | null>(null);
  // Synchronous in-flight flag so guards work before React state settles.
  const streamingRef = useRef(false);

  // --- initial load ---
  useEffect(() => {
    (async () => {
      try {
        const [catalog, sess] = await Promise.all([
          api.listModels(),
          api.listSessions(),
        ]);
        setModels(catalog.models);
        setSelectedModel(pickDefaultModel(catalog.models));
        setSessions(sess);
      } catch (e) {
        setError((e as Error).message);
      }
      // Agents are an optional enhancement (the @-menu); never block chat on them.
      try {
        setAgents(await api.listAgents());
      } catch {
        /* non-fatal: no @-mention menu */
      }
    })();
  }, []);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
    } catch {
      /* non-fatal */
    }
  }, []);

  const selectSession = useCallback(
    async (id: string) => {
      if (streamingRef.current) return;
      setActiveId(id);
      setError(null);
      try {
        const [msgs, all] = await Promise.all([
          api.listMessages(id),
          api.listSessions(),
        ]);
        setMessages(msgs);
        const s = all.find((x) => x.id === id);
        if (s) {
          if (s.model) setSelectedModel(s.model);
          setSystemPrompt(s.systemPrompt ?? "");
        }
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [],
  );

  const newChat = useCallback(() => {
    if (streamingRef.current) return;
    setActiveId(null);
    setMessages([]);
    setStreamingText("");
    setError(null);
  }, []);

  const deleteSession = useCallback(
    async (id: string) => {
      if (streamingRef.current) return;
      try {
        await api.deleteSession(id);
        if (id === activeId) newChat();
        await refreshSessions();
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [activeId, newChat, refreshSessions],
  );

  const changeModel = useCallback(
    async (modelId: string) => {
      setSelectedModel(modelId);
      if (activeId) {
        try {
          await api.updateSession(activeId, { model: modelId });
        } catch {
          /* non-fatal */
        }
      }
    },
    [activeId],
  );

  const saveSystemPrompt = useCallback(
    async (prompt: string) => {
      setSystemPrompt(prompt);
      if (activeId) {
        try {
          await api.updateSession(activeId, { systemPrompt: prompt });
        } catch {
          /* non-fatal */
        }
      }
    },
    [activeId],
  );

  const send = useCallback(
    async (content: string) => {
      if (streamingRef.current) return;
      if (!selectedModel) {
        setError("Select a model first.");
        return;
      }
      setError(null);
      // Claim the in-flight slot synchronously so a rapid second submit can't
      // create a duplicate session or start an overlapping stream.
      streamingRef.current = true;
      setStreaming(true);
      setStreamingText("");
      let sessionId = activeId;

      // Lazily create a session on the first message.
      if (!sessionId) {
        try {
          const created = await api.createSession({
            model: selectedModel,
            systemPrompt: systemPrompt || null,
          });
          sessionId = created.id;
          setActiveId(created.id);
          setSessions((prev) => [created, ...prev]);
        } catch (e) {
          setError((e as Error).message);
          streamingRef.current = false;
          setStreaming(false);
          return;
        }
      }

      const optimisticUser: Message = {
        id: `tmp-${Date.now()}`,
        sessionId,
        userId: "me",
        role: "user",
        content,
        status: "complete",
        model: selectedModel,
        agent: null,
        createdAt: new Date().toISOString(),
      };
      setMessages((prev) => [...prev, optimisticUser]);

      const isCommand = content.trimStart().startsWith("/");
      const finalize = async () => {
        streamingRef.current = false;
        setStreaming(false);
        abortRef.current = null;
        try {
          setMessages(await api.listMessages(sessionId!));
        } catch {
          /* keep optimistic view */
        }
        setStreamingText("");
        // A slash command can change the session's model or system prompt on the
        // server; re-sync the controls so the change holds for the next turn.
        if (isCommand) {
          try {
            const all = await api.listSessions();
            setSessions(all);
            const s = all.find((x) => x.id === sessionId);
            if (s) {
              if (s.model) setSelectedModel(s.model);
              setSystemPrompt(s.systemPrompt ?? "");
            }
          } catch {
            /* non-fatal */
          }
        } else {
          void refreshSessions();
        }
      };

      abortRef.current = api.streamChat(
        { sessionId, content, model: selectedModel, params },
        {
          onDelta: (t) => setStreamingText((prev) => prev + t),
          onDone: () => void finalize(),
          onError: (msg) => {
            setError(msg);
            void finalize();
          },
          // Stop button: reconcile with the server's cancelled message.
          onAbort: () => void finalize(),
        },
      );
    },
    [activeId, selectedModel, systemPrompt, params, refreshSessions],
  );

  const stop = useCallback(() => {
    // Triggers the stream's AbortError -> onAbort -> finalize (which clears
    // the in-flight flag and reloads persisted messages).
    abortRef.current?.();
  }, []);

  const displayMessages: DisplayMessage[] = useMemo(() => {
    const base: DisplayMessage[] = messages
      .filter((m) => m.role !== "system")
      .map((m) => ({ id: m.id, role: m.role, content: m.content, agent: m.agent }));
    if (streaming) {
      base.push({
        id: "streaming",
        role: "assistant",
        content: streamingText,
        pending: true,
      });
    }
    return base;
  }, [messages, streaming, streamingText]);

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        onSelect={selectSession}
        onNewChat={newChat}
        onDelete={deleteSession}
        onOpenSettings={() => setSettingsOpen(true)}
        disabled={streaming}
      />

      <main style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}>
        <header
          style={{
            display: "flex",
            alignItems: "center",
            gap: 16,
            padding: "12px max(16px, 6%)",
            borderBottom: "1px solid var(--border)",
            background: "var(--bg-elevated)",
          }}
        >
          <ModelPicker models={models} value={selectedModel} onChange={changeModel} />
          <div style={{ marginLeft: "auto", fontSize: "0.8em", color: "var(--fg-muted)" }}>
            {streaming ? "Generating…" : "Ready"}
          </div>
        </header>

        {error && (
          <div
            role="alert"
            style={{
              padding: "10px max(16px, 6%)",
              background: "var(--danger)",
              color: "#fff",
              display: "flex",
              justifyContent: "space-between",
              gap: 12,
            }}
          >
            <span>{error}</span>
            <button
              onClick={() => setError(null)}
              aria-label="Dismiss error"
              style={{ border: "none", background: "transparent", color: "#fff" }}
            >
              ✕
            </button>
          </div>
        )}

        <MessageList messages={displayMessages} />
        <Composer
          disabled={streaming || !selectedModel}
          streaming={streaming}
          agents={agents}
          onSend={send}
          onStop={stop}
        />
      </main>

      <aside
        aria-label="Model parameters"
        style={{
          width: 320,
          flexShrink: 0,
          borderLeft: "1px solid var(--border)",
          background: "var(--bg-elevated)",
          padding: 20,
          display: "flex",
          flexDirection: "column",
          gap: 24,
          overflowY: "auto",
        }}
      >
        <ParamControls params={params} onChange={setParams} />
        <SystemPromptEditor value={systemPrompt} onSave={saveSystemPrompt} />
      </aside>

      {settingsOpen && (
        <SettingsPanel models={models} onClose={() => setSettingsOpen(false)} />
      )}
    </div>
  );
}
