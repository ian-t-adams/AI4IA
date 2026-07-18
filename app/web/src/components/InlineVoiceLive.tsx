"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import type { DisplayMessage } from "./MessageList";
import type { AgentSummary, VoiceTurnInput } from "@/lib/types";
import {
  DEFAULT_VOICE,
  DEFAULT_VOICE_PROVIDER,
  DEFAULT_VOICE_SETTINGS,
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  useVoiceLive,
  type LiveTurn,
  type SpeechVoiceLiveSettings,
  type VoiceProviderId,
  type VoiceLiveConfig,
  type VoiceSeedTurn,
  type VoiceSessionSettings,
} from "@/lib/voiceLive";

export type InlineVoicePhase =
  | "idle"
  | "connecting"
  | "listening"
  | "thinking"
  | "speaking"
  | "ending";

interface InlineVoiceLiveOptions {
  config: VoiceLiveConfig;
  providerId?: VoiceProviderId;
  model: string | null;
  region?: string | null;
  agent: string | null;
  agents: AgentSummary[];
  history: VoiceSeedTurn[];
  // Voice/session settings + the governed-tools opt-in, threaded straight into
  // useVoiceLive. Optional so existing callers keep today's behavior (default
  // voice/settings, tools off) until they opt into the settings panel.
  voice?: string;
  settings?: VoiceSessionSettings;
  speechSettings?: SpeechVoiceLiveSettings;
  tools?: boolean;
  // Existing chat at the moment Voice Live starts. Binding this without
  // calling ensureSession avoids empty-chat creation while ensuring a later
  // finalized turn cannot drift into a different chat after navigation.
  activeSessionId?: string | null;
  ensureSession: () => Promise<string>;
  persistConversation: (
    sessionId: string,
    conversationId: string,
    turns: VoiceTurnInput[],
  ) => Promise<void>;
}

export interface InlineVoiceLiveState {
  messages: DisplayMessage[];
  enabled: boolean;
  supported: boolean;
  active: boolean;
  saving: boolean;
  phase: InlineVoicePhase;
  statusLabel: string;
  agentLabel: string;
  error: string | null;
  persistenceError: string | null;
  // True while there are finalized-but-unsaved voice turns (or a save is in
  // flight / failed) that would be lost by navigating away. False for a live
  // connection that has produced no exchanges yet, so switching sessions is
  // never blocked merely because the microphone happens to be open.
  hasUnsavedTurns: boolean;
  // Alias of hasUnsavedTurns for callers gating session/chat navigation.
  exitLocked: boolean;
  // Existing chat captured when this Voice Live cycle started. Null means the
  // cycle began in an empty chat and may bind lazily when its first turn saves.
  boundSessionId: string | null;
  start: () => void;
  stop: () => void;
  retryPersistence: () => void;
  // Abandons a stuck (still saving) or failed voice transcript so navigation
  // unlocks immediately without waiting on the network. The underlying save
  // request, if still in flight, is left to resolve in the background (its
  // outcome is ignored) rather than aborted, since fetch cancellation isn't
  // plumbed through ensureSession/persistConversation.
  discardPersistence: () => void;
}

// Bounds how long a voice transcript save can leave the UI in "saving"
// state. ensureSession()/persistConversation() are plain fetches with no
// AbortController plumbed through them, so a hung request (dropped
// connection, backend stall) previously left `saving` true forever — which
// permanently disabled the Composer's voice button (by design, to prevent a
// second concurrent cycle) and permanently blocked chat/session navigation
// with no feedback. This timeout guarantees `saving` always resolves to
// either success or a diagnosable persistenceError within a bounded time.
const PERSIST_TIMEOUT_MS = 20_000;
const PERSIST_TIMEOUT_MESSAGE =
  "Saving the voice transcript is taking too long. Retry, or discard it to continue.";

function finalizedTurns(turns: LiveTurn[]): VoiceTurnInput[] {
  return turns
    .filter((turn) => !turn.pending && !turn.streaming && turn.text.trim())
    .map((turn) => ({
      role: turn.role,
      text: turn.text.trim(),
      ...(turn.createdAt ? { createdAt: turn.createdAt } : {}),
    }));
}

export function mergeDisplayMessages(
  base: DisplayMessage[],
  live: DisplayMessage[],
): DisplayMessage[] {
  return [...base, ...live].sort((left, right) => {
    if (!left.createdAt || !right.createdAt) return 0;
    return Date.parse(left.createdAt) - Date.parse(right.createdAt);
  });
}

export function voiceMessagesForSession(
  messages: DisplayMessage[],
  boundSessionId: string | null,
  activeSessionId: string | null,
): DisplayMessage[] {
  return boundSessionId === activeSessionId ? messages : [];
}

function phaseFor(
  status: "idle" | "connecting" | "live" | "closing",
  listening: boolean,
  speaking: boolean,
  turns: LiveTurn[],
): InlineVoicePhase {
  if (status === "connecting") return "connecting";
  if (status === "closing") return "ending";
  if (status === "idle") return "idle";
  if (listening) return "listening";
  if (speaking) return "speaking";
  const last = turns[turns.length - 1];
  if (last?.role === "user" && !last.pending) return "thinking";
  return "listening";
}

function labelFor(phase: InlineVoicePhase): string {
  switch (phase) {
    case "connecting":
      return "Connecting";
    case "listening":
      return "Listening";
    case "thinking":
      return "Thinking";
    case "speaking":
      return "Speaking";
    case "ending":
      return "Ending";
    default:
      return "Voice Live ready";
  }
}

export function useInlineVoiceLive({
  config,
  providerId = DEFAULT_VOICE_PROVIDER,
  model,
  region = null,
  agent,
  agents,
  history,
  voice = DEFAULT_VOICE,
  settings = DEFAULT_VOICE_SETTINGS,
  speechSettings = DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  tools = false,
  activeSessionId = null,
  ensureSession,
  persistConversation,
}: InlineVoiceLiveOptions): InlineVoiceLiveState {
  const [connectionError, setConnectionError] = useState<string | null>(null);
  const [persistenceError, setPersistenceError] = useState<string | null>(null);
  const [persisted, setPersisted] = useState(false);
  const [saving, setSaving] = useState(false);
  const [cycleId, setCycleId] = useState("initial");
  const [boundSessionId, setBoundSessionId] = useState<string | null>(null);

  const live = useVoiceLive(
    config,
    providerId,
    model,
    region,
    voice,
    setConnectionError,
    agent,
    history,
    settings,
    speechSettings,
    tools,
    activeSessionId,
  );
  const startLive = live.start;
  const stopLive = live.stop;

  const turnsRef = useRef(live.turns);
  useLayoutEffect(() => {
    turnsRef.current = live.turns;
  }, [live.turns]);
  const sessionPromiseRef = useRef<Promise<string> | null>(null);
  const persistenceRef = useRef<Promise<void> | null>(null);
  const persistedRef = useRef(false);
  const wasActiveRef = useRef(false);
  const conversationIdRef = useRef("");
  // Whether the current cycle's chat binding has locked in (see the
  // boundSessionId adjustment below). State rather than a ref: it is read
  // during render there, and refs cannot be read or written outside of
  // effects/callbacks.
  const [bindingCommitted, setBindingCommitted] = useState(false);
  // Set by discardPersistence() to tell an in-flight (or about-to-time-out)
  // save attempt that its eventual outcome should be ignored: the user has
  // already explicitly abandoned it and the UI has moved on.
  const abandonedRef = useRef(false);

  const persist = useCallback((): Promise<void> => {
    if (persistedRef.current) return Promise.resolve();
    if (persistenceRef.current) return persistenceRef.current;
    const turns = finalizedTurns(turnsRef.current);
    if (turns.length === 0) return Promise.resolve();

    abandonedRef.current = false;
    // boundSessionId is read directly, not just the ref, so a session bound
    // moments ago by the render-time adjustment above is never missed: the
    // ref only caches an in-flight ensureSession() call made while no session
    // was bound yet, to dedupe concurrent persist() attempts.
    const sessionPromise = boundSessionId
      ? Promise.resolve(boundSessionId)
      : (sessionPromiseRef.current ?? ensureSession());
    sessionPromiseRef.current = sessionPromise;
    setPersistenceError(null);
    setSaving(true);

    // settled + finish() guard so whichever happens first — the real
    // request completing, or the timeout firing — is the only one that acts.
    // The underlying fetch chain is not aborted (no AbortSignal is plumbed
    // through ensureSession/persistConversation); this only bounds how long
    // the UI can be stuck waiting on it, and a late real completion after a
    // timeout is safely ignored via `settled`.
    const request = new Promise<void>((resolve) => {
      let settled = false;
      const finish = (error: Error | null) => {
        if (settled) return;
        settled = true;
        clearTimeout(timeoutId);
        if (persistenceRef.current === request) persistenceRef.current = null;
        setSaving(false);
        if (abandonedRef.current) {
          // discardPersistence() already forced terminal state; don't
          // resurrect an error or flip persisted based on a stale outcome.
          resolve();
          return;
        }
        if (error) {
          if (sessionPromiseRef.current === sessionPromise) {
            sessionPromiseRef.current = null;
          }
          setPersistenceError(
            error.message || "Couldn't save the Voice Live transcript.",
          );
        } else {
          persistedRef.current = true;
          setPersisted(true);
          setPersistenceError(null);
        }
        resolve();
      };

      const timeoutId = setTimeout(() => {
        finish(new Error(PERSIST_TIMEOUT_MESSAGE));
      }, PERSIST_TIMEOUT_MS);

      sessionPromise
        .then((sessionId) => {
          setBindingCommitted(true);
          setBoundSessionId(sessionId);
          return persistConversation(sessionId, conversationIdRef.current, turns);
        })
        .then(() => finish(null))
        .catch((error: unknown) =>
          finish(error instanceof Error ? error : new Error(String(error))),
        );
    });
    persistenceRef.current = request;
    return request;
  }, [boundSessionId, ensureSession, persistConversation]);

  const discardPersistence = useCallback(() => {
    abandonedRef.current = true;
    persistenceRef.current = null;
    sessionPromiseRef.current = null;
    persistedRef.current = true;
    setSaving(false);
    setPersistenceError(null);
    setPersisted(true);
  }, []);

  const start = useCallback(() => {
    if (persistenceRef.current) return;
    if (persistenceError) {
      void persist();
      return;
    }
    setConnectionError(null);
    setPersistenceError(null);
    setPersisted(false);
    persistedRef.current = false;
    const nextConversationId =
      typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    conversationIdRef.current = nextConversationId;
    setCycleId(nextConversationId);
    sessionPromiseRef.current = activeSessionId
      ? Promise.resolve(activeSessionId)
      : null;
    setBindingCommitted(activeSessionId !== null);
    setBoundSessionId(activeSessionId);

    // The controller begins getUserMedia/AudioContext work synchronously here,
    // directly inside the original button gesture. The relay does not need a
    // session id to open a live connection, so no session is created here: an
    // empty chat that never produces a finalized turn (denied mic permission,
    // a gateway failure, a bare retry) never creates one. Session creation is
    // deferred entirely to persist(), which lazily calls ensureSession() only
    // once there is a finalized turn that actually needs saving.
    startLive();
  }, [activeSessionId, persist, persistenceError, startLive]);

  const stop = useCallback(() => {
    void persist();
    stopLive();
  }, [persist, stopLive]);

  useEffect(() => {
    if (live.active && !wasActiveRef.current) {
      persistedRef.current = false;
      setPersisted(false);
    } else if (wasActiveRef.current && !live.active) {
      void persist();
    }
    wasActiveRef.current = live.active;
  }, [live.active, persist]);

  // A cycle that began in an empty chat may follow navigation only while it is
  // still truly silent. As soon as the upstream emits its first pending or
  // finalized turn, commit the current chat binding; that turn is then hidden
  // from every other chat and persistence cannot drift later.
  //
  // Adjusted here during render rather than in an effect: boundSessionId is
  // sticky history (once committed it must not drift just because
  // activeSessionId changes elsewhere), so it can't be derived fresh each
  // render, but it also must never lag a render behind bindingCommitted. An
  // effect-deferred update leaves a one-frame window, after a relevant
  // prop/turn change, where boundSessionId still names the previous chat —
  // exactly the kind of stale state a late-arriving upstream event could
  // otherwise re-lock onto. Doing it here means React finishes reconciling
  // with the corrected value in the same pass, before anything commits or
  // paints (see "Adjusting some state when a prop changes",
  // https://react.dev/learn/you-might-not-need-an-effect). Only state is
  // touched here, never sessionPromiseRef: refs cannot be read or written
  // during render, so persist() reads boundSessionId itself instead of a
  // ref mirror of it (see persist() above).
  if (bindingCommitted) {
    // Text remains available during Voice Live. If a text send creates the
    // session for the same formerly-empty chat, adopt that new id so the live
    // transcript stays visible. Once a non-null id is bound, navigation locks
    // prevent it from drifting to another chat while turns are unsaved.
    if (live.active && boundSessionId === null && activeSessionId !== null) {
      setBoundSessionId(activeSessionId);
    }
  } else if (live.turns.length === 0) {
    if (boundSessionId !== activeSessionId) {
      setBoundSessionId(activeSessionId);
    }
  } else {
    setBindingCommitted(true);
    setBoundSessionId(activeSessionId);
  }

  const phase = phaseFor(
    live.status,
    live.listening,
    live.speaking,
    live.turns,
  );
  const agentLabel = agent
    ? agents.find((candidate) => candidate.name === agent)?.displayName ?? agent
    : "";

  const messages = useMemo<DisplayMessage[]>(() => {
    return live.turns
      .filter(
        (turn) =>
          !persisted || turn.pending || turn.streaming || !turn.text.trim(),
      )
      .map((turn) => ({
        id: `voice-live-${cycleId}-${turn.id}`,
        role: turn.role,
        content:
          turn.text ||
          (turn.role === "user" && turn.pending ? "Listening…" : ""),
        createdAt: turn.createdAt,
        pending: turn.pending || turn.streaming,
        source: "voice",
        agent: turn.role === "assistant" ? agent : null,
      }));
  }, [agent, cycleId, live.turns, persisted]);

  // Whether leaving now would lose data. A live (or connecting) session with
  // no exchanges yet is NOT unsaved — only finalized turns awaiting/failing
  // persistence (or an in-flight save) make navigating away destructive.
  const hasUnsavedTurns =
    saving || Boolean(persistenceError) || (!persisted && live.turns.length > 0);

  return {
    messages,
    enabled: config.enabled && (providerId === "speech_voice_live" || model !== null),
    supported: live.supported,
    active: live.active,
    saving,
    phase,
    statusLabel: saving ? "Saving voice transcript" : labelFor(phase),
    agentLabel,
    error: connectionError,
    persistenceError,
    hasUnsavedTurns,
    exitLocked: hasUnsavedTurns,
    boundSessionId,
    start,
    stop,
    retryPersistence: () => void persist(),
    discardPersistence,
  };
}

export function InlineVoiceLiveStatus({
  voice,
}: {
  voice: InlineVoiceLiveState;
}) {
  if (!voice.enabled) return null;

  const error = voice.persistenceError ?? voice.error;
  const isActive = voice.phase !== "idle";
  const status = !voice.supported
    ? "Voice Live isn't supported in this browser."
    : error ?? voice.statusLabel;
  return (
    <div
      aria-live="polite"
      aria-busy={
        voice.phase === "connecting" || voice.phase === "ending" || voice.saving
      }
      style={{
        display: "flex",
        alignItems: "center",
        flexWrap: "wrap",
        gap: 8,
        minHeight: 32,
        padding: "5px max(16px, 6%)",
        borderTop: "1px solid var(--border)",
        background: "var(--bg-elevated)",
        color: error ? "var(--danger)" : "var(--fg-muted)",
        fontSize: "0.78em",
      }}
    >
      <span
        aria-hidden="true"
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: error
            ? "var(--danger)"
            : isActive
              ? "var(--accent)"
              : "var(--border)",
        }}
      />
      <strong style={{ color: error ? "var(--danger)" : "var(--fg)" }}>
        {status}
      </strong>
      {!error && voice.agentLabel && <span>with {voice.agentLabel}</span>}
      {!error && voice.active && (
        <span title="Typed messages stay in this chat and are included the next time Voice Live connects.">
          You can keep typing in this chat.
        </span>
      )}
      {voice.error && !voice.active && !voice.persistenceError && (
        <button
          type="button"
          onClick={voice.start}
          style={{
            border: "1px solid var(--border)",
            borderRadius: 999,
            padding: "3px 9px",
            background: "var(--bg)",
            color: "var(--fg)",
            cursor: "pointer",
          }}
        >
          Retry
        </button>
      )}
      {voice.persistenceError && (
        <button
          type="button"
          onClick={voice.retryPersistence}
          style={{
            border: "1px solid var(--border)",
            borderRadius: 999,
            padding: "3px 9px",
            background: "var(--bg)",
            color: "var(--fg)",
            cursor: "pointer",
          }}
        >
          Retry saving
        </button>
      )}
      {(voice.saving || voice.persistenceError) && (
        <button
          type="button"
          onClick={voice.discardPersistence}
          title="Abandon this voice transcript without saving it. Chat navigation unlocks immediately."
          style={{
            border: "1px solid var(--border)",
            borderRadius: 999,
            padding: "3px 9px",
            background: "var(--bg)",
            color: "var(--fg)",
            cursor: "pointer",
          }}
        >
          Discard
        </button>
      )}
    </div>
  );
}
