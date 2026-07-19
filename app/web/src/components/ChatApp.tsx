"use client";

// Main chat application shell. Orchestrates session lifecycle, message streaming,
// model selection and chat parameters, and the voice / library / custom-tools /
// media surfaces. Feature panels are hidden here when their env flag is off, but
// enforcement is server-side — the API is the authority (see app/api).

import {
  useCallback,
  useEffect,
  useId,
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

// Bounds how long ANY caller of ensureSession -- not just whichever one
// started the request -- will wait on a single pending session creation
// before giving up and evicting the shared cache entry. Without this, a
// hung createSession() network call (dropped connection, backend stall)
// left the entry cached forever: a later Retry (after voice's own
// PERSIST_TIMEOUT_MS fired) or a plain text send would join the exact same
// doomed promise and hang right along with it, with no way out short of a
// page reload. 20s matches voice's PERSIST_TIMEOUT_MS (InlineVoiceLive.tsx)
// so the two settle around the same time for voice's own call; send/upload
// have no timeout of their own today and rely entirely on this bound.
const SESSION_CREATION_TIMEOUT_MS = 20_000;

// Rejects once `deadline` (a Date.now()-style absolute timestamp) has
// passed; never resolves. Racing this against a pending creation bounds how
// long a caller waits without affecting the underlying request itself --
// that keeps running in the background exactly as it does today unless
// abandonPendingSessionCreation() also aborts it. Deadline-based (rather
// than a fixed delay from "now") so a caller that joins an already-pending
// creation partway through still only waits out the REMAINDER of the
// original budget, not a fresh full timeout.
//
// Its own setTimeout is never cleared, so on the far more common path --
// entry.promise wins the race because creation succeeds well within the
// budget -- this promise is left pending and rejects on its own, unread,
// once the original deadline arrives. A bare `.catch(() => {})` here (on a
// throwaway derived promise, not the one actually returned/raced) keeps
// that inevitable, ignorable rejection from ever surfacing as an "Uncaught
// (in promise)" console warning; Promise.race still independently attaches
// its own handler to the very same returned promise, so the real caller
// racing against it is completely unaffected and still observes the
// rejection when this side of the race genuinely wins.
function sessionCreationDeadline(deadline: number): Promise<never> {
  const timeout = new Promise<never>((_, reject) => {
    const delay = Math.max(0, deadline - Date.now());
    setTimeout(() => {
      reject(
        new Error(
          "Creating the conversation is taking too long. Please try again.",
        ),
      );
    }, delay);
  });
  timeout.catch(() => {});
  return timeout;
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
  // The CONCRETE session id a real, already-resolved send() is actively
  // streaming into -- set only once send() has an actual id in hand (never
  // merely because streamingRef.current is true), cleared in finalize().
  // streamingRef.current alone can't gate ensureSession()'s supersession
  // check below: it's set synchronously at the very top of send(), before
  // send() even knows its own eventual session id, so it's also true while
  // send()'s OWN ensureSession() call is still deciding whether to activate
  // its first-ever session -- a false "someone is already consuming the
  // active session" signal about to be produced by that very call. Keying
  // on the concrete id instead means the flag can only ever match an
  // ALREADY-established sessionIdRef.current that a DIFFERENT, prior
  // send() actually finished activating and started consuming.
  const streamingSessionIdRef = useRef<string | null>(null);
  // Holds an in-flight lazy session-creation promise so a rapid send + upload
  // (or two uploads) share a single session instead of racing to create two.
  // Keyed by an "intent" fingerprint (see ensureSession) of the exact
  // selection generation plus the model/systemPrompt/agent/tools/docs the
  // in-flight request was built from, so a caller whose own current settings
  // have since diverged (e.g. after Stop waiting + New chat resets them)
  // never silently reuses a stale creation made under different settings --
  // it fires its own instead. The generation component additionally ensures
  // a creation started in one generation is never reused by a later one even
  // if New chat happens to reset settings back to identical defaults: New
  // chat never clears this ref, so without the generation component two
  // otherwise-unrelated generations that share the same default settings
  // could otherwise collide on the same cache entry.
  //
  // startedAt + controller back the SESSION_CREATION_TIMEOUT_MS bound in
  // ensureSession: startedAt lets every caller (not just the one that
  // installed the entry) compute the same absolute deadline, and controller
  // lets a hung request actually be cancelled -- either once that bound
  // trips, or immediately via abandonPendingSessionCreation() (wired to
  // voice's "Stop waiting") -- instead of merely being ignored while it
  // keeps running forever in the background. mismatchClaimed lets at most
  // ONE caller sharing this entry (same generation + settings, hence the
  // same key) ever attempt a cross-key mismatch supersession against
  // activeIntentKeyRef (see ensureSession) -- two such callers normally
  // settle back-to-back in the same microtask flush, but are not guaranteed
  // to, and without this a second one could re-observe "the active key
  // differs from mine" (because a third, genuinely different intent
  // superseded in the gap) and incorrectly flip activation back to this
  // entry's own, already-adjudicated key. waiterCount tracks how many
  // ensureSession() calls are CURRENTLY awaiting this exact entry's outcome
  // (incremented right before, decremented right after, regardless of
  // whether the entry was just created or an existing one was joined) so
  // abandonPendingSessionCreation() can tell "only the abandoning attempt's
  // own call is left" (safe to abort) apart from "someone else -- a
  // concurrent send()/upload/other voice attempt sharing this exact entry --
  // is also relying on it" (must not abort out from under them). A caller's
  // OWN outer timeout firing (e.g. voice's PERSIST_TIMEOUT_MS) never itself
  // decrements this: only this entry's Promise.race actually settling does,
  // so the count stays accurate regardless of who's still listening to it.
  const creatingRef = useRef<{
    intentKey: string;
    promise: Promise<string>;
    startedAt: number;
    controller: AbortController;
    mismatchClaimed: boolean;
    waiterCount: number;
  } | null>(null);
  // Synchronous mirror of activeId so ensureSession sees a just-created session
  // immediately (before the setActiveId state flush), preventing a double create
  // when an upload is quickly followed by a send.
  const sessionIdRef = useRef<string | null>(null);
  // The intentKey (see ensureSession) that sessionIdRef.current was last
  // activated under. Null whenever sessionIdRef.current is null. Lets
  // ensureSession detect a genuine settings mismatch -- a later caller
  // racing to create its OWN session, under different model/prompt/agent/
  // tools/docs, that resolves after an earlier (differently-configured)
  // candidate already activated -- and let the later, mismatched caller
  // supersede rather than silently inherit config it never asked for. Only
  // ever consulted while sessionIdRef.current is non-null (activation is a
  // one-shot event per generation prior to that), so this never second-
  // guesses an already-established, in-use conversation -- only the narrow
  // window where two brand-new candidate sessions are still racing.
  const activeIntentKeyRef = useRef<string | null>(null);
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
      if (streamingRef.current) return;
      if (voiceNavigationLockedRef.current) {
        setError(
          "Finish saving the voice transcript before switching conversations. Use \u201cRetry saving\u201d or \u201cStop waiting\u201d in the voice status bar to continue.",
        );
        return;
      }
      if (activeUploadCountRef.current > 0) {
        setError("Wait for active attachments to finish before changing conversations.");
        return;
      }
      if (id !== sessionIdRef.current && voiceActiveRef.current) {
        voiceStopRef.current();
      }
      const generation = ++selectionGenerationRef.current;
      sessionIdRef.current = id;
      // A brand-new selection generation can never legitimately be compared
      // against an intentKey computed under the previous one (its captured
      // generation number is now stale), so there is nothing left for a
      // future ensureSession race to correctly supersede against -- clear it
      // rather than let a coincidental key match from an unrelated future
      // creation silently skip activation.
      activeIntentKeyRef.current = null;
      setActiveId(id);
      setError(null);
      try {
        const [msgs, all, docs] = await Promise.all([
          api.listMessages(id),
          api.listSessions(),
          api.listDocuments(id).catch(() => [] as DocumentSummary[]),
        ]);
        if (generation !== selectionGenerationRef.current) return;
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
        setError((e as Error).message);
      }
    },
    [libraryEnabled],
  );

  const newChat = useCallback(() => {
    if (streamingRef.current) return;
    if (voiceNavigationLockedRef.current) {
      setError(
        "Finish saving the voice transcript before starting a new conversation. Use \u201cRetry saving\u201d or \u201cStop waiting\u201d in the voice status bar to continue.",
      );
      return;
    }
    if (activeUploadCountRef.current > 0) {
      setError("Wait for active attachments to finish before starting a new conversation.");
      return;
    }
    if (voiceActiveRef.current) voiceStopRef.current();
    selectionGenerationRef.current += 1;
    sessionIdRef.current = null;
    // See the matching comment in selectSession: a new generation invalidates
    // any intentKey computed under the old one.
    activeIntentKeyRef.current = null;
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
  }, [models]);

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
      if (voiceNavigationLockedRef.current) {
        setError(
          "Finish saving the voice transcript before deleting this conversation. Use \u201cRetry saving\u201d or \u201cStop waiting\u201d in the voice status bar to continue.",
        );
        return;
      }
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

  // Shared by ensureSession and abandonPendingSessionCreation so both always
  // compute the identical key from the identical formula. Memoized (rather
  // than a plain function) so its own identity only changes when one of the
  // settings it captures actually does -- both callers below list it as a
  // dependency, and without memoizing it here that would force a fresh
  // ensureSession/abandonPendingSessionCreation every single render.
  const computeSessionIntentKey = useCallback(
    (capturedGeneration: number) =>
      `${capturedGeneration}:${JSON.stringify({
        model: selectedModel,
        systemPrompt: systemPrompt || null,
        agentName: draftDefaults.agentName,
        toolOverrides: draftDefaults.toolOverrides,
        libraryDocumentIds: draftDefaults.libraryDocumentIds,
      })}`,
    [selectedModel, systemPrompt, draftDefaults],
  );

  // Lazily create (or reuse) the active session. Shared by send + document
  // upload so they never race to create two sessions; concurrent callers await
  // the same in-flight creation promise.
  //
  // isStillWanted is an optional caller-supplied predicate re-checked right
  // before the created session is made active, alongside the selection
  // generation captured at call time. Both a real navigation (selectSession/
  // newChat, which bump selectionGenerationRef) *and* a caller-side
  // abandonment that isn't a navigation at all (e.g. Voice Live discarding a
  // transcript while staying on the same blank "new chat" view, which never
  // touches selectionGenerationRef) must be able to stop this call from
  // force-navigating the UI to a session nobody wants anymore once the
  // createSession() network call finally resolves. The session itself is
  // still real and already persisted on the backend either way, so it always
  // joins the sidebar list -- only becoming the *active/navigated-to* session
  // is gated.
  //
  // Only the network call + sidebar append is shared via creatingRef: the
  // activation gate below runs independently for EVERY caller, each with its
  // own capturedGeneration/isStillWanted. A single shared gate (evaluated
  // once, using only the first caller's answer) would let a later, still-
  // valid caller's "yes" be silently discarded just because it happened to
  // share the same in-flight creation as an earlier caller's "no".
  //
  // Sharing is further gated by an "intent" fingerprint of the selection
  // generation plus the exact payload this call would send to
  // api.createSession. Without the settings component, a caller whose
  // settings changed *after* an earlier creation started (e.g. Voice Live
  // begins creating a session, the user hits Stop waiting + New chat which
  // resets model/systemPrompt/agent/tools/docs, then sends a fresh message)
  // would silently reuse that stale in-flight promise -- binding the new,
  // differently-configured send to a session actually created with the OLD
  // settings, since both callers would otherwise await the identical network
  // request. Without the generation component, New chat (which never clears
  // creatingRef) could let a later generation reuse a still-in-flight
  // creation from an earlier, already-abandoned generation whenever New
  // chat happened to reset settings back to identical defaults, since the
  // settings-only fingerprint would then coincidentally match. A caller only
  // reuses the cached entry when its own current generation AND settings
  // would produce an identical request; otherwise it fires its own and
  // installs its own entry, without clobbering a different still-in-flight
  // generation that may belong to another still-valid caller.
  //
  // Because differently-keyed intents install *separate* creatingRef
  // entries, two genuinely different callers (e.g. Voice Live creating
  // under one set of settings while a text send/upload creates under
  // another, both concurrently) can each have their own in-flight
  // createSession() call outstanding at once. Activation below lets
  // whichever intent resolves LAST supersede an already-active DIFFERENT
  // (mismatched) one -- see activeIntentKeyRef -- rather than always
  // favoring whoever merely resolved first, since silently converging every
  // caller onto the first winner would mean a later, differently-configured
  // caller (e.g. a plain text send after Stop waiting + New chat changed
  // the settings) sends into a session actually built from stale config. A
  // same-key resolution never displaces anything, so this only ever
  // arbitrates between brand-new, still-racing candidates on a blank
  // starting point -- never against an already-established, in-use
  // conversation (activation is impossible once sessionIdRef.current is set
  // unless the intent key genuinely differs) -- AND never against a session
  // that already has a real, in-flight consumer (see currentSessionInUse
  // below): once a real send()/upload is actively using a session, no
  // later-resolving mismatched intent may rip the UI over to a different
  // one out from under it, since send()/upload capture their own session id
  // once and would keep silently updating the visible transcript for the
  // no-longer-"active" session while the header/sidebar showed another. The
  // return value below guarantees every OTHER caller -- one whose own
  // intent didn't end up activating -- still ends up with the session that
  // is actually current by the time its own call resolves, never its own
  // now-orphaned creation.
  //
  // A pending creation's wait is bounded to SESSION_CREATION_TIMEOUT_MS from
  // when it STARTED (not from when each caller joined), for every caller
  // sharing the entry -- not just whichever one installed it -- so a hung
  // network call can never wedge Retry, a plain text send, or navigation
  // forever. Once that bound trips (or abandonPendingSessionCreation() is
  // called explicitly -- see InlineVoiceLive's "Stop waiting" wiring), the
  // entry is evicted identity-safely and its request is aborted so the next
  // caller gets a genuinely fresh attempt instead of racing the same doomed
  // promise again.
  const ensureSession = useCallback(
    async (isStillWanted?: () => boolean): Promise<string> => {
      if (sessionIdRef.current) return sessionIdRef.current;
      const capturedGeneration = selectionGenerationRef.current;
      const intentKey = computeSessionIntentKey(capturedGeneration);
      let entry = creatingRef.current;
      if (!entry || entry.intentKey !== intentKey) {
        // The identity-safe cleanup below is registered via `.finally()`
        // (a separately-invoked callback, run once the request settles)
        // rather than a `try/finally` inside the async IIFE itself: the
        // latter's `finally` block is part of the SAME function that is
        // immediately invoked to produce `creation`, which trips
        // TypeScript's definite-assignment analysis ("used before being
        // assigned") even though it only actually runs well after this
        // statement has returned. `.finally()`'s callback is merely
        // registered here, not invoked synchronously, so it can safely
        // close over `creation` once assigned.
        const controller = new AbortController();
        const creation: Promise<string> = (async () => {
          const created = await api.createSession(
            {
              model: selectedModel,
              systemPrompt: systemPrompt || null,
              agentName: draftDefaults.agentName,
              toolOverrides: draftDefaults.toolOverrides,
              libraryDocumentIds: draftDefaults.libraryDocumentIds,
            },
            controller.signal,
          );
          setSessions((prev) => [created, ...prev]);
          return created.id;
        })().finally(() => {
          // Identity-safe: only clear the slot if it still points at THIS
          // creation. A differently-scoped caller may already have
          // installed its own newer entry while this one was in flight.
          if (creatingRef.current?.promise === creation) {
            creatingRef.current = null;
          }
        });
        entry = {
          intentKey,
          promise: creation,
          startedAt: Date.now(),
          controller,
          mismatchClaimed: false,
          waiterCount: 0,
        };
        creatingRef.current = entry;
      }
      // Counts this call among the entry's current waiters for the whole
      // span it's awaiting the race below, regardless of whether it just
      // created the entry or joined an existing one -- see the waiterCount
      // comment on creatingRef.
      entry.waiterCount += 1;
      let id: string;
      try {
        id = await Promise.race([
          entry.promise,
          sessionCreationDeadline(
            entry.startedAt + SESSION_CREATION_TIMEOUT_MS,
          ),
        ]);
      } catch (waitError) {
        // Bounded out. Evict identity-safely (a differently-keyed, newer
        // entry may already have replaced this one) and abort the
        // underlying request so it stops consuming a connection and any
        // OTHER caller still awaiting this same entry.promise also gives up
        // now instead of hanging until its own (later-computed, but
        // identical) deadline -- rather than leave the next caller to race
        // the same doomed promise again. The abort's eventual rejection
        // inside the creation IIFE above is swallowed by the existing
        // `.finally()`, so this never produces an unhandled rejection.
        if (creatingRef.current === entry) {
          creatingRef.current = null;
        }
        entry.controller.abort();
        throw waitError;
      } finally {
        entry.waiterCount -= 1;
      }
      const stillCurrentSelection =
        selectionGenerationRef.current === capturedGeneration;
      const stillWanted = isStillWanted ? isStillWanted() : true;
      const noSessionActiveYet = !sessionIdRef.current;
      // A session that already has a real, in-flight consumer (an active
      // stream or upload) must never be superseded out from under it:
      // send()/runUpload() capture their own session id ONCE and never
      // re-read activeId/sessionIdRef afterward, so reactivating a
      // DIFFERENT session mid-stream wouldn't confuse THEIR own closures
      // (they'd keep correctly targeting their original session), but it
      // would desynchronize the visible transcript -- still being updated
      // for the original session -- from whatever the header/sidebar now
      // report as active, with nothing ever reloading messages for the new
      // one. This only ever gates the mismatch-supersession branch below,
      // never noSessionActiveYet: a caller activating the very first
      // session for a blank chat may itself have already set
      // streamingRef.current before its own ensureSession() call resolves,
      // and that first-ever activation must still succeed.
      //
      // Deliberately keyed on the CONCRETE session id (streamingSessionIdRef
      // / each upload target's own sessionId), not the caller-agnostic
      // streamingRef.current/activeUploadCountRef.current booleans: those
      // flip on before the setting caller itself has a session id (send()
      // marks streamingRef.current synchronously before calling
      // ensureSession() for its OWN first session), so a same-call read
      // would misreport "the active session is in use" for a session that
      // doesn't even exist yet. Comparing concrete ids means this can only
      // ever match a DIFFERENT, already-resolved call's real target --
      // never this invocation's own not-yet-known id.
      const currentSessionInUse =
        sessionIdRef.current !== null &&
        (streamingSessionIdRef.current === sessionIdRef.current ||
          Array.from(uploadTargetsRef.current.values()).some(
            (target) => target.sessionId === sessionIdRef.current,
          ));
      // At most one caller sharing this entry ever gets to attempt a
      // cross-key mismatch supersession -- see the mismatchClaimed comment
      // on creatingRef. Claimed unconditionally (regardless of the outcome
      // below), since the invariant is "this key's one comparison has
      // happened", not "this key's comparison succeeded".
      const canClaimMismatch = !entry.mismatchClaimed;
      entry.mismatchClaimed = true;
      const settingsMismatch =
        canClaimMismatch &&
        !noSessionActiveYet &&
        !currentSessionInUse &&
        activeIntentKeyRef.current !== intentKey;
      if (
        stillCurrentSelection &&
        stillWanted &&
        (noSessionActiveYet || settingsMismatch)
      ) {
        sessionIdRef.current = id;
        activeIntentKeyRef.current = intentKey;
        setActiveId(id);
        return id;
      }
      // A *different*, concurrently-running intent -- its own creatingRef
      // entry, since it had different settings and/or a different selection
      // generation -- may have already become the active session while this
      // caller's own creation was in flight (regardless of resolution
      // order: whichever intent's network call settles LAST, if its
      // settings genuinely differ from what's currently active, wins
      // activation above; a same-key resolution never displaces anything).
      // Unconditionally returning THIS creation's own id here would hand
      // the caller a real, backend-persisted, but never-activated/never-
      // shown session -- a send/upload/voice-turn append would then
      // silently write into a conversation nobody can see. Falling back to
      // whatever id IS current instead means every caller converges on the
      // one conversation actually on screen: send/upload (which never
      // supply isStillWanted, and can't have their generation change mid-
      // flight since navigation is blocked while they're pending) always
      // get either their own session or this safe fallback. Voice, the
      // only caller whose own stillWanted can be false, independently
      // re-checks that identical condition before acting on any result
      // (persist/finish in InlineVoiceLive.tsx), so it safely ignores
      // whatever id comes back once it's no longer wanted. If nothing has
      // activated at all yet (this call's own activation was declined and
      // no other intent has settled either), fall back to this creation's
      // own id exactly as before.
      return sessionIdRef.current ?? id;
    },
    [computeSessionIntentKey, draftDefaults, selectedModel, systemPrompt],
  );

  // Lets a caller (voice's "Stop waiting") release a pending session
  // creation immediately instead of waiting out SESSION_CREATION_TIMEOUT_MS.
  // Recomputes the SAME intentKey formula ensureSession uses and only acts
  // if creatingRef.current still matches it -- key-safe rather than
  // identity-safe, since the caller here (InlineVoiceLive) has no access to
  // ensureSession's internal entry/promise, only to the settings that would
  // produce the same key. If a DIFFERENT intent (different settings, or a
  // later generation) now occupies the slot, this is a no-op: the caller
  // abandoning its OWN wait must never cancel someone else's creation.
  // Likewise a no-op if this intent already activated or failed, since
  // creatingRef is cleared by the original `.finally()` once a creation
  // settles -- there is nothing left in flight to abandon.
  //
  // Detaching from the cache slot (so no FUTURE caller joins this entry) is
  // always safe and happens unconditionally. Actually cancelling the
  // in-flight network request is NOT always safe: this exact entry may be
  // shared by a concurrent, unrelated send()/upload()/other voice attempt
  // that never asked to abandon anything (ensureSession dedupes ANY callers
  // whose settings/generation produce the same key, not just voice's own).
  // entry.waiterCount (see the comment on creatingRef) always includes this
  // abandoning attempt's own still-pending ensureSession() call even though
  // its caller (persist()) has already stopped listening via its own
  // PERSIST_TIMEOUT_MS -- that outer timeout doesn't cancel the underlying
  // ensureSession() call, only this explicit abort does -- so a count of 1
  // means "just me" (safe to abort) and a count above 1 means someone else
  // is genuinely still relying on the live result (must not abort out from
  // under them; they'll still resolve normally, or evict/retry on their own
  // if the bound eventually trips).
  const abandonPendingSessionCreation = useCallback(() => {
    const intentKey = computeSessionIntentKey(selectionGenerationRef.current);
    const entry = creatingRef.current;
    if (entry && entry.intentKey === intentKey) {
      creatingRef.current = null;
      if (entry.waiterCount <= 1) {
        entry.controller.abort();
      }
    }
  }, [computeSessionIntentKey]);

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
  //
  // isStillValid is re-checked by the caller (InlineVoiceLive) right after the
  // backend append resolves -- it reports false once this exact voice attempt
  // has been discarded or superseded by a newer cycle. The append itself is
  // never aborted (a save already in flight keeps running server-side), but
  // every client-side commit derived from its result -- messages, inspector
  // version, the post-append refetch/reconcile -- is gated on both that
  // predicate and the same session-generation check used elsewhere (selectSession/
  // ensureSession), so a stale attempt can never splice old voice turns into a
  // conversation the user has since discarded, restarted, or navigated away from
  // and back to.
  const persistVoiceConversation = useCallback(
    async (
      sessionId: string,
      conversationId: string,
      turns: VoiceTurnInput[],
      isStillValid: () => boolean,
    ) => {
      if (turns.length === 0) return;
      const capturedGeneration = selectionGenerationRef.current;
      const isCurrent = () =>
        isCurrentSessionGeneration(
          sessionId,
          capturedGeneration,
          sessionIdRef.current,
          selectionGenerationRef.current,
        ) && isStillValid();
      const created = await api.appendVoiceTurns(
        sessionId,
        conversationId,
        turns,
      );
      if (isCurrent()) {
        setMessages((previous) => {
          const createdIds = new Set(created.map((message) => message.id));
          return [
            ...previous.filter((message) => !createdIds.has(message.id)),
            ...created,
          ];
        });
        setInspectorVersion((value) => value + 1);
      }
      void Promise.allSettled([
        api.listMessages(sessionId).then((fresh) => {
          if (isCurrent()) {
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
    abandonPendingSessionCreation,
    persistConversation: persistVoiceConversation,
  });
  // Session/chat navigation is blocked only while there is voice data that
  // would actually be lost — an open mic with no exchanges yet never blocks a
  // switch (see useInlineVoiceLive.hasUnsavedTurns).
  const voiceExitLocked = inlineVoice.exitLocked;
  // Sidebar navigation is hard-disabled (not just soft-gated like the upload
  // lock) while streaming or while voice data is unsaved, so a plain
  // `disabled` attribute leaves users with no idea why the button won't
  // respond or how to get out. Surface the reason — and, for the voice case,
  // the recovery path (Retry saving/Stop waiting in the voice status bar) — via a
  // visible, aria-describedby-linked hint on those controls (Sidebar renders
  // its own shared hint; the header's standalone EditableSessionTitle gets
  // its own copy below via headerLockReasonId, since it lives outside the
  // Sidebar's DOM subtree and can't reference an id from there).
  const sidebarDisabledReason = streaming
    ? "Wait for the current reply to finish generating."
    : voiceExitLocked
      ? "Finish saving the voice transcript before switching conversations. Use \u201cRetry saving\u201d or \u201cStop waiting\u201d in the voice status bar below."
      : undefined;
  const headerLockReasonId = useId();
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

      // sessionId is now a real, resolved id this call is about to actively
      // consume -- record it so a later-resolving, differently-keyed
      // ensureSession() call can detect a genuine in-flight consumer (see
      // currentSessionInUse) instead of only seeing the caller-agnostic
      // streamingRef.current, which was already true before this call had
      // any session id at all.
      streamingSessionIdRef.current = sessionId;

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
        // Identity-safe: only clear if it's still this call's own session id
        // (there's only ever one in-flight send() at a time thanks to the
        // streamingRef.current guard above, but this keeps the invariant
        // explicit rather than relying on that external guarantee).
        if (streamingSessionIdRef.current === sessionId) {
          streamingSessionIdRef.current = null;
        }
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
          setInspectorVersion((value) => value + 1);
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
          disabledReason={sidebarDisabledReason}
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
            <>
              <EditableSessionTitle
                title={
                  sessions.find((session) => session.id === activeId)?.title ??
                  "Untitled"
                }
                onSave={(title) => renameSession(activeId, title)}
                disabled={streaming || voiceExitLocked}
                disabledReasonId={headerLockReasonId}
              />
              {(streaming || voiceExitLocked) && sidebarDisabledReason && (
                <span
                  id={headerLockReasonId}
                  role="status"
                  className="visually-hidden"
                >
                  {sidebarDisabledReason}
                </span>
              )}
            </>
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
