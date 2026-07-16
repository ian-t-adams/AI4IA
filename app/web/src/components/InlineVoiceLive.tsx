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
}

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
  const bindingCommittedRef = useRef(false);

  const persist = useCallback((): Promise<void> => {
    if (persistedRef.current) return Promise.resolve();
    if (persistenceRef.current) return persistenceRef.current;
    const turns = finalizedTurns(turnsRef.current);
    if (turns.length === 0) return Promise.resolve();

    const sessionPromise = sessionPromiseRef.current ?? ensureSession();
    sessionPromiseRef.current = sessionPromise;
    setPersistenceError(null);
    setSaving(true);
    const request = sessionPromise
      .then((sessionId) => {
        bindingCommittedRef.current = true;
        setBoundSessionId(sessionId);
        return persistConversation(sessionId, conversationIdRef.current, turns);
      })
      .then(() => {
        persistedRef.current = true;
        setPersisted(true);
        setPersistenceError(null);
      })
      .catch((error: unknown) => {
        if (sessionPromiseRef.current === sessionPromise) {
          sessionPromiseRef.current = null;
        }
        setPersistenceError(
          (error as Error).message || "Couldn't save the Voice Live transcript.",
        );
      })
      .finally(() => {
        persistenceRef.current = null;
        setSaving(false);
      });
    persistenceRef.current = request;
    return request;
  }, [ensureSession, persistConversation]);

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
    bindingCommittedRef.current = activeSessionId !== null;
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
  useEffect(() => {
    if (bindingCommittedRef.current) {
      // Text remains available during Voice Live. If a text send creates the
      // session for the same formerly-empty chat, adopt that new id so the live
      // transcript stays visible. Once a non-null id is bound, navigation locks
      // prevent it from drifting to another chat while turns are unsaved.
      if (
        live.active &&
        boundSessionId === null &&
        activeSessionId !== null
      ) {
        setBoundSessionId(activeSessionId);
        sessionPromiseRef.current = Promise.resolve(activeSessionId);
      }
      return;
    }
    if (live.turns.length === 0) {
      setBoundSessionId(activeSessionId);
      sessionPromiseRef.current = activeSessionId
        ? Promise.resolve(activeSessionId)
        : null;
      return;
    }
    bindingCommittedRef.current = true;
    setBoundSessionId(activeSessionId);
    sessionPromiseRef.current = activeSessionId
      ? Promise.resolve(activeSessionId)
      : null;
  }, [activeSessionId, boundSessionId, live.active, live.turns.length]);

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
    </div>
  );
}
