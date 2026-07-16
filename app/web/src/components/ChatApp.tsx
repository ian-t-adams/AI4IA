"use client";

// Main chat application shell. Orchestrates session lifecycle, message streaming,
// model selection and chat parameters, and the voice / library / custom-tools /
// media surfaces. Feature panels are hidden here when their env flag is off, but
// enforcement is server-side — the API is the authority (see app/api).

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
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
import {
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  isRealtimeVoice,
  isSpeechVoiceProvider,
  realtimeModels,
  resolveAuthorizedVoiceProviders,
  type SpeechVoiceLiveSettings,
  type VoiceLiveProviderCatalogResponse,
  type VoiceProvider,
} from "@/lib/voiceLive";
import {
  DEFAULT_VOICE_PREFERENCES,
  hasStoredVoicePreferences,
  loadVoicePreferences,
  normalizeSpeechVoiceLiveSettings,
  normalizeVoiceSessionSettings,
  resolveEffectiveAgent,
  resolveEffectiveModel,
  resolveEffectiveVoiceProvider,
  normalizeVoicePreferences,
  saveVoicePreferences,
  type VoicePreferences,
} from "@/lib/voicePreferences";
import { LibraryPanel } from "./LibraryPanel";
import { MediaPlayer } from "./MediaPlayer";
import { MessageList, type DisplayMessage } from "./MessageList";
import { Composer } from "./Composer";
import {
  InlineVoiceLiveStatus,
  mergeDisplayMessages,
  useInlineVoiceLive,
  voiceMessagesForSession,
} from "./InlineVoiceLive";
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

function reconcileMessages(
  previous: Message[],
  fresh: Message[],
  removeIds: ReadonlySet<string> = new Set(),
): Message[] {
  const byId = new Map(fresh.map((message) => [message.id, message]));
  for (const message of previous) {
    if (!removeIds.has(message.id) && !byId.has(message.id)) {
      byId.set(message.id, message);
    }
  }
  return [...byId.values()].sort(
    (left, right) =>
      Date.parse(left.createdAt) - Date.parse(right.createdAt) ||
      left.id.localeCompare(right.id),
  );
}

function getProviderDefaultVoice(provider: VoiceProvider): string {
  const voices = provider.capabilities.voices.options as readonly string[];
  return provider.capabilities.voices.default ?? voices[0] ?? "";
}

function sanitizeSpeechPreferences(
  speech: VoicePreferences["speech"],
  provider: VoiceProvider | undefined,
): VoicePreferences["speech"] {
  const normalized = normalizeSpeechVoiceLiveSettings(speech);
  const speechProvider = isSpeechVoiceProvider(provider) ? provider : undefined;
  const voices: readonly string[] =
    speechProvider?.capabilities.voices.options ?? [normalized.voice];
  const locales: readonly string[] =
    speechProvider?.capabilities.locale?.options ?? [normalized.locale];
  const transcriptions: readonly string[] =
    speechProvider?.capabilities.inputTranscription.options ?? [normalized.transcription];
  const turnDetections: readonly SpeechVoiceLiveSettings["turnDetection"][] =
    speechProvider?.capabilities.turnDetection.options ?? [normalized.turnDetection];
  const noiseSuppression: readonly SpeechVoiceLiveSettings["noiseSuppression"][] =
    speechProvider?.capabilities.noiseSuppression?.options ?? [normalized.noiseSuppression];
  const echoCancellation: readonly SpeechVoiceLiveSettings["echoCancellation"][] =
    speechProvider?.capabilities.echoCancellation?.options ?? [normalized.echoCancellation];
  return {
    ...normalized,
    voice:
      voices.includes(normalized.voice) && speechProvider
        ? normalized.voice
        : speechProvider
          ? getProviderDefaultVoice(speechProvider)
          : normalized.voice,
    locale:
      locales.includes(normalized.locale) && speechProvider
        ? normalized.locale
        : speechProvider?.capabilities.locale?.default ?? normalized.locale,
    transcription:
      transcriptions.includes(normalized.transcription) && speechProvider
        ? normalized.transcription
        : speechProvider?.capabilities.inputTranscription.default ?? normalized.transcription,
    turnDetection:
      turnDetections.includes(normalized.turnDetection) && speechProvider
        ? normalized.turnDetection
        : speechProvider?.capabilities.turnDetection.default ?? normalized.turnDetection,
    noiseSuppression:
      noiseSuppression.includes(normalized.noiseSuppression) && speechProvider
        ? normalized.noiseSuppression
        : speechProvider?.capabilities.noiseSuppression?.default ?? normalized.noiseSuppression,
    echoCancellation:
      echoCancellation.includes(normalized.echoCancellation) && speechProvider
        ? normalized.echoCancellation
        : speechProvider?.capabilities.echoCancellation?.default ?? normalized.echoCancellation,
  };
}

function sanitizeVoicePreferencesForProviders(
  prefs: VoicePreferences,
  providers: VoiceProvider[],
  realtimeModelIds: ReadonlySet<string>,
  defaultRealtimeModelId: string | null,
  voiceToolsAvailable: boolean,
  defaultProviderId: VoicePreferences["provider"],
  hasStoredPreferences: boolean,
): VoicePreferences {
  let provider = resolveEffectiveVoiceProvider(
    prefs.provider,
    providers.map((entry) => entry.id),
    defaultProviderId,
    hasStoredPreferences,
  );
  if (provider === "azure_openai" && realtimeModelIds.size === 0) {
    provider =
      (providers.find((entry) => entry.id === "speech_voice_live")?.id ??
        provider) as VoicePreferences["provider"];
  }
  const providerEntry =
    providers.find((entry) => entry.id === provider) ?? providers[0];
  const defaultModel = defaultRealtimeModelId ?? null;
  return {
    ...normalizeVoicePreferences(prefs),
    provider,
    model:
      provider === "azure_openai"
        ? resolveEffectiveModel(prefs.model, realtimeModelIds, defaultModel)
        : null,
    voice: isRealtimeVoice(prefs.voice) ? prefs.voice : DEFAULT_VOICE_PREFERENCES.voice,
    tools: voiceToolsAvailable && prefs.tools,
    settings: normalizeVoiceSessionSettings(prefs.settings),
    speech: sanitizeSpeechPreferences(prefs.speech, providerEntry),
  };
}

function providerModelRegion(models: ModelEntry[], modelId: string | null): string | null {
  if (!modelId) return null;
  const model = models.find((entry) => entry.id === modelId);
  return model?.options[0]?.region ?? null;
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
  const [voiceProviderConfig, setVoiceProviderConfig] =
    useState<VoiceLiveProviderCatalogResponse | null>(null);

  const [streaming, setStreaming] = useState(false);
  const [streamingText, setStreamingText] = useState("");
  const [streamingStartedAt, setStreamingStartedAt] = useState<string | null>(
    null,
  );
  // Live agent activity for the in-flight turn (tool steps streamed as they run).
  const [liveSteps, setLiveSteps] = useState<ActivityStep[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [studioOpen, setStudioOpen] = useState(false);
  const [imageryOpen, setImageryOpen] = useState(false);
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
  const voiceNavigationLockedRef = useRef(false);
  const voiceActiveRef = useRef(false);
  const voiceStopRef = useRef<() => void>(() => {});
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

  useEffect(() => {
    if (!voiceLiveConfig.enabled) return;
    let cancelled = false;
    void api
      .getVoiceLiveConfig()
      .then((config) => {
        if (!cancelled) setVoiceProviderConfig(config);
      })
      .catch(() => {
        if (!cancelled) setVoiceProviderConfig(null);
      });
    return () => {
      cancelled = true;
    };
  }, [voiceLiveConfig.enabled]);

  const refreshSessions = useCallback(async () => {
    try {
      setSessions(await api.listSessions());
    } catch {
      /* non-fatal */
    }
  }, []);

  const selectSession = useCallback(
    async (id: string) => {
      if (streamingRef.current || voiceNavigationLockedRef.current) return;
      if (id !== sessionIdRef.current && voiceActiveRef.current) {
        voiceStopRef.current();
      }
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
    if (streamingRef.current || voiceNavigationLockedRef.current) return;
    if (voiceActiveRef.current) voiceStopRef.current();
    setActiveId(null);
    setMessages([]);
    setDocuments([]);
    setLibraryDocs([]);
    setStreamingText("");
    setStreamingStartedAt(null);
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
      if (streamingRef.current || voiceNavigationLockedRef.current) return;
      if (id === activeId && voiceActiveRef.current) voiceStopRef.current();
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
    async (
      sessionId: string,
      conversationId: string,
      turns: VoiceTurnInput[],
    ) => {
      if (turns.length === 0) return;
      const created = await api.appendVoiceTurns(
        sessionId,
        conversationId,
        turns,
      );
      if (sessionIdRef.current === sessionId) {
        setMessages((previous) => {
          const createdIds = new Set(created.map((message) => message.id));
          return [
            ...previous.filter((message) => !createdIds.has(message.id)),
            ...created,
          ];
        });
      }
      void Promise.allSettled([
        api.listMessages(sessionId).then((fresh) => {
          if (sessionIdRef.current === sessionId) {
            setMessages((previous) => reconcileMessages(previous, fresh));
          }
        }),
        refreshSessions(),
      ]);
    },
    [refreshSessions],
  );

  // Voice Live settings disclosure: persisted picks (agent/model/voice/tools/
  // advanced settings), loaded once on mount (localStorage is client-only —
  // starting from the default keeps SSR/first paint stable).
  const [voicePrefs, setVoicePrefs] = useState<VoicePreferences>(
    DEFAULT_VOICE_PREFERENCES,
  );
  const [hasSavedVoicePrefs, setHasSavedVoicePrefs] = useState(false);
  useEffect(() => {
    setHasSavedVoicePrefs(hasStoredVoicePreferences());
    setVoicePrefs(loadVoicePreferences());
  }, []);
  const updateVoicePrefs = useCallback((next: VoicePreferences) => {
    setHasSavedVoicePrefs(true);
    setVoicePrefs(next);
    saveVoicePreferences(next);
  }, []);

  const realtimeModelList = useMemo(() => realtimeModels(models), [models]);
  const authorizedVoiceProviders = useMemo(
    () => resolveAuthorizedVoiceProviders(voiceProviderConfig),
    [voiceProviderConfig],
  );
  const voiceProviders = authorizedVoiceProviders.providers;
  const voiceLiveEnabled = Boolean(
    voiceLiveConfig.enabled &&
      authorizedVoiceProviders.defaultProviderId &&
      voiceProviders.some(
        (provider) =>
          provider.selectionMode === "fixed_managed_model" ||
          realtimeModelList.length > 0,
      ),
  );
  const voicePrefsResolved = useMemo(
    () => {
      if (!authorizedVoiceProviders.defaultProviderId) {
        return {
          ...normalizeVoicePreferences(voicePrefs),
          tools: false,
        };
      }
      return sanitizeVoicePreferencesForProviders(
        voicePrefs,
        voiceProviders,
        new Set(realtimeModelList.map((model) => model.id)),
        realtimeModelList[0]?.id ?? null,
        voiceLiveConfig.toolsAvailable,
        authorizedVoiceProviders.defaultProviderId,
        hasSavedVoicePrefs,
      );
    },
    [
      authorizedVoiceProviders.defaultProviderId,
      hasSavedVoicePrefs,
      realtimeModelList,
      voiceLiveConfig.toolsAvailable,
      voicePrefs,
      voiceProviders,
    ],
  );
  const currentVoiceAgent = useMemo(() => {
    const enabled = new Set(
      agents.filter((agent) => agent.enabled).map((agent) => agent.name),
    );
    for (let i = messages.length - 1; i >= 0; i--) {
      const agent = messages[i].agent;
      if (
        messages[i].role === "assistant" &&
        agent &&
        enabled.has(agent)
      ) {
        return agent;
      }
    }
    return null;
  }, [agents, messages]);

  const enabledAgentNames = useMemo(
    () => new Set(agents.filter((a) => a.enabled).map((a) => a.name)),
    [agents],
  );
  // The explicit pick (agent/model) wins when it is still valid; otherwise the
  // active chat's current agent / the catalog default. Switching the active
  // chat agent never overwrites a stored explicit pick — this is re-derived
  // from the unchanged stored value on every render, so a stale pick resumes
  // automatically once it becomes valid again (e.g. the agent is re-enabled).
  const effectiveVoiceAgent = resolveEffectiveAgent(
    voicePrefsResolved.explicitAgent,
    enabledAgentNames,
    currentVoiceAgent,
  );
  const effectiveVoiceModel =
    voicePrefsResolved.provider === "azure_openai" ? voicePrefsResolved.model : null;
  const effectiveVoiceRegion = providerModelRegion(
    realtimeModelList,
    effectiveVoiceModel,
  );
  const voiceToolsAvailable = voiceLiveEnabled && voiceLiveConfig.toolsAvailable;
  const activeVoiceProvider =
    voiceProviders.find((provider) => provider.id === voicePrefsResolved.provider) ??
    voiceProviders[0];
  const activeVoiceVoice =
    voicePrefsResolved.provider === "speech_voice_live"
      ? voicePrefsResolved.speech.voice
      : voicePrefsResolved.voice;

  const authorizedVoiceLiveConfig = useMemo(
    () => ({ ...voiceLiveConfig, enabled: voiceLiveEnabled }),
    [voiceLiveConfig, voiceLiveEnabled],
  );
  const inlineVoice = useInlineVoiceLive({
    config: authorizedVoiceLiveConfig,
    providerId: voicePrefsResolved.provider,
    model: effectiveVoiceModel,
    region: effectiveVoiceRegion,
    voice: activeVoiceVoice,
    agent: effectiveVoiceAgent,
    agents,
    history: voiceHistory,
    settings: voicePrefsResolved.settings,
    speechSettings: voicePrefsResolved.speech,
    tools: voiceToolsAvailable && voicePrefsResolved.tools,
    activeSessionId: activeId,
    ensureSession,
    persistConversation: persistVoiceConversation,
  });
  // Session/chat navigation is blocked only while there is voice data that
  // would actually be lost — an open mic with no exchanges yet never blocks a
  // switch (see useInlineVoiceLive.hasUnsavedTurns).
  const voiceExitLocked = inlineVoice.exitLocked;
  useLayoutEffect(() => {
    voiceNavigationLockedRef.current = voiceExitLocked;
    voiceActiveRef.current = inlineVoice.active;
    voiceStopRef.current = inlineVoice.stop;
  }, [inlineVoice.active, inlineVoice.stop, voiceExitLocked]);
  useEffect(() => {
    if (!voiceExitLocked) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [voiceExitLocked]);

  // Props for the compact inline Voice settings disclosure (agent, realtime
  // model, voice, governed tools, advanced session settings). Built once here
  // so both the labels (which need the live catalog/agent list) and the
  // persisted picks stay in one place; Composer only adds the transient
  // `locked` flag (derived from the live connection's own status).
  const voiceSettingsProps = useMemo(
    () =>
      activeVoiceProvider
        ? {
            agents,
            providers: voiceProviders.map((provider) => ({
              id: provider.id,
              displayLabel: provider.displayLabel,
              description: provider.description,
            })),
            provider: voicePrefsResolved.provider,
            onProviderChange: (nextProvider: VoicePreferences["provider"]) =>
              updateVoicePrefs({ ...voicePrefsResolved, provider: nextProvider }),
            activeProvider: activeVoiceProvider,
            defaultAgentLabel: currentVoiceAgent
              ? `Current chat agent (${
                  agents.find((a) => a.name === currentVoiceAgent)?.displayName ??
                  currentVoiceAgent
                })`
              : "Current chat agent (generic assistant)",
            explicitAgent: voicePrefsResolved.explicitAgent,
            onAgentChange: (nextAgent: string | null) =>
              updateVoicePrefs({ ...voicePrefsResolved, explicitAgent: nextAgent }),
            models: realtimeModelList.map((m) => ({
              id: m.id,
              displayName: m.displayName,
            })),
            defaultModelLabel: realtimeModelList[0]
              ? `Default (${realtimeModelList[0].displayName})`
              : "Default",
            explicitModel: voicePrefsResolved.model,
            onModelChange: (nextModel: string | null) =>
              updateVoicePrefs({ ...voicePrefsResolved, model: nextModel }),
            voice: activeVoiceVoice,
            onVoiceChange: (nextVoice: string) =>
              voicePrefsResolved.provider === "speech_voice_live"
                ? updateVoicePrefs({
                    ...voicePrefsResolved,
                    speech: { ...voicePrefsResolved.speech, voice: nextVoice },
                  })
                : updateVoicePrefs({
                    ...voicePrefsResolved,
                    voice: nextVoice as VoicePreferences["voice"],
                  }),
            toolsAvailable: voiceToolsAvailable,
            tools: voicePrefsResolved.tools,
            onToolsChange: (nextTools: boolean) =>
              updateVoicePrefs({ ...voicePrefsResolved, tools: nextTools }),
            settings: voicePrefsResolved.settings,
            onSettingsChange: (nextSettings: VoicePreferences["settings"]) =>
              updateVoicePrefs({ ...voicePrefsResolved, settings: nextSettings }),
            speechSettings: voicePrefsResolved.speech,
            onSpeechSettingsChange: (nextSpeech: VoicePreferences["speech"]) =>
              updateVoicePrefs({ ...voicePrefsResolved, speech: nextSpeech }),
            onReset: () =>
              updateVoicePrefs(
                voicePrefsResolved.provider === "speech_voice_live"
                  ? {
                      ...voicePrefsResolved,
                      speech: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
                    }
                  : {
                      ...voicePrefsResolved,
                      voice: DEFAULT_VOICE_PREFERENCES.voice,
                      model: null,
                      settings: DEFAULT_VOICE_PREFERENCES.settings,
                    },
              ),
          }
        : undefined,
    [
      agents,
      currentVoiceAgent,
      realtimeModelList,
      activeVoiceProvider,
      activeVoiceVoice,
      updateVoicePrefs,
      voicePrefsResolved,
      voiceProviders,
      voiceToolsAvailable,
    ],
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

      const userCreatedAt = new Date();
      const optimisticUser: Message = {
        id: `tmp-${Date.now()}`,
        sessionId,
        userId: "me",
        role: "user",
        content,
        status: "complete",
        model: selectedModel,
        agent: null,
        createdAt: userCreatedAt.toISOString(),
      };
      setStreamingStartedAt(
        new Date(userCreatedAt.getTime() + 1).toISOString(),
      );
      setMessages((prev) => [...prev, optimisticUser]);

      const isCommand = content.trimStart().startsWith("/");
      const finalize = async () => {
        streamingRef.current = false;
        setStreaming(false);
        abortRef.current = null;
        try {
          const fresh = await api.listMessages(sessionId!);
          setMessages((previous) =>
            reconcileMessages(
              previous,
              fresh,
              new Set([optimisticUser.id]),
            ),
          );
        } catch {
          /* keep optimistic view */
        }
        setStreamingText("");
        setStreamingStartedAt(null);
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
        createdAt: m.createdAt,
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
        createdAt: streamingStartedAt ?? undefined,
        pending: true,
        steps: liveSteps,
      });
    }
    return mergeDisplayMessages(
      base,
      voiceMessagesForSession(
        inlineVoice.messages,
        inlineVoice.boundSessionId,
        activeId,
      ),
    );
  }, [
    messages,
    streaming,
    streamingText,
    streamingStartedAt,
    liveSteps,
    inlineVoice.messages,
    inlineVoice.boundSessionId,
    activeId,
  ]);

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
          disabled={streaming || voiceExitLocked}
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
          <AdminLink disabled={voiceExitLocked} />
          <UserMenu disabled={voiceExitLocked} />
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
        <InlineVoiceLiveStatus voice={inlineVoice} />
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
          voiceLive={
            voiceLiveEnabled && voiceSettingsProps
              ? {
                  active: inlineVoice.active,
                  supported: inlineVoice.supported,
                  connecting: inlineVoice.phase === "connecting",
                  ending: inlineVoice.phase === "ending",
                  saving: inlineVoice.saving,
                  saveBlocked: Boolean(inlineVoice.persistenceError),
                  retrying: Boolean(inlineVoice.error),
                  start: inlineVoice.start,
                  stop: inlineVoice.stop,
                  settings: voiceSettingsProps,
                }
              : undefined
          }
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
    </div>
  );
}
