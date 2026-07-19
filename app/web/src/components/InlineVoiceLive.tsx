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
  // isStillWanted is re-checked by the caller (ChatApp) right before a newly
  // created session is made active, alongside its own selection-generation
  // check -- letting persist() signal that a discarded/superseded attempt no
  // longer wants the session it triggered creation of to hijack navigation
  // once creation resolves, even when no actual navigation ever happened.
  ensureSession: (isStillWanted?: () => boolean) => Promise<string>;
  persistConversation: (
    sessionId: string,
    conversationId: string,
    turns: VoiceTurnInput[],
    // Re-checked by the caller (ChatApp) right before it commits any
    // client-side state from this save -- mirrors isStillWanted above.
    // Reports false once this exact attempt has been discarded or
    // superseded by a newer cycle, even though the underlying save request
    // itself (already in flight) is left to complete in the background.
    isStillValid: () => boolean,
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
  "Saving the voice transcript is taking too long. Retry, or stop waiting to continue.";

function finalizedTurns(turns: LiveTurn[], active: boolean): VoiceTurnInput[] {
  return turns
    .filter((turn) => {
      const settled = !turn.pending && !turn.streaming;
      // Once the connection has ended, a still-open (pending/streaming) turn
      // can never receive more content -- voiceLive.ts never flips those
      // flags after a mid-turn teardown, so waiting for "settled" would wait
      // forever. If it already holds real text (most commonly an assistant
      // reply cut off mid-stream by stop()), treat connection-end itself as
      // the finalization signal so the genuine partial exchange is saved
      // instead of silently lost. An empty still-open turn (nothing was ever
      // said) is excluded either way by the trim() check below, and is
      // separately dropped from view entirely -- see the `messages` filter.
      return (settled || !active) && turn.text.trim();
    })
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
  // Mirrors live.active alongside live.turns for persist()'s synchronous
  // first call (fired from stop() before teardown/re-render), which must
  // read a ref rather than the reactive `live.active` prop directly -- see
  // finalizedTurns() above and the call site below. stop() also writes this
  // ref directly and eagerly (see stop() below) -- this effect subsequently
  // re-confirms the same `false` value once live.active itself catches up,
  // never overwriting it back to a stale `true`.
  const liveActiveRef = useRef(live.active);
  useLayoutEffect(() => {
    turnsRef.current = live.turns;
    liveActiveRef.current = live.active;
  }, [live.turns, live.active]);
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
  // Monotonic per-attempt token. Each persist() call captures the value it
  // bumps to; discardPersistence() also bumps it. A settling attempt only
  // touches saving/persisted/persistenceError (or session/conversation
  // binding) if its own captured id still matches the current value —
  // otherwise a NEWER cycle has since started or the attempt was explicitly
  // discarded, and its outcome (however it eventually resolves) must be a
  // no-op rather than mutate state that now belongs to a different cycle.
  // A plain boolean ("abandoned") can't express this: resetting it for a new
  // attempt also un-silences any older, still in-flight attempt that hasn't
  // settled yet.
  const attemptIdRef = useRef(0);

  const persist = useCallback((): Promise<void> => {
    if (persistedRef.current) return Promise.resolve();
    if (persistenceRef.current) return persistenceRef.current;
    const turns = finalizedTurns(turnsRef.current, liveActiveRef.current);
    if (turns.length === 0) return Promise.resolve();

    const attemptId = ++attemptIdRef.current;
    // Captured now, not read fresh when the save eventually settles: by then
    // a newer cycle's start() may have already reassigned
    // conversationIdRef.current, which would otherwise persist this
    // attempt's turns under the WRONG (newer) conversation id.
    const conversationIdForAttempt = conversationIdRef.current;
    // boundSessionId is read directly, not just the ref, so a session bound
    // moments ago by the render-time adjustment above is never missed: the
    // ref only caches an in-flight ensureSession() call made while no session
    // was bound yet, to dedupe concurrent persist() attempts.
    //
    // ensureSession() is contractually async (Promise<string>) and should
    // never throw synchronously, but persist() itself must not either:
    // callers fire it with `void persist()` and immediately run more code
    // afterward -- stop() calls the real WS/mic/AudioContext teardown
    // (stopLive()) on the very next line. If ensureSession violated its
    // contract and threw synchronously (e.g. a future bug, or a caller-side
    // regression mirroring the kind of type mismatch that has caused
    // backend session-update failures), an uncaught throw here would abort
    // stop() before stopLive() ever ran, trapping the live connection open.
    // Catching it and funneling it through the same terminal state as any
    // other save failure keeps that teardown guarantee unconditional.
    let sessionPromise: Promise<string>;
    try {
      // The predicate lets ensureSession's caller-side commit (ChatApp) know
      // this exact attempt is what's asking: if discardPersistence() (or a
      // newer persist() cycle) bumps attemptIdRef before the underlying
      // createSession() network call resolves, the created session still
      // joins the sidebar but must not force-navigate the UI to it or get
      // treated as "current" by ChatApp -- even though nothing here ever
      // called selectSession/newChat.
      sessionPromise = boundSessionId
        ? Promise.resolve(boundSessionId)
        : (sessionPromiseRef.current ??
          ensureSession(() => attemptIdRef.current === attemptId));
    } catch (error) {
      setPersistenceError(
        error instanceof Error
          ? error.message
          : "Couldn't save the Voice Live transcript.",
      );
      return Promise.resolve();
    }
    sessionPromiseRef.current = sessionPromise;
    setPersistenceError(null);
    setSaving(true);

    // finish() is intentionally reentrant, gated on attemptId currency
    // rather than a one-shot "settled" latch. The underlying fetch chain is
    // not aborted (no AbortSignal is plumbed through ensureSession/
    // persistConversation), so if the PERSIST_TIMEOUT_MS timeout wins the
    // race and reports an error first, the real request keeps running in the
    // background. When it later completes for real, a still-current (not
    // superseded) attempt must let that real outcome correct the UI --
    // clearing a timeout-driven error and marking the turns persisted on
    // late success, or replacing the timeout message with the real failure
    // on late failure -- otherwise the UI would stay permanently
    // locked/erroring even though the data safely landed durably. Only an
    // attempt actually superseded by discardPersistence() or a newer
    // persist() cycle (attemptIdRef no longer matching) is suppressed.
    // clearTimeout() and the persistenceRef/sessionPromiseRef identity
    // checks below are unconditional and idempotent, so calling finish() a
    // second time for the same still-current attempt is safe, and the outer
    // Promise's own resolve() is a no-op once already settled.
    const request = new Promise<void>((resolve) => {
      const finish = (error: Error | null) => {
        clearTimeout(timeoutId);
        if (persistenceRef.current === request) persistenceRef.current = null;
        if (attemptIdRef.current !== attemptId) {
          // Superseded by discardPersistence() or a later persist() call:
          // don't resurrect an error or flip saving/persisted for a cycle
          // this attempt no longer belongs to.
          resolve();
          return;
        }
        setSaving(false);
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
          if (attemptIdRef.current !== attemptId) {
            // Superseded while the session was still resolving: skip
            // binding to it and skip the network call to persist stale,
            // already-discarded turns under a session/conversation that no
            // longer matches the current cycle.
            return undefined;
          }
          setBindingCommitted(true);
          setBoundSessionId(sessionId);
          return persistConversation(
            sessionId,
            conversationIdForAttempt,
            turns,
            () => attemptIdRef.current === attemptId,
          );
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
    // Invalidates any attempt captured so far -- in-flight or not -- so its
    // eventual settlement (however long it takes, since the underlying
    // request is not aborted) can never mutate state again.
    attemptIdRef.current += 1;
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
    // stopLive() (voiceLive.ts's own stop()) tears down the socket/mic/
    // AudioContext fully synchronously (no awaits before its own status
    // flips to "idle"), so no turn can gain any more text after this point
    // -- the connection is over the instant this function runs, even though
    // live.active's reactive update (and the mirroring effect above) won't
    // catch up until React's next commit. Writing the ref directly, right
    // now, makes this call's finalizedTurns() computation already treat any
    // still-open turn's real content as final, instead of computing an
    // incomplete result off the stale `true` and caching it as if it were
    // everything: persist()'s own in-flight/already-persisted guards assume
    // a cycle's calls are all asking for the same thing, which only holds if
    // every call agrees on `active` up front. Without this, a call made
    // moments later by the effect below (once live.active genuinely
    // flips) recomputes the correct, larger result but is then silently
    // discarded by those guards, permanently dropping a still-open turn's
    // content whenever an earlier turn in the same call had already
    // finished normally.
    liveActiveRef.current = false;
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
      .filter((turn) => {
        const stillOpen = turn.pending || turn.streaming;
        // Once the live connection has ended, a turn that is still open but
        // never received any real content -- the user started speaking and
        // stopped before a transcript arrived, or the assistant was cut off
        // before its first token -- can never resolve. Drop it outright
        // instead of leaving a permanent "Listening…"/generating indicator
        // with no way to complete. A turn that already has real text is
        // kept (see the map below, which turns its live indicator off), so
        // a genuine partial exchange stays visible.
        if (!live.active && stillOpen && !turn.text.trim()) return false;
        // Once the connection has ended, a still-open turn is as final as
        // it will ever get (mirrors finalizedTurns() above), even though
        // voiceLive.ts never flips its own pending/streaming flag after a
        // mid-turn teardown. Without this, a turn now eligible for (and
        // subsequently) persisted under that same rule would never drop
        // from this transient overlay -- producing a permanent duplicate
        // once ChatApp's own `messages` state also picks up the saved copy.
        const isFinal = !live.active || !stillOpen;
        return !persisted || !isFinal || !turn.text.trim();
      })
      .map((turn) => {
        // Mirrors the filter above: a turn still open the instant the
        // connection closed can never receive more content, so stop
        // presenting it as in-progress even though it was never explicitly
        // finalized.
        const stillLive = live.active && (turn.pending || turn.streaming);
        return {
          id: `voice-live-${cycleId}-${turn.id}`,
          role: turn.role,
          content:
            turn.text || (stillLive && turn.role === "user" ? "Listening…" : ""),
          createdAt: turn.createdAt,
          pending: stillLive,
          source: "voice",
          agent: turn.role === "assistant" ? agent : null,
        };
      });
  }, [agent, cycleId, live.active, live.turns, persisted]);

  // Whether leaving now would lose data. A live (or connecting) session with
  // no exchanges yet is NOT unsaved — nothing has been said. Once the call
  // is still connected and has produced any turn (even one still pending),
  // navigating away would stop the call and truncate whatever is mid-flight,
  // so that keeps blocking regardless of finalization. Once the call has
  // ended, finalizedTurns() itself now finalizes any still-open turn that
  // already holds real text (a mid-stream cutoff), so that content keeps
  // counting as unsaved here too, right up until persist() actually saves
  // it -- only a turn that was still open AND empty the instant stop() cut
  // the connection can never be completed or saved, so that (and only that)
  // case must stop counting once inactive, or navigation would lock forever
  // with neither a "Saving…" state nor a Retry/Stop-waiting control able to
  // appear (both are gated on saving/persistenceError).
  const hasUnsavedTurns =
    saving ||
    Boolean(persistenceError) ||
    (!persisted &&
      live.turns.length > 0 &&
      (live.active || finalizedTurns(live.turns, live.active).length > 0));

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
          title="Stop waiting on this voice transcript so chat navigation unlocks immediately. A save already in progress isn't cancelled and may still complete in the background."
          style={{
            border: "1px solid var(--border)",
            borderRadius: 999,
            padding: "3px 9px",
            background: "var(--bg)",
            color: "var(--fg)",
            cursor: "pointer",
          }}
        >
          Stop waiting
        </button>
      )}
    </div>
  );
}
