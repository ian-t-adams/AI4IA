"use client";

// Main chat application shell. Orchestrates session lifecycle, message streaming,
// model selection and chat parameters, and the voice / library / custom-tools /
// media surfaces. Feature panels are hidden here when their env flag is off, but
// enforcement is server-side — the API is the authority (see app/api).

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import * as api from "@/lib/api";
import type { ActivityStep, AgentSummary, ChatParams, DocumentSummary, Message, ModelEntry, Session, VoiceTurnInput } from "@/lib/types";
import type { LibraryDocument } from "@/lib/library";
import { Sidebar } from "./Sidebar";
import { ModelPicker } from "./ModelPicker";
import { ParamControls } from "./ParamControls";
import { SystemPromptEditor } from "./SystemPromptEditor";
import { SettingsPanel } from "./SettingsPanel";
import { StudioPanel } from "./StudioPanel";
import { ImageStudioPanel } from "./ImageStudioPanel";
import { VoiceLivePanel } from "./VoiceLivePanel";
import { realtimeModels } from "@/lib/voiceLive";
import { LibraryPanel } from "./LibraryPanel";
import { MediaPlayer } from "./MediaPlayer";
import { MessageList, type DisplayMessage } from "./MessageList";
import { Composer } from "./Composer";
import { UserMenu } from "./UserMenu";
import { AdminLink } from "./AdminLink";
import { DOCS_PORTAL_URL } from "@/lib/docs";
import { useVoiceLiveConfig } from "./VoiceLiveProvider";
import { useLibraryConfig } from "./LibraryProvider";
import { useCustomToolsConfig } from "./CustomToolsProvider";

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
  const customToolsConfig = useCustomToolsConfig();
  const customToolsEnabled = customToolsConfig.enabled;
  // The document library. When on, the Composer paperclip routes
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
  // Live agent activity for the in-flight turn (tool steps streamed as they run).
  const [liveSteps, setLiveSteps] = useState<ActivityStep[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [studioOpen, setStudioOpen] = useState(false);
  const [imageryOpen, setImageryOpen] = useState(false);
  const [voiceOpen, setVoiceOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  // Left sidebar + right parameters panel collapse state, persisted across
  // reloads. Initialized false (matching SSR) and hydrated from localStorage on
  // mount to avoid a hydration mismatch.
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  // Citation deep-link: the audio/video doc a clicked chat citation
  // resolved to, plus the moment to seek. Opens the same MediaPlayer modal the
  // LibraryPanel uses. Null when no citation is open.
  const [citationTarget, setCitationTarget] = useState<{
    doc: LibraryDocument;
    seekToMs?: number;
  } | null>(null);
  // Cache of the user's full library, lazily fetched the first time a citation is
  // clicked so resolution doesn't pay a round-trip on every chip.
  const libraryIndexRef = useRef<LibraryDocument[] | null>(null);

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

  // Recent text-chat turns handed to Voice Live so a live session opens with the
  // conversation's context (the hook caps how much it actually replays). System
  // turns are excluded; empties are dropped.
  const voiceHistory = useMemo<VoiceTurnInput[]>(
    () =>
      messages
        .filter((m) => m.role !== "system" && m.content.trim())
        .map((m) => ({ role: m.role as "user" | "assistant", text: m.content })),
    [messages],
  );

  // Persist a finished Voice Live exchange back into the shared session so voice
  // turns land in the text transcript and the user can keep typing in the same
  // conversation. Lazily creates the session if the live chat was the first turn.
  const persistVoiceConversation = useCallback(
    async (turns: VoiceTurnInput[]) => {
      if (turns.length === 0) return;
      try {
        const sid = await ensureSession();
        await api.appendVoiceTurns(sid, turns);
        if (sessionIdRef.current === sid) {
          setMessages(await api.listMessages(sid));
        }
        await refreshSessions();
      } catch (e) {
        setError((e as Error).message);
      }
    },
    [ensureSession, refreshSessions],
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

  // Resolve a clicked chat citation to a ready audio/video library
  // document and open the player at the cited moment. Citations name a file by
  // its filename (what the model is given + told to cite), so we match
  // case-insensitively against the user's ready media; the first match wins on the
  // rare duplicate-name case. Best-effort: a miss surfaces a soft error, never
  // throws into the message list.
  const handleCitation = useCallback(
    async (filename: string, ms: number) => {
      if (!libraryEnabled) return;
      const resolve = (docs: LibraryDocument[]) =>
        docs.find(
          (d) =>
            d.status === "ready" &&
            (d.modality === "audio" || d.modality === "video") &&
            d.filename.toLowerCase() === filename.toLowerCase(),
        );
      let doc = libraryIndexRef.current
        ? resolve(libraryIndexRef.current)
        : undefined;
      if (!doc) {
        try {
          const all = await api.listLibraryDocuments();
          libraryIndexRef.current = all;
          doc = resolve(all);
        } catch {
          setError("Couldn't open the cited media.");
          return;
        }
      }
      if (doc) {
        setCitationTarget({ doc, seekToMs: ms });
      } else {
        setError(`Couldn't find a playable document named "${filename}".`);
      }
    },
    [libraryEnabled],
  );
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
      setLiveSteps([]);
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
        setLiveSteps([]);
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
          onStep: (step) => setLiveSteps((prev) => [...prev, step]),
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
        source: m.source,
        steps: m.steps,
      }));
    if (streaming) {
      base.push({
        id: "streaming",
        role: "assistant",
        content: streamingText,
        pending: true,
        steps: liveSteps,
      });
    }
    return base;
  }, [messages, streaming, streamingText, liveSteps]);

  // Live voice is offered only when the runtime flag is on AND the catalog exposes
  // at least one realtime model (filtered from the same /api/models the picker
  // uses). The full realtime list is handed to the panel's model picker; when the
  // flag is off, the control is never rendered and nothing about the chat UI changes.
  const realtimeModelList = useMemo(() => realtimeModels(models), [models]);
  const voiceLiveEnabled = voiceLiveConfig.enabled && realtimeModelList.length > 0;

  // Hydrate panel-collapse preferences from localStorage on mount (after SSR).
  useEffect(() => {
    try {
      setLeftCollapsed(localStorage.getItem("ai4ia.leftCollapsed") === "1");
      setRightCollapsed(localStorage.getItem("ai4ia.rightCollapsed") === "1");
    } catch {
      // localStorage unavailable (private mode etc.) — keep expanded defaults.
    }
  }, []);

  const toggleLeftCollapsed = useCallback(() => {
    setLeftCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ai4ia.leftCollapsed", next ? "1" : "0");
      } catch {
        // best-effort persistence
      }
      return next;
    });
  }, []);

  const toggleRightCollapsed = useCallback(() => {
    setRightCollapsed((prev) => {
      const next = !prev;
      try {
        localStorage.setItem("ai4ia.rightCollapsed", next ? "1" : "0");
      } catch {
        // best-effort persistence
      }
      return next;
    });
  }, []);

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      {leftCollapsed ? (
        <div
          aria-label="Chat sessions (collapsed)"
          style={{
            width: 48,
            flexShrink: 0,
            background: "var(--bg-sidebar)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            paddingTop: 16,
            gap: 12,
          }}
        >
          {/* eslint-disable-next-line @next/next/no-img-element -- small static brand mark */}
          <img
            src="/ai4ia-mark.png"
            alt=""
            aria-hidden="true"
            width={28}
            height={28}
            style={{ borderRadius: 6, display: "block" }}
          />
          <button
            onClick={toggleLeftCollapsed}
            aria-label="Expand sidebar"
            title="Expand sidebar"
            style={{
              border: "none",
              background: "transparent",
              color: "var(--sidebar-muted)",
              cursor: "pointer",
              fontSize: "1.1em",
              lineHeight: 1,
              padding: 4,
            }}
          >
            »
          </button>
        </div>
      ) : (
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
          onCollapse={toggleLeftCollapsed}
          disabled={streaming}
        />
      )}

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
          <a
            href={DOCS_PORTAL_URL}
            target="_blank"
            rel="noopener noreferrer"
            title="Open the AI4IA documentation and live status portal"
            style={{
              fontSize: "0.8em",
              padding: "4px 10px",
              borderRadius: 6,
              border: "1px solid var(--border)",
              background: "var(--bg-elevated)",
              color: "var(--fg)",
              textDecoration: "none",
              whiteSpace: "nowrap",
            }}
          >
            Docs &amp; status
          </a>
          <AdminLink />
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

        <MessageList
          messages={displayMessages}
          onError={setError}
          onCitation={libraryEnabled ? handleCitation : undefined}
        />
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
          onStartVoice={voiceLiveEnabled ? () => setVoiceOpen(true) : undefined}
        />
      </main>

      {rightCollapsed ? (
        <aside
          aria-label="Model parameters (collapsed)"
          style={{
            width: 44,
            flexShrink: 0,
            borderLeft: "1px solid var(--border)",
            background: "var(--bg-elevated)",
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            paddingTop: 20,
          }}
        >
          <button
            onClick={toggleRightCollapsed}
            aria-label="Expand parameters panel"
            title="Parameters"
            style={{
              border: "none",
              background: "transparent",
              color: "var(--fg-muted)",
              cursor: "pointer",
              fontSize: "1.1em",
              lineHeight: 1,
              padding: 4,
            }}
          >
            «
          </button>
        </aside>
      ) : (
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
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
            }}
          >
            <span
              style={{
                fontWeight: 600,
                fontSize: "0.8em",
                letterSpacing: 0.5,
                textTransform: "uppercase",
                color: "var(--fg-muted)",
              }}
            >
              Parameters
            </span>
            <button
              onClick={toggleRightCollapsed}
              aria-label="Collapse parameters panel"
              title="Collapse panel"
              style={{
                border: "none",
                background: "transparent",
                color: "var(--fg-muted)",
                cursor: "pointer",
                fontSize: "1.1em",
                lineHeight: 1,
                padding: 4,
              }}
            >
              »
            </button>
          </div>
          <ParamControls
            params={params}
            onChange={setParams}
            model={models.find((m) => m.id === selectedModel) ?? null}
          />
          <SystemPromptEditor value={systemPrompt} onSave={saveSystemPrompt} />
        </aside>
      )}

      {settingsOpen && (
        <SettingsPanel models={models} onClose={() => setSettingsOpen(false)} />
      )}
      {studioOpen && (
        <StudioPanel
          models={models}
          agents={agents}
          runModel={selectedModel}
          customToolsEnabled={customToolsEnabled}
          onAgentsChanged={refreshAgents}
          onRun={openWorkflowRun}
          onClose={() => setStudioOpen(false)}
        />
      )}
      {libraryOpen && libraryEnabled && (
        <LibraryPanel onClose={() => setLibraryOpen(false)} />
      )}
      {citationTarget && libraryEnabled && (
        <MediaPlayer
          doc={citationTarget.doc}
          seekToMs={citationTarget.seekToMs}
          onClose={() => setCitationTarget(null)}
        />
      )}
      {imageryOpen && (
        <ImageStudioPanel models={models} onClose={() => setImageryOpen(false)} />
      )}
      {voiceOpen && voiceLiveEnabled && (
        <VoiceLivePanel
          config={voiceLiveConfig}
          models={realtimeModelList}
          agents={agents}
          onClose={() => setVoiceOpen(false)}
          onError={setError}
          history={voiceHistory}
          onConversation={persistVoiceConversation}
        />
      )}
    </div>
  );
}
