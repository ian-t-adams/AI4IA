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
import type {
  ActivityStep,
  AgentSummary,
  AttachmentCapabilities,
  ChatParams,
  ConversationDraftDefaults,
  DocumentSummary,
  Message,
  ModelEntry,
  Session,
  VoiceTurnInput,
} from "@/lib/types";
import type { LibraryDocument } from "@/lib/library";
import { Sidebar } from "./Sidebar";
import { ConversationInspector } from "./ConversationInspector";
import { SettingsPanel } from "./SettingsPanel";
import { StudioPanel } from "./StudioPanel";
import {
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_SPEECH_MODEL_ID,
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
import { Composer, type UploadItem } from "./Composer";
import {
  InlineVoiceLiveStatus,
  mergeDisplayMessages,
  useInlineVoiceLive,
  voiceMessagesForSession,
} from "./InlineVoiceLive";
import { useVoiceLiveConfig } from "./VoiceLiveProvider";
import { useLibraryConfig } from "./LibraryProvider";
import { useCustomToolsConfig } from "./CustomToolsProvider";
import { useMediaQuery } from "./useMediaQuery";
import {
  closeUnavailableMobileDrawer,
  toggleMobileDrawer as nextMobileDrawer,
  type MobileDrawer,
} from "@/lib/workspaceLayout";
import {
  commitLatestSessionMutation,
  isCurrentSessionGeneration,
} from "@/lib/sessionMutation";
import { performBoundUpload } from "@/lib/uploadSession";
import { EditableSessionTitle } from "./EditableSessionTitle";

const MOBILE_SIDEBAR_QUERY = "(max-width: 720px)";
const MOBILE_INSPECTOR_QUERY = "(max-width: 1050px)";

function newClientTurnId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10).join(""),
  ].join("-");
}

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
  const byId = new Map<string, Message>();
  for (const message of previous) {
    if (!removeIds.has(message.id)) byId.set(message.id, message);
  }
  for (const message of fresh) {
    const existing = byId.get(message.id);
    if (!existing) {
      byId.set(message.id, message);
      continue;
    }
    const existingTerminal = existing.status !== "streaming";
    if (existingTerminal && message.status === "streaming") continue;
    byId.set(message.id, {
      ...message,
      content: message.content || existing.content,
      steps:
        finalizedSteps(message.steps ?? existing.steps ?? []) ??
        message.steps ??
        existing.steps,
    });
  }
  return [...byId.values()].sort(
    (left, right) =>
      Date.parse(left.createdAt) - Date.parse(right.createdAt) ||
      left.id.localeCompare(right.id),
  );
}

// The backend upserts a "streaming" placeholder before the model call, then
// rewrites it with the final content only after SSE sends `[DONE]` (chat.py).
// A refetch right after `[DONE]` can race that upsert and see the placeholder.
// Every new browser turn has a clientTurnId before POST. Server row ids are a
// rolling-deployment fallback only; exact clientTurnId + source is authoritative.
function correlatedRows(messages: Message[], pending: PendingTurn): Message[] {
  const byClientTurn = messages.filter(
    (message) =>
      (message.source ?? "chat") === pending.source &&
      message.clientTurnId === pending.clientTurnId,
  );
  if (byClientTurn.length) return byClientTurn;
  // Rolling deployment compatibility with the previous server contract, which
  // emitted durable row ids over SSE but did not yet persist clientTurnId.
  const oldServerIds = new Set(
    [pending.userMessageId, pending.assistantMessageId].filter(
      (id): id is string => Boolean(id),
    ),
  );
  return oldServerIds.size
    ? messages.filter((message) => oldServerIds.has(message.id))
    : [];
}

function isReconciliationStale(
  messages: Message[],
  pending: PendingTurn,
): boolean {
  const row = correlatedRows(messages, pending).find(
    (message) => message.role === "assistant",
  );
  return !row || row.status === "streaming";
}

const RECONCILE_MAX_ATTEMPTS = 2;
const RECONCILE_RETRY_DELAY_MS = 150;

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

// Retries briefly on a stale result (see isReconciliationStale). Returns
// null -- not partial data -- on a throw or exhausted retries, so the
// caller can fall back to its own buffer instead of trusting stale data.
// `isCancelled` is checked before/after each network wait so an abandoned
// turn (unmount, or the user navigated to a different session) stops
// polling instead of retrying against a conversation no one is watching.
async function fetchReconciledMessages(
  sessionId: string,
  pending: PendingTurn,
  isCancelled: () => boolean,
  signal: AbortSignal,
): Promise<Message[] | null> {
  try {
    for (let attempt = 0; attempt < RECONCILE_MAX_ATTEMPTS; attempt++) {
      if (isCancelled()) return null;
      const candidate = await api.listMessages(sessionId, signal);
      if (isCancelled()) return null;
      if (!isReconciliationStale(candidate, pending)) return candidate;
      if (attempt < RECONCILE_MAX_ATTEMPTS - 1) {
        await wait(RECONCILE_RETRY_DELAY_MS, signal);
        if (isCancelled()) return null;
      }
    }
  } catch {
    return null;
  }
  return null;
}

// A turn tracked from the moment its stream finalizes until an authoritative
// fetch confirms its persisted reply. `assistantMessageId`/`userMessageId`
// are the backend's own already-stable Message.id values for this turn's
// rows -- echoed to the client early via a dedicated SSE event (see
// StreamHandlers.onMessageIds in api.ts) -- so resolution below is an exact
// id match rather than a timestamp heuristic: immune to clock skew, and
// structurally unable to match an unrelated turn (an interleaved Voice Live
// exchange gets ids from an entirely different namespace, so it can never
// be mistaken for, or need special-casing against, a concurrent text turn).
// The row ids remain optional for compatibility with the immediately preceding
// server version; new servers persist clientTurnId on local and model replies.
interface PendingTurn {
  sessionId: string;
  generation: number;
  clientTurnId: string;
  source: "chat";
  knownMessageIds: ReadonlySet<string>;
  optimisticUserId: string;
  placeholderId: string | null;
  assistantMessageId?: string;
  userMessageId?: string;
}

// Resolves and/or prunes `pendingTurns` (mutated in place) against an
// authoritative fetch for `fetchedSessionId`, without touching `messages`
// itself -- callers decide how (or whether) to merge. A turn resolves once
// its own tracked assistantMessageId appears in `fresh` with a non-
// "streaming" status (or trivially, on the first check, if it never had an
// id -- see PendingTurn). `removeIds` are this call's now-safe-to-drop local
// bubbles (optimistic user + any local fallback placeholder); `openRowIds`
// are ids of `fresh` rows that belong to a still-unresolved turn -- both the
// assistant placeholder *and* its already-persisted user message, since the
// latter would otherwise duplicate the still-showing optimistic user bubble
// -- and so must be excluded from whatever the caller merges into `messages`.
function resolvePendingTurns(
  pendingTurns: Map<string, PendingTurn>,
  fetchedSessionId: string,
  fresh: Message[],
  activeGeneration: number,
): { removeIds: Set<string>; openRowIds: Set<string> } {
  const removeIds = new Set<string>();
  const openRowIds = new Set<string>();
  for (const [clientTurnId, entry] of [...pendingTurns.entries()]) {
    if (entry.sessionId !== fetchedSessionId) continue;
    // A stale-generation entry's messages were already wiped by whatever
    // reselect bumped the generation; nothing left here to resolve.
    if (entry.generation !== activeGeneration) {
      pendingTurns.delete(clientTurnId);
      continue;
    }
    const rows = correlatedRows(fresh, entry);
    const persistedAssistant = rows.find(
      (message) => message.role === "assistant",
    );
    const resolved =
      persistedAssistant !== undefined &&
      persistedAssistant.status !== "streaming";
    if (resolved) {
      removeIds.add(entry.optimisticUserId);
      if (entry.placeholderId) removeIds.add(entry.placeholderId);
      pendingTurns.delete(clientTurnId);
    } else {
      for (const row of rows) openRowIds.add(row.id);
      if (
        rows.length === 0 &&
        !entry.assistantMessageId &&
        !entry.userMessageId
      ) {
        // A fully old server neither persisted clientTurnId nor emitted row ids.
        // Such rows cannot be safely attributed to this turn. Keep only rows that
        // were already known before POST; never guess by timestamp/content.
        for (const message of fresh) {
          if (
            (message.source ?? "chat") === entry.source &&
            !message.clientTurnId &&
            !entry.knownMessageIds.has(message.id)
          ) {
            openRowIds.add(message.id);
          }
        }
      }
    }
  }
  return { removeIds, openRowIds };
}

// Mirrors the backend's persistence contract (runtime.py persist=False on
// tool_start): drops the in-flight "running X" marker so a client-built
// placeholder never shows a step that looks permanently unresolved.
function finalizedSteps(steps: ActivityStep[]): ActivityStep[] | undefined {
  const finalized = steps.filter((step) => step.kind !== "tool_start");
  return finalized.length ? finalized : undefined;
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
  const speechProvider = providers.find(
    (entry) => entry.id === "speech_voice_live",
  );
  const defaultModel = defaultRealtimeModelId ?? null;
  const speechModelIds = isSpeechVoiceProvider(speechProvider)
    ? new Set(speechProvider.managedModels.map((model) => model.id))
    : new Set<string>();
  const defaultSpeechModel = isSpeechVoiceProvider(speechProvider)
    ? speechProvider.defaultManagedModelId
    : DEFAULT_SPEECH_MODEL_ID;
  return {
    ...normalizeVoicePreferences(prefs),
    provider,
    model: resolveEffectiveModel(prefs.model, realtimeModelIds, defaultModel),
    speechModel: speechModelIds.has(prefs.speechModel)
      ? prefs.speechModel
      : speechModelIds.has(defaultSpeechModel)
        ? defaultSpeechModel
        : [...speechModelIds][0] ?? DEFAULT_SPEECH_MODEL_ID,
    voice: isRealtimeVoice(prefs.voice) ? prefs.voice : DEFAULT_VOICE_PREFERENCES.voice,
    tools: voiceToolsAvailable && prefs.tools,
    settings: normalizeVoiceSessionSettings(prefs.settings),
    speech: sanitizeSpeechPreferences(prefs.speech, speechProvider),
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
  const messagesRef = useRef<Message[]>([]);
  const [documents, setDocuments] = useState<DocumentSummary[]>([]);
  // Library docs attached via the paperclip this view (only used when
  // libraryEnabled). Transient: cleared on new chat / session switch, but the
  // doc itself persists in the user's library and stays available to the agent.
  const [libraryDocs, setLibraryDocs] = useState<LibraryDocument[]>([]);
  const [uploading, setUploading] = useState(false);
  const [attachmentCapabilities, setAttachmentCapabilities] =
    useState<AttachmentCapabilities | null>(null);
  const [attachmentCapabilitiesError, setAttachmentCapabilitiesError] =
    useState<string | null>(null);
  const [uploads, setUploads] = useState<UploadItem[]>([]);
  const uploadTargetsRef = useRef(
    new Map<
      string,
      {
        file: File;
        sessionId: string | null;
        selectionGeneration: number;
      }
    >(),
  );
  const uploadChainRef = useRef<Promise<void>>(Promise.resolve());
  const activeUploadCountRef = useRef(0);
  const [inspectorVersion, setInspectorVersion] = useState(0);

  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [params, setParams] = useState<ChatParams>({
    temperature: 0.7,
    top_p: 1,
    max_tokens: 1024,
  });
  const [systemPrompt, setSystemPrompt] = useState("");
  const [draftDefaults, setDraftDefaults] = useState<ConversationDraftDefaults>({
    agentName: null,
    toolOverrides: { added: [], removed: [] },
    libraryDocumentIds: [],
  });
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
  const [libraryOpen, setLibraryOpen] = useState(false);
  // Left sidebar + right parameters panel collapse state, persisted across
  // reloads. Initialized false (matching SSR) and hydrated from localStorage on
  // mount to avoid a hydration mismatch.
  const [leftCollapsed, setLeftCollapsed] = useState(false);
  const [rightCollapsed, setRightCollapsed] = useState(false);
  const [mobileDrawer, setMobileDrawer] = useState<MobileDrawer>(null);
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
  // Monotonic per-turn token: identifies each send() call so its own
  // finalize() can tell whether it is still the latest turn (used to gate
  // the shared live-preview state, which only one turn owns at a time).
  const turnCounterRef = useRef(0);
  // Turns whose stream has finalized but whose persisted reply isn't yet
  // confirmed. Keyed by turnId so applyReconciledMessages can resolve each
  // turn independently -- an earlier turn stuck behind a slow/failed refetch
  // must not be clobbered or duplicated by a later turn's own reconciliation.
  const pendingTurnsRef = useRef<Map<string, PendingTurn>>(new Map());
  // Set false on unmount so an in-flight reconciliation retry/wait stops
  // instead of touching state after the component is gone.
  const mountedRef = useRef(true);
  const historySequenceRef = useRef(0);
  const latestHistoryRequestRef = useRef<Map<string, number>>(new Map());
  const historyControllersRef = useRef<Set<AbortController>>(new Set());
  // Holds an in-flight lazy session-creation promise so a rapid send + upload
  // (or two uploads) share a single session instead of racing to create two.
  const creatingRef = useRef<Promise<string> | null>(null);
  // Synchronous mirror of activeId so ensureSession sees a just-created session
  // immediately (before the setActiveId state flush), preventing a double create
  // when an upload is quickly followed by a send.
  const sessionIdRef = useRef<string | null>(null);
  const selectionGenerationRef = useRef(0);
  const modelMutationGenerationRef = useRef(0);
  const capabilityGenerationRef = useRef(0);
  const voiceNavigationLockedRef = useRef(false);
  const voiceActiveRef = useRef(false);
  const voiceStopRef = useRef<() => void>(() => {});
  const sidebarOpenerRef = useRef<HTMLButtonElement>(null);
  const sidebarReturnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    sessionIdRef.current = activeId;
  }, [activeId]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);
  useEffect(() => {
    mountedRef.current = true;
    // Captured once per mount: these containers are stable, mutated-in-place
    // refs (never reassigned), so closing over them here -- rather than
    // re-reading `.current` inside the cleanup -- satisfies the ref-in-effect
    // lint rule without changing behavior.
    const historyControllers = historyControllersRef.current;
    const pendingTurns = pendingTurnsRef.current;
    return () => {
      mountedRef.current = false;
      abortRef.current?.();
      for (const controller of historyControllers) controller.abort();
      historyControllers.clear();
      pendingTurns.clear();
    };
  }, []);

  // Resolves and/or removes tracked pending turns against an authoritative
  // fetch for `fetchedSessionId`, then merges the result into `messages`.
  // Used by both the text-chat finalize() path, selectSession's same-session
  // reselect, and the Voice Live merge -- one reconciliation path so none of
  // them can leave a stale local id behind or duplicate an unresolved turn's
  // persisted rows (see resolvePendingTurns).
  const applyReconciledMessages = useCallback(
    (
      fetchedSessionId: string,
      fresh: Message[],
      requestVersion?: number,
    ) => {
      if (fetchedSessionId !== sessionIdRef.current) return;
      if (
        requestVersion !== undefined &&
        latestHistoryRequestRef.current.get(fetchedSessionId) !== requestVersion
      ) {
        return;
      }
      const { removeIds, openRowIds } = resolvePendingTurns(
        pendingTurnsRef.current,
        fetchedSessionId,
        fresh,
        selectionGenerationRef.current,
      );
      const settled = openRowIds.size
        ? fresh.filter((message) => !openRowIds.has(message.id))
        : fresh;
      setMessages((previous) => reconcileMessages(previous, settled, removeIds));
    },
    [],
  );

  const requestHistory = useCallback(async (sessionId: string) => {
    const version = ++historySequenceRef.current;
    latestHistoryRequestRef.current.set(sessionId, version);
    const controller = new AbortController();
    historyControllersRef.current.add(controller);
    try {
      const messages = await api.listMessages(sessionId, controller.signal);
      if (
        controller.signal.aborted ||
        latestHistoryRequestRef.current.get(sessionId) !== version
      ) {
        return null;
      }
      return { messages, version };
    } finally {
      historyControllersRef.current.delete(controller);
    }
  }, []);

  const cancelHistoryRequests = useCallback(() => {
    for (const controller of historyControllersRef.current) controller.abort();
    historyControllersRef.current.clear();
  }, []);

  const loadAttachmentCapabilities = useCallback(async () => {
    const generation = ++capabilityGenerationRef.current;
    setAttachmentCapabilities(null);
    setAttachmentCapabilitiesError(null);
    try {
      const capabilities = await api.getAttachmentCapabilities();
      if (generation !== capabilityGenerationRef.current) return;
      setAttachmentCapabilities(capabilities);
    } catch (reason) {
      if (generation !== capabilityGenerationRef.current) return;
      setAttachmentCapabilitiesError((reason as Error).message);
    }
  }, []);

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
      void loadAttachmentCapabilities();
      // Agents are an optional enhancement (the @-menu); never block chat on them.
      try {
        setAgents(await api.listAgents());
      } catch {
        /* non-fatal: no @-mention menu */
      }
    })();
  }, [loadAttachmentCapabilities]);

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
      if (activeUploadCountRef.current > 0) {
        setError("Wait for active attachments to finish before changing conversations.");
        return;
      }
      const isSameSession = id === sessionIdRef.current;
      if (!isSameSession && voiceActiveRef.current) {
        voiceStopRef.current();
      }
      // Reselecting the session already on screen isn't a new viewing
      // episode, so it must not bump the generation: doing so would force-
      // prune this session's still-open pendingTurnsRef entries (see
      // resolvePendingTurns) and let a same-session click land right after
      // [DONE] replace a not-yet-reconciled fallback bubble with a raw
      // snapshot that may still show the backend's pre-completion
      // "streaming" placeholder.
      const generation = isSameSession
        ? selectionGenerationRef.current
        : ++selectionGenerationRef.current;
      cancelHistoryRequests();
      sessionIdRef.current = id;
      setActiveId(id);
      if (!isSameSession) setMessages([]);
      setError(null);
      try {
        const [snapshot, all, docs] = await Promise.all([
          requestHistory(id),
          api.listSessions(),
          api.listDocuments(id).catch(() => [] as DocumentSummary[]),
        ]);
        if (generation !== selectionGenerationRef.current) return;
        if (snapshot) {
          applyReconciledMessages(id, snapshot.messages, snapshot.version);
        }
        setDocuments(docs);
        // Library chips are a transient per-view confirmation; the docs persist
        // in the library and stay available to the agent regardless.
        setLibraryDocs([]);
        setSessions(all);
        const s = all.find((x) => x.id === id);
        if (s) {
          if (s.model) setSelectedModel(s.model);
          setSystemPrompt(s.systemPrompt ?? "");
          if (libraryEnabled) {
           const [owned, shared] = await Promise.all([
             api.listLibraryDocuments(),
             api.listSharedWithMe(),
           ]);
           if (
             generation !== selectionGenerationRef.current ||
             sessionIdRef.current !== id
           ) return;
           const byId = new Map(
             [...owned, ...shared].map((document) => [document.id, document]),
           );
           const library = [...byId.values()];
            if (s.libraryDocumentIds === null) {
              setLibraryDocs(library);
            } else {
              const selected = new Set(s.libraryDocumentIds);
              setLibraryDocs(library.filter((document) => selected.has(document.id)));
            }
          } else {
            setLibraryDocs([]);
          }
        }
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        setError((e as Error).message);
      }
    },
    [
      libraryEnabled,
      applyReconciledMessages,
      cancelHistoryRequests,
      requestHistory,
    ],
  );

  const newChat = useCallback(() => {
    if (streamingRef.current || voiceNavigationLockedRef.current) return;
    if (activeUploadCountRef.current > 0) {
      setError("Wait for active attachments to finish before starting a new conversation.");
      return;
    }
    if (voiceActiveRef.current) voiceStopRef.current();
    cancelHistoryRequests();
    pendingTurnsRef.current.clear();
    selectionGenerationRef.current += 1;
    sessionIdRef.current = null;
    setActiveId(null);
    setMessages([]);
    setDocuments([]);
    setLibraryDocs([]);
    setSelectedModel(pickDefaultModel(models));
    setSystemPrompt("");
    setDraftDefaults({
      agentName: null,
      toolOverrides: { added: [], removed: [] },
      libraryDocumentIds: [],
    });
    setStreamingText("");
    setStreamingStartedAt(null);
    setError(null);
  }, [models, cancelHistoryRequests]);

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
      if (activeUploadCountRef.current > 0) {
        setError(
          "Wait for active attachments to finish before deleting this conversation.",
        );
        return;
      }
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

  const renameSession = useCallback(async (id: string, title: string) => {
    const updated = await api.updateSession(id, { title });
    setSessions((current) =>
      current.map((session) => (session.id === updated.id ? updated : session)),
    );
  }, []);

  const changeModel = useCallback(
    async (modelId: string) => {
      const capturedSession = sessionIdRef.current;
      const generation = ++modelMutationGenerationRef.current;
      setSelectedModel(modelId);
      if (capturedSession) {
        try {
          await commitLatestSessionMutation({
            capturedSession,
            capturedGeneration: generation,
            currentSession: () => sessionIdRef.current,
            currentGeneration: () => modelMutationGenerationRef.current,
            operation: () => api.updateSession(capturedSession, { model: modelId }),
            commit: () => setInspectorVersion((value) => value + 1),
          });
        } catch {
          /* non-fatal */
        }
      }
    },
    [],
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
        agentName: draftDefaults.agentName,
        toolOverrides: draftDefaults.toolOverrides,
        libraryDocumentIds: draftDefaults.libraryDocumentIds,
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
  }, [draftDefaults, selectedModel, systemPrompt]);

  const runUpload = useCallback(
    async (uploadId: string) => {
      const target = uploadTargetsRef.current.get(uploadId);
      if (!target) return;
      setError(null);
      setUploading(true);
      setUploads((current) =>
        current.map((item) =>
          item.id === uploadId ? { ...item, status: "uploading", error: undefined } : item,
        ),
      );
      try {
        const capabilities = attachmentCapabilities;
        if (!capabilities) {
          throw new Error("Attachment capabilities are unavailable.");
        }
        const sid = target.sessionId ?? await ensureSession();
        target.sessionId = sid;
        setUploads((current) =>
          current.map((item) =>
            item.id === uploadId ? { ...item, sessionId: sid } : item,
          ),
        );
        const isCurrent = () =>
          isCurrentSessionGeneration(
            sid,
            target.selectionGeneration,
            sessionIdRef.current,
            selectionGenerationRef.current,
          );
        if (!isCurrent()) {
          uploadTargetsRef.current.delete(uploadId);
          setUploads((current) => current.filter((item) => item.id !== uploadId));
          return;
        }
        const result = await performBoundUpload({
          capabilities,
          sessionId: sid,
          file: target.file,
          isCurrent,
          uploadLibrary: api.uploadLibraryDocument,
          associateLibrary: api.associateLibraryDocument,
          uploadSession: api.uploadDocument,
          onAssociating: () =>
            setUploads((current) =>
              current.map((item) =>
                item.id === uploadId ? { ...item, status: "associating" } : item,
              ),
            ),
        });
        if (!result) {
          uploadTargetsRef.current.delete(uploadId);
          setUploads((current) => current.filter((item) => item.id !== uploadId));
          return;
        }
        if (result.path === "library") {
          setLibraryDocs((prev) => [
            ...prev.filter((item) => item.id !== result.document.id),
            result.document,
          ]);
          setSessions((current) =>
            current.map((item) =>
              item.id === result.session.id ? result.session : item,
            ),
          );
        } else {
          setDocuments((prev) => [
            ...prev.filter((item) => item.id !== result.document.id),
            result.document,
          ]);
        }
        uploadTargetsRef.current.delete(uploadId);
        setUploads((current) => current.filter((item) => item.id !== uploadId));
        setInspectorVersion((value) => value + 1);
      } catch (e) {
        const message = (e as Error).message;
        const targetSession = uploadTargetsRef.current.get(uploadId)?.sessionId;
        if (targetSession === sessionIdRef.current) {
          setError(message);
          setUploads((current) =>
            current.map((item) =>
              item.id === uploadId
                ? { ...item, status: "failed", error: message }
                : item,
            ),
          );
        }
      }
    },
    [attachmentCapabilities, ensureSession],
  );

  const queueUpload = useCallback(
    (uploadId: string): Promise<void> => {
      activeUploadCountRef.current += 1;
      setUploading(true);
      const next = uploadChainRef.current.then(async () => {
        try {
          await runUpload(uploadId);
        } finally {
          activeUploadCountRef.current = Math.max(
            0,
            activeUploadCountRef.current - 1,
          );
          setUploading(activeUploadCountRef.current > 0);
        }
      });
      uploadChainRef.current = next.catch(() => {});
      return next;
    },
    [runUpload],
  );

  const uploadDocument = useCallback(
    async (file: File) => {
      const id =
        typeof crypto.randomUUID === "function"
          ? crypto.randomUUID()
          : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      uploadTargetsRef.current.set(id, {
        file,
        sessionId: sessionIdRef.current,
        selectionGeneration: selectionGenerationRef.current,
      });
      setUploads((current) => [
        ...current,
        {
          id,
          filename: file.name,
          status: "queued",
          sessionId: sessionIdRef.current,
        },
      ]);
      await queueUpload(id);
    },
    [queueUpload],
  );

  const retryUpload = useCallback(
    (uploadId: string) => {
      const target = uploadTargetsRef.current.get(uploadId);
      if (!target || target.sessionId !== sessionIdRef.current) return;
      target.selectionGeneration = selectionGenerationRef.current;
      void queueUpload(uploadId);
    },
    [queueUpload],
  );

  const dismissUpload = useCallback((uploadId: string) => {
    uploadTargetsRef.current.delete(uploadId);
    setUploads((current) => current.filter((item) => item.id !== uploadId));
  }, []);

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
        applyReconciledMessages(sessionId, created);
      }
      void Promise.allSettled([
        requestHistory(sessionId).then((snapshot) => {
          if (snapshot) {
            applyReconciledMessages(
              sessionId,
              snapshot.messages,
              snapshot.version,
            );
          }
        }),
        refreshSessions(),
      ]);
      setInspectorVersion((value) => value + 1);
    },
    [refreshSessions, applyReconciledMessages, requestHistory],
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
          provider.selectionMode === "managed_model_catalog" ||
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
    voicePrefsResolved.provider === "speech_voice_live"
      ? voicePrefsResolved.speechModel
      : voicePrefsResolved.model;
  const effectiveVoiceRegion =
    voicePrefsResolved.provider === "azure_openai"
      ? providerModelRegion(realtimeModelList, effectiveVoiceModel)
      : null;
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
            speechModel: voicePrefsResolved.speechModel,
            onSpeechModelChange: (nextModel: string) =>
              updateVoicePrefs({ ...voicePrefsResolved, speechModel: nextModel }),
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
                      speechModel: DEFAULT_SPEECH_MODEL_ID,
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
      const capturedSession = activeId;
      const generation = selectionGenerationRef.current;
      const prev = documents;
      // Optimistic removal; restore on failure.
      setDocuments((cur) => cur.filter((d) => d.id !== documentId));
      try {
        await api.deleteDocument(capturedSession, documentId);
      } catch (e) {
        if (
          sessionIdRef.current !== capturedSession ||
          selectionGenerationRef.current !== generation
        ) return;
        setDocuments(prev);
        setError((e as Error).message);
      }
    },
    [activeId, documents],
  );

  // Removing a library chip only detaches it from this conversation. The user's
  // durable library artifact remains intact.
  const removeLibraryDocument = useCallback(
    async (documentId: string) => {
      if (!activeId) return;
      const capturedSession = activeId;
      const generation = selectionGenerationRef.current;
      const prev = libraryDocs;
      setLibraryDocs((cur) => cur.filter((d) => d.id !== documentId));
      try {
        const updated = await api.disassociateLibraryDocument(
          capturedSession,
          documentId,
        );
        if (
          sessionIdRef.current !== capturedSession ||
          selectionGenerationRef.current !== generation
        ) return;
        setSessions((current) =>
          current.map((item) => (item.id === updated.id ? updated : item)),
        );
      } catch (e) {
        if (
          sessionIdRef.current !== capturedSession ||
          selectionGenerationRef.current !== generation
        ) return;
        setLibraryDocs(prev);
        setError((e as Error).message);
      }
    },
    [activeId, libraryDocs],
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
        const changed = libraryDocs.some((document) => {
          const next = byId.get(document.id);
          return next && next.status !== document.status;
        });
        setLibraryDocs((prev) =>
          prev.map((d) => (tracked.has(d.id) ? byId.get(d.id) ?? d : d)),
        );
        if (changed) setInspectorVersion((value) => value + 1);
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

      // Captured once so `finalize` can tell -- after its awaited network
      // calls settle -- whether this is still the turn the user is watching:
      // same conversation (finalize clears the streaming lock before its own
      // awaits finish) and no newer send has since started in it.
      const turnSessionId = sessionId;
      const turnGeneration = selectionGenerationRef.current;
      const turnId = ++turnCounterRef.current;
      const clientTurnId = newClientTurnId();
      // Mirrors streamingText/liveSteps outside React state, since `send` is
      // memoized and won't see fresh values of that state. Lets `finalize`
      // reconstruct the reply if the reconciliation fetch fails or is stale.
      let bufferedContent = "";
      let bufferedSteps: ActivityStep[] = [];
      // The browser id below is authoritative. Server row ids, when echoed,
      // support a rolling deployment against the preceding API version.
      const userCreatedAt = new Date();
      const optimisticUser: Message = {
        id: `tmp-${clientTurnId}`,
        sessionId,
        userId: "me",
        role: "user",
        content,
        status: "complete",
        model: selectedModel,
        agent: null,
        source: "chat",
        clientTurnId,
        createdAt: userCreatedAt.toISOString(),
      };
      setStreamingStartedAt(
        new Date(userCreatedAt.getTime() + 1).toISOString(),
      );
      setMessages((prev) => [...prev, optimisticUser]);
      const pendingTurn: PendingTurn = {
        sessionId: turnSessionId,
        generation: turnGeneration,
        clientTurnId,
        source: "chat",
        knownMessageIds: new Set(
          messagesRef.current.map((message) => message.id),
        ),
        optimisticUserId: optimisticUser.id,
        placeholderId: null,
      };
      pendingTurnsRef.current.set(clientTurnId, pendingTurn);

      const isCommand = content.trimStart().startsWith("/");
      // True only while this is still the most recent turn for the
      // conversation on screen, so a late-resolving fetch from an
      // overtaken turn can't clobber whatever is now live.
      const isCurrentTurn = () =>
        turnSessionId === sessionIdRef.current &&
        turnGeneration === selectionGenerationRef.current &&
        turnId === turnCounterRef.current;
      let finalized = false;
      let rejected = false;
      const releaseLiveTurn = () => {
        streamingRef.current = false;
        setStreaming(false);
        abortRef.current = null;
        if (isCurrentTurn()) {
          setStreamingText("");
          setStreamingStartedAt(null);
          setLiveSteps([]);
        }
      };
      const rejectOptimisticTurn = () => {
        rejected = true;
        pendingTurnsRef.current.delete(clientTurnId);
        setMessages((previous) =>
          previous.filter((message) => message.id !== optimisticUser.id),
        );
        releaseLiveTurn();
      };
      const finalize = async (status: "complete" | "cancelled" | "error") => {
        if (finalized || rejected) return;
        finalized = true;
        const hasContent = bufferedContent.trim().length > 0 || bufferedSteps.length > 0;
        const placeholderId = hasContent ? `local-${clientTurnId}` : null;
        // Track this turn so its optimistic user bubble (and placeholder, if
        // any) is superseded once a fetch -- this turn's own, or any later
        // turn's in the same session -- proves the persisted reply. See
        // applyReconciledMessages/resolvePendingTurns above.
        pendingTurn.placeholderId = placeholderId;
        if (placeholderId) {
          // Materialized *before* the streaming lock is released, so the
          // reply is never absent from both streamingText and `messages` --
          // not even for the instant before a same-session resend. Superseded
          // later by applyReconciledMessages once the persisted copy lands.
          setMessages((previous) => [
            ...previous,
            {
              id: placeholderId,
              sessionId: turnSessionId,
              userId: "me",
              role: "assistant",
              content: bufferedContent,
              status,
              model: selectedModel,
              agent: null,
              source: "chat",
              clientTurnId,
              createdAt: new Date().toISOString(),
              steps: finalizedSteps(bufferedSteps),
            },
          ]);
        }
        // Only one turn can ever hold this lock (send() rejects a second
        // call while it's held), so releasing it here always belongs to the
        // turn currently running finalize -- unconditional, unlike the
        // per-turn checks below that guard the *shared* live-preview state.
        releaseLiveTurn();

        const controller = new AbortController();
        const requestVersion = ++historySequenceRef.current;
        latestHistoryRequestRef.current.set(turnSessionId, requestVersion);
        historyControllersRef.current.add(controller);
        const isCancelled = () =>
          !mountedRef.current ||
          controller.signal.aborted ||
          turnSessionId !== sessionIdRef.current ||
          turnGeneration !== selectionGenerationRef.current ||
          pendingTurnsRef.current.get(clientTurnId) !== pendingTurn;
        let fresh: Message[] | null = null;
        try {
          fresh = await fetchReconciledMessages(
            turnSessionId,
            pendingTurn,
            isCancelled,
            controller.signal,
          );
        } finally {
          historyControllersRef.current.delete(controller);
        }
        if (fresh) {
          applyReconciledMessages(
            turnSessionId,
            fresh,
            requestVersion,
          );
          if (isCurrentTurn()) setInspectorVersion((value) => value + 1);
        }
        // A slash command can change the session's model or system prompt on the
        // server; re-sync the controls so the change holds for the next turn.
        // Skipped once unmounted -- fences this last request/result too, per
        // the same isCancelled contract fetchReconciledMessages follows.
        if (mountedRef.current) {
          if (isCommand) {
            try {
              const all = await api.listSessions();
              setSessions(all);
              const s = all.find((x) => x.id === turnSessionId);
              if (s && isCurrentTurn()) {
                if (s.model) setSelectedModel(s.model);
                setSystemPrompt(s.systemPrompt ?? "");
              }
            } catch {
              /* non-fatal */
            }
          } else {
            void refreshSessions();
          }
        }
      };

      abortRef.current = api.streamChat(
        { sessionId, content, model: selectedModel, params, clientTurnId },
        {
          onDelta: (t) => {
            bufferedContent += t;
            setStreamingText((prev) => prev + t);
          },
          onStep: (step) => {
            bufferedSteps = [...bufferedSteps, step];
            setLiveSteps((prev) => [...prev, step]);
          },
          onMessageIds: (ids) => {
            if (ids.clientTurnId && ids.clientTurnId !== clientTurnId) return;
            pendingTurn.assistantMessageId = ids.assistantMessageId;
            pendingTurn.userMessageId = ids.userMessageId;
          },
          onDone: () => void finalize("complete"),
          onError: (msg) => {
            setError(msg);
            if (rejected) return;
            void finalize("error");
          },
          onRejected: () => rejectOptimisticTurn(),
          // Stop button: reconcile with the server's cancelled message.
          onAbort: () => void finalize("cancelled"),
        },
      );
    },
    [activeId, selectedModel, params, refreshSessions, ensureSession, applyReconciledMessages],
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
  const mobileSidebar = useMediaQuery(MOBILE_SIDEBAR_QUERY);
  const drawerInspector = useMediaQuery(MOBILE_INSPECTOR_QUERY);
  const mobileSidebarOpen = mobileSidebar && mobileDrawer === "sidebar";
  const mobileInspectorOpen = drawerInspector && mobileDrawer === "inspector";
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const sidebarMedia = window.matchMedia(MOBILE_SIDEBAR_QUERY);
    const inspectorMedia = window.matchMedia(MOBILE_INSPECTOR_QUERY);
    const closeUnavailable = () => {
      setMobileDrawer((current) =>
        closeUnavailableMobileDrawer(
          current,
          sidebarMedia.matches,
          inspectorMedia.matches,
        ),
      );
    };
    sidebarMedia.addEventListener("change", closeUnavailable);
    inspectorMedia.addEventListener("change", closeUnavailable);
    return () => {
      sidebarMedia.removeEventListener("change", closeUnavailable);
      inspectorMedia.removeEventListener("change", closeUnavailable);
    };
  }, []);
  const leftIsCollapsed = mobileSidebar ? !mobileSidebarOpen : leftCollapsed;
  const rightIsCollapsed = drawerInspector ? !mobileInspectorOpen : rightCollapsed;
  const toggleLeftPanel = useCallback(() => {
    if (mobileSidebar) {
      setMobileDrawer((current) => nextMobileDrawer(current, "sidebar"));
    } else {
      toggleLeftCollapsed();
    }
  }, [mobileSidebar, toggleLeftCollapsed]);
  const toggleRightPanel = useCallback(() => {
    if (drawerInspector) {
      setMobileDrawer((current) => nextMobileDrawer(current, "inspector"));
    } else {
      toggleRightCollapsed();
    }
  }, [drawerInspector, toggleRightCollapsed]);

  return (
    <div style={{ display: "flex", height: "100vh", overflow: "hidden" }}>
      {!leftIsCollapsed && mobileSidebar ? (
        <button
          type="button"
          className="drawer-backdrop"
          aria-label="Close conversation sidebar"
          onClick={toggleLeftPanel}
        />
      ) : null}
      <div
        className="sidebar-slot"
        inert={mobileInspectorOpen ? true : undefined}
        aria-hidden={mobileInspectorOpen ? true : undefined}
      >
        {leftIsCollapsed ? (
          <div
          className="sidebar-collapsed-trigger"
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
            ref={(element) => {
              sidebarOpenerRef.current = element;
              if (element) sidebarReturnFocusRef.current = element;
            }}
            onClick={() => {
              sidebarReturnFocusRef.current = sidebarOpenerRef.current;
              toggleLeftPanel();
            }}
            aria-label={mobileSidebar ? "Open conversation sidebar" : "Expand sidebar"}
            title={mobileSidebar ? "Open conversations" : "Expand sidebar"}
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
          onRename={renameSession}
          onOpenSettings={() => setSettingsOpen(true)}
          onOpenStudio={() => setStudioOpen(true)}
          onOpenLibrary={libraryEnabled ? () => setLibraryOpen(true) : undefined}
          onCollapse={toggleLeftPanel}
          openerRef={sidebarReturnFocusRef}
          disabled={streaming || voiceExitLocked}
          />
        )}
      </div>

      <main
        inert={mobileSidebarOpen || mobileInspectorOpen ? true : undefined}
        aria-hidden={mobileSidebarOpen || mobileInspectorOpen ? true : undefined}
        style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0 }}
      >
        <header
          className="chat-header"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 16,
            padding: "12px max(16px, 6%)",
            borderBottom: "1px solid var(--border)",
            background: "var(--bg-elevated)",
          }}
        >
          {activeId ? (
            <EditableSessionTitle
              title={
                sessions.find((session) => session.id === activeId)?.title ??
                "Untitled"
              }
              onSave={(title) => renameSession(activeId, title)}
              disabled={streaming || voiceExitLocked}
            />
          ) : (
            <strong>New conversation</strong>
          )}
          <div
            role="status"
            aria-live="polite"
            aria-atomic="true"
            style={{ marginLeft: "auto", fontSize: "0.8em", color: "var(--fg-muted)" }}
          >
            {streaming ? "Generating…" : "Ready"}
          </div>
        </header>

        {error && (
          <div
            role="alert"
            style={{
              padding: "10px max(16px, 6%)",
              background: "var(--danger)",
              color: "var(--danger-fg)",
              display: "flex",
              justifyContent: "space-between",
              gap: 12,
            }}
          >
            <span>{error}</span>
            <button
              onClick={() => setError(null)}
              aria-label="Dismiss error"
              style={{ border: "none", background: "transparent", color: "var(--danger-fg)" }}
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
          capabilities={attachmentCapabilities}
          capabilitiesError={attachmentCapabilitiesError}
          uploads={uploads.filter(
            (upload) => upload.sessionId === activeId,
          )}
          onSend={send}
          onStop={stop}
          onUpload={uploadDocument}
          onRetryUpload={retryUpload}
          onDismissUpload={dismissUpload}
          onRetryCapabilities={() => void loadAttachmentCapabilities()}
          onRemoveDocument={removeDocument}
          onRemoveLibraryDocument={removeLibraryDocument}
          onError={setError}
          voiceLive={
            voiceLiveEnabled
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
                }
              : undefined
          }
        />
      </main>

      {!rightIsCollapsed && drawerInspector ? (
        <button
          type="button"
          className="drawer-backdrop inspector-backdrop"
          aria-label="Close conversation inspector"
          onClick={toggleRightPanel}
        />
      ) : null}
      <div
        className="inspector-slot"
        inert={mobileSidebarOpen ? true : undefined}
        aria-hidden={mobileSidebarOpen ? true : undefined}
      >
        <ConversationInspector
          key={activeId ?? "new-conversation"}
          sessionId={activeId}
          refreshKey={inspectorVersion}
          models={models}
          agents={agents}
          selectedModel={selectedModel}
          onModelChange={changeModel}
          params={params}
          onParamsChange={setParams}
          systemPrompt={systemPrompt}
          onSystemPromptChange={setSystemPrompt}
          draftDefaults={draftDefaults}
          onDraftDefaultsChange={setDraftDefaults}
          onSessionUpdated={(updated) => {
            if (updated.id !== sessionIdRef.current) return;
            setSessions((current) =>
              current.map((session) => (session.id === updated.id ? updated : session)),
            );
            setSystemPrompt(updated.systemPrompt ?? "");
            if (updated.model) setSelectedModel(updated.model);
          }}
          onOpenLibrary={libraryEnabled ? () => setLibraryOpen(true) : undefined}
          attachmentCapabilities={attachmentCapabilities}
          voiceSettings={voiceSettingsProps}
          voiceLocked={voiceExitLocked}
          collapsed={rightIsCollapsed}
          onToggle={toggleRightPanel}
        />
      </div>

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
    </div>
  );
}
