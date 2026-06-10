"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "@/lib/api";
import type { AgentSummary, ChatParams, DocumentSummary, Message, ModelEntry, Session } from "@/lib/types";
import type { LibraryDocument } from "@/lib/library";
import { Sidebar } from "./Sidebar";
import { ModelPicker } from "./ModelPicker";
import { ParamControls } from "./ParamControls";
import { SystemPromptEditor } from "./SystemPromptEditor";
import { SettingsPanel } from "./SettingsPanel";
import { StudioPanel } from "./StudioPanel";
import { ImageStudioPanel } from "./ImageStudioPanel";
import { LibraryPanel } from "./LibraryPanel";
import { MessageList, type DisplayMessage } from "./MessageList";
import { Composer } from "./Composer";
import { UserMenu } from "./UserMenu";
import { useVoiceLiveConfig } from "./VoiceLiveProvider";
import { useLibraryConfig } from "./LibraryProvider";

function pickDefaultModel(models: ModelEntry[]): string | null {
  // Never default to a capability model: prefer a plain "chat" model, then any
  // conversational model, and only then give up.
  const conversational = models.filter((m) => m.conversational);
  return (
    conversational.find((m) => m.category === "chat")?.id ??
    conversational[0]?.id ??
    null
  );
}

export function ChatApp() {
  const voiceLiveConfig = useVoiceLiveConfig();
  const libraryConfig = useLibraryConfig();
  // The document library (Phase 11B-2). When on, the Composer paperclip routes
  // uploads through the per-user library CU-ingest pipeline instead of the
  // session-scoped local-extract path, so the doc is parsed, surfaced to the
  // agent (retrieval tiers + fetch_document) and runnable via run_code.
  const libraryEnabled = libraryConfig.enabled;
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [agents, setAgents] = useState<AgentSummary[]>([]);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  // Library docs attached via the paperclip this view (only used when
  // libraryEnabled). Transient: cleared on new chat / session switch, but the
  // doc itself persists in the user's library and stays available to the agent.
  const [libraryDocs, setLibraryDocs] = useState<LibraryDocument[]>([]);
  const [uploading, setUploading] = useState(false);

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
  const [studioOpen, setStudioOpen] = useState(false);
  const [imageryOpen, setImageryOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);

  const abortRef = useRef<(() => void) | null>(null);
  // Synchronous in-flight flag so guards work before React state settles.
  const streamingRef = useRef(false);
  // Holds an in-flight lazy session-creation promise so a rapid send + upload
  // (or two uploads) share a single session instead of racing to create two.
  const creatingRef = useRef<Promise<string> | null>(null);
  // Synchronous mirror of activeId so ensureSession sees a just-created session
  // immediately (before the setActiveId state flush), preventing a double create
  // when an upload is quickly followed by a send.
  const sessionIdRef = useRef<string | null>(null);
  useEffect(() => {
    sessionIdRef.current = activeId;
  }, [activeId]);

  // --- initial load ---
  useEffect(() => {
    (async () => {
      // The model catalog and the conversation list are loaded independently:
      // the catalog must populate for chat to be usable, while history is
      // best-effort. Loading them separately means a backing-store outage on one
      // can't blank the other. (Previously a single Promise.all meant a sessions
      // 500 rejected the whole load and left the model picker empty.)
      await Promise.allSettled([
        api.listModels().then(
          (catalog) => {
            setModels(catalog.models);
            setSelectedModel(pickDefaultModel(catalog.models));
          },
          (e) => setError((e as Error).message),
        ),
        api.listSessions().then(
          (sess) => setSessions(sess),
          (e) =>
            setError(
              (prev) =>
                prev ??
                `Couldn't load your conversations: ${(e as Error).message}`,
            ),
        ),
      ]);
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
        const [msgs, all, docs] = await Promise.all([
          api.listMessages(id),
          api.listSessions(),
          api.listDocuments(id).catch(() => [] as DocumentSummary[]),
        ]);
        setMessages(msgs);
        setDocuments(docs);
        // Library chips are a transient per-view confirmation; the docs persist
        // in the library and stay available to the agent regardless.
        setLibraryDocs([]);
        setSessions(all);
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
    setDocuments([]);
    setLibraryDocs([]);
    setStreamingText("");
    setError(null);
  }, []);

  const refreshAgents = useCallback(async () => {
    try {
      setAgents(await api.listAgents());
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  const openWorkflowRun = useCallback(
    (sessionId: string) => {
      setStudioOpen(false);
      void selectSession(sessionId);
    },
    [selectSession],
  );

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

  // Lazily create (or reuse) the active session. Shared by send + document
  // upload so they never race to create two sessions; concurrent callers await
  // the same in-flight creation promise.
  const ensureSession = useCallback(async (): Promise<string> => {
    if (sessionIdRef.current) return sessionIdRef.current;
    if (creatingRef.current) return creatingRef.current;
    const p = (async () => {
      const created = await api.createSession({
        model: selectedModel,
        systemPrompt: systemPrompt || null,
      });
      sessionIdRef.current = created.id;
      setActiveId(created.id);
      setSessions((prev) => [created, ...prev]);
      return created.id;
    })();
    creatingRef.current = p;
    try {
      return await p;
    } finally {
      creatingRef.current = null;
    }
  }, [selectedModel, systemPrompt]);

  const uploadDocument = useCallback(
    async (file: File) => {
      setError(null);
      setUploading(true);
      try {
        if (libraryEnabled) {
          // CU-ingest path: send the file to the user's library so it is
          // parsed by Content Understanding and surfaced to the agent (and
          // run_code) via the existing retrieval tiers. No session is needed
          // for ingest — it is created lazily on the first send.
          const doc = await api.uploadLibraryDocument(file);
          setLibraryDocs((prev) => [...prev.filter((d) => d.id !== doc.id), doc]);
        } else {
          // Session-scoped local-extract fallback (flag off / local dev).
          const sid = await ensureSession();
          const doc = await api.uploadDocument(sid, file);
          // Replace any same-id entry (defensive) and append.
          setDocuments((prev) => [...prev.filter((d) => d.id !== doc.id), doc]);
        }
      } catch (e) {
        setError((e as Error).message);
      } finally {
        setUploading(false);
      }
    },
    [ensureSession, libraryEnabled],
  );

  const removeDocument = useCallback(
    async (documentId: string) => {
      if (!activeId) return;
      const prev = documents;
      // Optimistic removal; restore on failure.
      setDocuments((cur) => cur.filter((d) => d.id !== documentId));
      try {
        await api.deleteDocument(activeId, documentId);
      } catch (e) {
        setDocuments(prev);
        setError((e as Error).message);
      }
    },
    [activeId, documents],
  );

  // Removing a library chip deletes the just-uploaded library document (the
  // paperclip both adds and removes it), mirroring the session-doc remove.
  const removeLibraryDocument = useCallback(
    async (documentId: string) => {
      const prev = libraryDocs;
      setLibraryDocs((cur) => cur.filter((d) => d.id !== documentId));
      try {
        await api.deleteLibraryDocument(documentId);
      } catch (e) {
        setLibraryDocs(prev);
        setError((e as Error).message);
      }
    },
    [libraryDocs],
  );

  // Poll while any attached library doc is still being ingested (stored →
  // analyzing → ready/failed), reconciling tracked chips by id. Mirrors the
  // LibraryPanel polling; stops once nothing is in flight.
  useEffect(() => {
    if (!libraryEnabled) return;
    const inFlight = libraryDocs.some(
      (d) =>
        d.status === "pending" ||
        d.status === "stored" ||
        d.status === "analyzing",
    );
    if (!inFlight) return;
    const tracked = new Set(libraryDocs.map((d) => d.id));
    const t = setInterval(async () => {
      try {
        const all = await api.listLibraryDocuments();
        const byId = new Map(all.map((d) => [d.id, d]));
        setLibraryDocs((prev) =>
          prev.map((d) => (tracked.has(d.id) ? byId.get(d.id) ?? d : d)),
        );
      } catch {
        /* best effort: keep the last-known status */
      }
    }, 3000);
    return () => clearInterval(t);
  }, [libraryEnabled, libraryDocs]);

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

      // Lazily create a session on the first message (shared with uploads).
      if (!sessionId) {
        try {
          sessionId = await ensureSession();
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
    [activeId, selectedModel, params, refreshSessions, ensureSession],
  );

  const stop = useCallback(() => {
    // Triggers the stream's AbortError -> onAbort -> finalize (which clears
    // the in-flight flag and reloads persisted messages).
    abortRef.current?.();
  }, []);

  const displayMessages: DisplayMessage[] = useMemo(() => {
    const base: DisplayMessage[] = messages
      .filter((m) => m.role !== "system")
      .map((m) => ({
        id: m.id,
        role: m.role,
        content: m.content,
        agent: m.agent,
        attachments: m.attachments,
      }));
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

  // Live voice is offered only when the runtime flag is on AND the catalog exposes
  // a realtime model (filtered from the same /api/models the picker uses). When it
  // is off, the control is never rendered and nothing about the chat UI changes.
  const realtimeModelId = useMemo(
    () => models.find((m) => m.category === "realtime")?.id ?? null,
    [models],
  );
  const voiceLiveEnabled = voiceLiveConfig.enabled && realtimeModelId !== null;

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      <Sidebar
        sessions={sessions}
        activeId={activeId}
        onSelect={selectSession}
        onNewChat={newChat}
        onDelete={deleteSession}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenStudio={() => setStudioOpen(true)}
        onOpenImagery={() => setImageryOpen(true)}
        onOpenLibrary={libraryEnabled ? () => setLibraryOpen(true) : undefined}
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
          <UserMenu />
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

        <MessageList messages={displayMessages} onError={setError} />
        <Composer
          disabled={streaming || !selectedModel}
          streaming={streaming}
          agents={agents}
          documents={documents}
          libraryDocuments={libraryDocs}
          uploading={uploading}
          onSend={send}
          onStop={stop}
          onUpload={uploadDocument}
          onRemoveDocument={removeDocument}
          onRemoveLibraryDocument={removeLibraryDocument}
          onError={setError}
          voiceLiveEnabled={voiceLiveEnabled}
          voiceLiveConfig={voiceLiveConfig}
          voiceLiveModel={realtimeModelId}
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
      {studioOpen && (
        <StudioPanel
          models={models}
          agents={agents}
          runModel={selectedModel}
          onAgentsChanged={refreshAgents}
          onRun={openWorkflowRun}
          onClose={() => setStudioOpen(false)}
        />
      )}
      {libraryOpen && libraryEnabled && (
        <LibraryPanel onClose={() => setLibraryOpen(false)} />
      )}
      {imageryOpen && (
        <ImageStudioPanel models={models} onClose={() => setImageryOpen(false)} />
      )}
    </div>
  );
}
