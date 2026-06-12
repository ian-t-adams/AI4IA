"use client";

// Phase 10 Voice Live — the dedicated live-conversation surface. A modal (mirroring
// the Imagery Studio pattern) that turns the governed `/api/voice/live` relay into a
// genuinely usable voice assistant: a turn-by-turn timeline with live partial
// transcripts, a clear listening / thinking / speaking indicator (barge-in is wired
// in the engine and reflected here), governed-tool badges, and graceful
// connecting / live / ending / error states. It reuses the `useVoiceLive` audio
// engine unchanged — this component only renders its structured state.
import { useEffect, useMemo, useRef, useState } from "react";
import type { AgentSummary, VoiceTurnInput } from "@/lib/types";
import {
  useVoiceLive,
  DEFAULT_VOICE,
  REALTIME_VOICES,
  isRealtimeVoice,
  type LiveTurn,
  type VoiceLiveConfig,
  type VoiceSeedTurn,
} from "@/lib/voiceLive";

// Where the chosen live-voice persona / agent are remembered across reloads (shared
// with the prior Composer affordance so a returning user keeps their selection).
const VOICE_STORAGE_KEY = "ai4ia.voiceLive.voice";
const AGENT_STORAGE_KEY = "ai4ia.voiceLive.agent";

type Phase =
  | "idle"
  | "connecting"
  | "listening"
  | "thinking"
  | "speaking"
  | "ready"
  | "ending";

function phaseLabel(phase: Phase): string {
  switch (phase) {
    case "connecting":
      return "Connecting…";
    case "listening":
      return "Listening…";
    case "thinking":
      return "Thinking…";
    case "speaking":
      return "Speaking…";
    case "ready":
      return "Listening — speak anytime";
    case "ending":
      return "Ending…";
    default:
      return "Not connected";
  }
}

function phaseColor(phase: Phase): string {
  switch (phase) {
    case "listening":
      return "var(--danger)";
    case "speaking":
    case "ready":
      return "var(--accent)";
    case "connecting":
    case "thinking":
    case "ending":
      return "var(--fg-muted)";
    default:
      return "var(--fg-muted)";
  }
}

function TurnBubble({ turn }: { turn: LiveTurn }) {
  const isUser = turn.role === "user";
  return (
    <li
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: isUser ? "flex-end" : "flex-start",
        gap: 4,
      }}
    >
      <span
        style={{
          fontSize: "0.7em",
          textTransform: "uppercase",
          letterSpacing: 0.5,
          color: "var(--fg-muted)",
        }}
      >
        {isUser ? "You" : "Assistant"}
      </span>
      <div
        style={{
          maxWidth: "82%",
          padding: "10px 14px",
          borderRadius: 14,
          borderTopRightRadius: isUser ? 4 : 14,
          borderTopLeftRadius: isUser ? 14 : 4,
          background: isUser ? "var(--accent)" : "var(--bg)",
          color: isUser ? "var(--accent-fg)" : "var(--fg)",
          border: isUser ? "none" : "1px solid var(--border)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
          lineHeight: 1.45,
        }}
      >
        {turn.tool && (
          <div
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
              marginBottom: turn.text ? 6 : 0,
              padding: "2px 8px",
              borderRadius: 999,
              background: "var(--bg-elevated)",
              border: "1px solid var(--border)",
              color: "var(--fg-muted)",
              fontSize: "0.72em",
              fontWeight: 600,
            }}
          >
            <span aria-hidden="true">🔧</span> {turn.tool}
          </div>
        )}
        {turn.pending && !turn.text ? (
          <span style={{ color: "var(--fg-muted)", fontStyle: "italic" }}>Listening…</span>
        ) : (
          <span>
            {turn.text}
            {turn.streaming && (
              <span aria-hidden="true" style={{ opacity: 0.6 }}>
                {" "}
                ▍
              </span>
            )}
          </span>
        )}
      </div>
    </li>
  );
}

export function VoiceLivePanel({
  config,
  model,
  agents,
  onClose,
  onError,
  history = [],
  onConversation,
}: {
  config: VoiceLiveConfig;
  model: string | null;
  agents: AgentSummary[];
  onClose: () => void;
  onError?: (message: string) => void;
  // Recent text-chat turns to seed the live session so voice continues the same
  // conversation. When non-empty the panel shows it's joined to an active chat.
  history?: VoiceSeedTurn[];
  // Called once when a live session ends, with the finalized voice turns, so the
  // host can persist them back into the shared session's transcript.
  onConversation?: (turns: VoiceTurnInput[]) => void;
}) {
  // Chosen voice persists across reloads and locks for a session once live (the
  // model fixes the voice after its first audio reply), so the picker is disabled
  // while a session is active.
  const [liveVoice, setLiveVoice] = useState<string>(DEFAULT_VOICE);
  const [liveAgent, setLiveAgent] = useState<string>("");

  useEffect(() => {
    try {
      const v = window.localStorage.getItem(VOICE_STORAGE_KEY);
      if (v && isRealtimeVoice(v)) setLiveVoice(v);
      const a = window.localStorage.getItem(AGENT_STORAGE_KEY);
      if (a) setLiveAgent(a);
    } catch {
      /* storage unavailable -> defaults */
    }
  }, []);

  const enabledAgents = useMemo(() => agents.filter((a) => a.enabled), [agents]);

  // Drop a remembered agent that no longer exists / was disabled.
  useEffect(() => {
    if (!liveAgent) return;
    if (enabledAgents.length > 0 && !enabledAgents.some((a) => a.name === liveAgent)) {
      setLiveAgent("");
    }
  }, [enabledAgents, liveAgent]);

  const onPickVoice = (value: string) => {
    setLiveVoice(value);
    try {
      window.localStorage.setItem(VOICE_STORAGE_KEY, value);
    } catch {
      /* best effort */
    }
  };
  const onPickAgent = (value: string) => {
    setLiveAgent(value);
    try {
      if (value) window.localStorage.setItem(AGENT_STORAGE_KEY, value);
      else window.localStorage.removeItem(AGENT_STORAGE_KEY);
    } catch {
      /* best effort */
    }
  };

  const live = useVoiceLive(
    config,
    model,
    liveVoice,
    (msg) => onError?.(msg),
    liveAgent || null,
    history,
  );

  // Persist the finalized voice turns back into the shared session when a live
  // session ends (the active -> inactive edge). teardown() keeps ``turns`` intact
  // after stop, so the snapshot here is the full exchange. Refs avoid re-running
  // this on every turn/ prop change — it fires only on the end edge.
  const turnsRef = useRef(live.turns);
  turnsRef.current = live.turns;
  const onConversationRef = useRef(onConversation);
  useEffect(() => {
    onConversationRef.current = onConversation;
  }, [onConversation]);
  const wasActiveRef = useRef(false);
  useEffect(() => {
    if (wasActiveRef.current && !live.active) {
      const finalized: VoiceTurnInput[] = turnsRef.current
        .filter((t) => !t.pending && !t.streaming && t.text.trim())
        .map((t) => ({ role: t.role, text: t.text.trim() }));
      if (finalized.length > 0) onConversationRef.current?.(finalized);
    }
    wasActiveRef.current = live.active;
  }, [live.active]);

  const agentLabel = liveAgent
    ? enabledAgents.find((a) => a.name === liveAgent)?.displayName ?? liveAgent
    : "";

  const lastTurn = live.turns[live.turns.length - 1];
  const phase: Phase = useMemo(() => {
    if (live.status === "connecting") return "connecting";
    if (live.status === "closing") return "ending";
    if (live.status === "idle") return "idle";
    // live
    if (live.listening) return "listening";
    if (live.speaking) return "speaking";
    // A user turn is awaiting the assistant's reply.
    if (lastTurn && lastTurn.role === "user") return "thinking";
    return "ready";
  }, [live.status, live.listening, live.speaking, lastTurn]);

  // Auto-scroll the timeline as turns / partials grow.
  const timelineRef = useRef<HTMLOListElement>(null);
  useEffect(() => {
    const el = timelineRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [live.turns]);

  const close = () => {
    live.stop();
    onClose();
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const unsupported = !live.supported;
  const pulsing = phase === "listening" || phase === "speaking" || phase === "connecting";

  return (
    <div
      role="dialog"
      aria-label="Voice Live"
      aria-modal="true"
      onClick={close}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 50,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(760px, 95vw)",
          height: "min(760px, 90vh)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", flexDirection: "column" }}>
            <strong style={{ fontSize: "1.05em" }}>🎧 Voice Live</strong>
            <span style={{ fontSize: "0.78em", color: "var(--fg-muted)" }}>
              {history.length > 0
                ? "Continuing your chat — what you say joins the same conversation."
                : "Real-time speech-to-speech — talk naturally and the assistant talks back."}
            </span>
          </div>
          <button
            onClick={close}
            aria-label="Close Voice Live"
            style={{
              border: "1px solid var(--border)",
              background: "var(--bg)",
              color: "var(--fg)",
              borderRadius: 8,
              width: 32,
              height: 32,
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        {/* Controls */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 10, alignItems: "center" }}>
          {enabledAgents.length > 0 && (
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}>
              <span style={{ color: "var(--fg-muted)" }}>Agent</span>
              <select
                aria-label="Live voice agent"
                value={liveAgent}
                disabled={live.active}
                onChange={(e) => onPickAgent(e.target.value)}
                style={{
                  minHeight: 40,
                  padding: "0 8px",
                  borderRadius: 10,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                  color: "var(--fg)",
                  fontSize: "1em",
                  cursor: live.active ? "not-allowed" : "pointer",
                  opacity: live.active ? 0.6 : 1,
                }}
              >
                <option value="">Default assistant</option>
                {enabledAgents.map((a) => (
                  <option key={a.name} value={a.name}>
                    {a.displayName}
                  </option>
                ))}
              </select>
            </label>
          )}
          <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}>
            <span style={{ color: "var(--fg-muted)" }}>Voice</span>
            <select
              aria-label="Live voice"
              value={liveVoice}
              disabled={live.active}
              onChange={(e) => onPickVoice(e.target.value)}
              style={{
                minHeight: 40,
                padding: "0 8px",
                borderRadius: 10,
                border: "1px solid var(--border)",
                background: "var(--bg)",
                color: "var(--fg)",
                fontSize: "1em",
                cursor: live.active ? "not-allowed" : "pointer",
                opacity: live.active ? 0.6 : 1,
              }}
            >
              {REALTIME_VOICES.map((v) => (
                <option key={v} value={v}>
                  {v.charAt(0).toUpperCase() + v.slice(1)}
                </option>
              ))}
            </select>
          </label>

          <button
            type="button"
            onClick={live.toggle}
            disabled={unsupported || live.status === "connecting" || live.status === "closing"}
            aria-pressed={live.active}
            aria-busy={live.status === "connecting"}
            style={{
              marginLeft: "auto",
              alignSelf: "flex-end",
              minHeight: 44,
              padding: "0 22px",
              borderRadius: 10,
              border: "none",
              background: live.active ? "var(--danger)" : "var(--accent)",
              color: live.active ? "#fff" : "var(--accent-fg)",
              fontSize: "1em",
              fontWeight: 700,
              cursor: unsupported ? "not-allowed" : "pointer",
              opacity: unsupported ? 0.5 : 1,
              display: "inline-flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <span aria-hidden="true">
              {live.status === "connecting" ? "…" : live.active ? "■" : "●"}
            </span>
            {live.status === "connecting"
              ? "Connecting"
              : live.active
                ? "End conversation"
                : "Start conversation"}
          </button>
        </div>

        {/* Status indicator */}
        <div
          aria-live="polite"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 10,
            padding: "8px 12px",
            borderRadius: 10,
            background: "var(--bg)",
            border: "1px solid var(--border)",
            minHeight: 20,
          }}
        >
          <span
            aria-hidden="true"
            className={pulsing ? "vl-pulse" : undefined}
            style={{
              width: 12,
              height: 12,
              borderRadius: "50%",
              flexShrink: 0,
              background: live.active ? phaseColor(phase) : "var(--border)",
            }}
          />
          <span style={{ fontSize: "0.85em", color: live.active ? "var(--fg)" : "var(--fg-muted)" }}>
            {live.active
              ? agentLabel
                ? `${phaseLabel(phase)} · ${agentLabel}`
                : phaseLabel(phase)
              : unsupported
                ? "Live voice isn't supported in this browser."
                : "Press Start and allow microphone access to begin."}
          </span>
        </div>

        {/* Timeline */}
        <ol
          ref={timelineRef}
          aria-label="Conversation"
          style={{
            flex: 1,
            overflowY: "auto",
            listStyle: "none",
            margin: 0,
            padding: 4,
            display: "flex",
            flexDirection: "column",
            gap: 14,
          }}
        >
          {live.turns.length === 0 ? (
            <li
              style={{
                margin: "auto",
                textAlign: "center",
                color: "var(--fg-muted)",
                fontSize: "0.9em",
                maxWidth: 360,
              }}
            >
              {live.active
                ? "Say hello — your conversation will appear here."
                : "Start a conversation, then just speak. Your words and the assistant's reply stream in here turn by turn."}
            </li>
          ) : (
            live.turns.map((t) => <TurnBubble key={t.id} turn={t} />)
          )}
        </ol>

        <p style={{ margin: 0, fontSize: "0.72em", color: "var(--fg-muted)" }}>
          Tip: you can interrupt at any time — just start talking and the assistant
          yields the floor.
        </p>
      </div>

      <style>{`
        @keyframes vlPulse {
          0% { transform: scale(1); opacity: 1; }
          50% { transform: scale(1.5); opacity: 0.5; }
          100% { transform: scale(1); opacity: 1; }
        }
        .vl-pulse { animation: vlPulse 1.1s ease-in-out infinite; }
        @media (prefers-reduced-motion: reduce) { .vl-pulse { animation: none; } }
      `}</style>
    </div>
  );
}
