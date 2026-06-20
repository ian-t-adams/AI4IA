"use client";

// Phase 10 Voice Live — the dedicated live-conversation surface. A modal (mirroring
// the Imagery Studio pattern) that turns the governed `/api/voice/live` relay into a
// genuinely usable voice assistant: a turn-by-turn timeline with live partial
// transcripts, a clear listening / thinking / speaking indicator (barge-in is wired
// in the engine and reflected here), governed-tool badges, and graceful
// connecting / live / ending / error states. It reuses the `useVoiceLive` audio
// engine unchanged — this component only renders its structured state.
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import type { AgentSummary, ModelEntry, VoiceTurnInput } from "@/lib/types";
import {
  useVoiceLive,
  DEFAULT_VOICE,
  DEFAULT_VOICE_SETTINGS,
  REALTIME_VOICES,
  VAD_TYPES,
  isRealtimeVoice,
  isVadType,
  realtimeModels,
  type LiveTurn,
  type VadType,
  type VoiceLiveConfig,
  type VoiceSeedTurn,
  type VoiceSessionSettings,
} from "@/lib/voiceLive";

// Where the chosen live-voice persona / agent / model / tools opt-in are remembered
// across reloads (shared with the prior Composer affordance so a returning user
// keeps their selection).
const VOICE_STORAGE_KEY = "ai4ia.voiceLive.voice";
const AGENT_STORAGE_KEY = "ai4ia.voiceLive.agent";
const MODEL_STORAGE_KEY = "ai4ia.voiceLive.model";
const TOOLS_STORAGE_KEY = "ai4ia.voiceLive.tools";

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

// Parse a numeric settings input: blank => null (omit from payload, model default).
function numOrNull(raw: string): number | null {
  const t = raw.trim();
  if (t === "") return null;
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}

const fieldStyle: CSSProperties = {
  minHeight: 36,
  padding: "0 8px",
  borderRadius: 8,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  fontSize: "0.95em",
};

export function VoiceLivePanel({
  config,
  models,
  agents,
  onClose,
  onError,
  history = [],
  onConversation,
}: {
  config: VoiceLiveConfig;
  // The realtime-category models the user can pick among (filtered upstream from
  // the same /api/models the chat picker uses). The panel defensively re-filters.
  models: ModelEntry[];
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
  // Chosen realtime model persists across reloads. Empty => fall back to the first
  // available realtime model (which is what the host auto-resolved before this
  // picker existed, so the default is unchanged).
  const [liveModel, setLiveModel] = useState<string>("");
  // Per-session opt-in to governed tool calls in voice. Only offered when the API
  // advertises tools (config.toolsAvailable); default OFF so behavior is unchanged.
  const [toolsAllowed, setToolsAllowed] = useState<boolean>(false);
  // Optional session-setting overrides. Defaults equal today's hardcoded values, so
  // an untouched panel produces a byte-for-byte identical session.update.
  const [settings, setSettings] = useState<VoiceSessionSettings>(DEFAULT_VOICE_SETTINGS);
  const [showSettings, setShowSettings] = useState<boolean>(false);

  // Realtime-only models for the picker (defensive re-filter even though the host
  // already passes a filtered list).
  const availableModels = useMemo(() => realtimeModels(models), [models]);

  useEffect(() => {
    try {
      const v = window.localStorage.getItem(VOICE_STORAGE_KEY);
      if (v && isRealtimeVoice(v)) setLiveVoice(v);
      const a = window.localStorage.getItem(AGENT_STORAGE_KEY);
      if (a) setLiveAgent(a);
      const m = window.localStorage.getItem(MODEL_STORAGE_KEY);
      if (m) setLiveModel(m);
      const t = window.localStorage.getItem(TOOLS_STORAGE_KEY);
      if (t === "1") setToolsAllowed(true);
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

  // Drop a remembered model that is no longer a valid realtime model.
  useEffect(() => {
    if (!liveModel) return;
    if (availableModels.length > 0 && !availableModels.some((m) => m.id === liveModel)) {
      setLiveModel("");
    }
  }, [availableModels, liveModel]);

  // The id actually sent to the host: the chosen model, or the first available as a
  // fallback (matches the previous auto-resolved behavior).
  const effectiveModel = liveModel || availableModels[0]?.id || null;

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
  const onPickModel = (value: string) => {
    setLiveModel(value);
    try {
      if (value) window.localStorage.setItem(MODEL_STORAGE_KEY, value);
      else window.localStorage.removeItem(MODEL_STORAGE_KEY);
    } catch {
      /* best effort */
    }
  };
  const onToggleTools = (value: boolean) => {
    setToolsAllowed(value);
    try {
      if (value) window.localStorage.setItem(TOOLS_STORAGE_KEY, "1");
      else window.localStorage.removeItem(TOOLS_STORAGE_KEY);
    } catch {
      /* best effort */
    }
  };

  // Tools are only effective when the server advertises them AND the user opted in.
  const toolsRequested = config.toolsAvailable && toolsAllowed;

  const live = useVoiceLive(
    config,
    effectiveModel,
    liveVoice,
    (msg) => onError?.(msg),
    liveAgent || null,
    history,
    settings,
    toolsRequested,
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
          {availableModels.length > 0 && (
            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}>
              <span style={{ color: "var(--fg-muted)" }}>Model</span>
              <select
                aria-label="Live voice model"
                value={effectiveModel ?? ""}
                disabled={live.active}
                onChange={(e) => onPickModel(e.target.value)}
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
                {availableModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.displayName || m.id}
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

        {/* Settings disclosure + per-session tools opt-in. Everything here defaults
            to today's behavior; an untouched panel sends the same session.update. */}
        <div style={{ display: "flex", flexWrap: "wrap", gap: 12, alignItems: "center" }}>
          <button
            type="button"
            onClick={() => setShowSettings((s) => !s)}
            aria-expanded={showSettings}
            style={{
              border: "1px solid var(--border)",
              background: "var(--bg)",
              color: "var(--fg-muted)",
              borderRadius: 8,
              padding: "6px 12px",
              fontSize: "0.78em",
              cursor: "pointer",
            }}
          >
            {showSettings ? "▾" : "▸"} Session settings
          </button>
          {config.toolsAvailable && (
            <label
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                fontSize: "0.78em",
                color: "var(--fg-muted)",
                cursor: live.active ? "not-allowed" : "pointer",
              }}
            >
              <input
                type="checkbox"
                aria-label="Allow tools in voice"
                checked={toolsAllowed}
                disabled={live.active}
                onChange={(e) => onToggleTools(e.target.checked)}
              />
              Allow tools in voice
            </label>
          )}
        </div>

        {showSettings && (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
              gap: 12,
              padding: 14,
              borderRadius: 10,
              border: "1px solid var(--border)",
              background: "var(--bg)",
            }}
          >
            <label
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 4,
                fontSize: "0.75em",
                gridColumn: "1 / -1",
              }}
            >
              <span style={{ color: "var(--fg-muted)" }}>
                Instructions {liveAgent ? "(ignored while an agent is selected)" : ""}
              </span>
              <textarea
                aria-label="Voice instructions"
                value={settings.instructions}
                disabled={live.active}
                rows={2}
                onChange={(e) =>
                  setSettings((s) => ({ ...s, instructions: e.target.value }))
                }
                style={{ ...fieldStyle, minHeight: 52, padding: 8, resize: "vertical" }}
              />
            </label>

            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}>
              <span style={{ color: "var(--fg-muted)" }}>Temperature (blank = default)</span>
              <input
                type="number"
                aria-label="Voice temperature"
                inputMode="decimal"
                step={0.1}
                min={0}
                max={2}
                value={settings.temperature ?? ""}
                disabled={live.active}
                onChange={(e) =>
                  setSettings((s) => ({ ...s, temperature: numOrNull(e.target.value) }))
                }
                style={fieldStyle}
              />
            </label>

            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}>
              <span style={{ color: "var(--fg-muted)" }}>Turn detection</span>
              <select
                aria-label="Voice turn detection"
                value={settings.vadType}
                disabled={live.active}
                onChange={(e) =>
                  setSettings((s) => ({
                    ...s,
                    vadType: isVadType(e.target.value) ? e.target.value : s.vadType,
                  }))
                }
                style={{ ...fieldStyle, cursor: live.active ? "not-allowed" : "pointer" }}
              >
                {VAD_TYPES.map((t) => (
                  <option key={t} value={t}>
                    {t === "server_vad" ? "Server VAD" : "Semantic VAD"}
                  </option>
                ))}
              </select>
            </label>

            {settings.vadType === "server_vad" && (
              <>
                <label
                  style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}
                >
                  <span style={{ color: "var(--fg-muted)" }}>VAD threshold (blank = default)</span>
                  <input
                    type="number"
                    aria-label="Voice VAD threshold"
                    inputMode="decimal"
                    step={0.05}
                    min={0}
                    max={1}
                    value={settings.vadThreshold ?? ""}
                    disabled={live.active}
                    onChange={(e) =>
                      setSettings((s) => ({ ...s, vadThreshold: numOrNull(e.target.value) }))
                    }
                    style={fieldStyle}
                  />
                </label>
                <label
                  style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}
                >
                  <span style={{ color: "var(--fg-muted)" }}>Silence ms (blank = default)</span>
                  <input
                    type="number"
                    aria-label="Voice VAD silence duration"
                    inputMode="numeric"
                    step={50}
                    min={0}
                    value={settings.vadSilenceMs ?? ""}
                    disabled={live.active}
                    onChange={(e) =>
                      setSettings((s) => ({ ...s, vadSilenceMs: numOrNull(e.target.value) }))
                    }
                    style={fieldStyle}
                  />
                </label>
              </>
            )}

            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}>
              <span style={{ color: "var(--fg-muted)" }}>Transcription model</span>
              <input
                type="text"
                aria-label="Voice transcription model"
                value={settings.transcriptionModel}
                disabled={live.active}
                onChange={(e) =>
                  setSettings((s) => ({ ...s, transcriptionModel: e.target.value }))
                }
                style={fieldStyle}
              />
            </label>

            <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: "0.75em" }}>
              <span style={{ color: "var(--fg-muted)" }}>Language hint (blank = auto)</span>
              <input
                type="text"
                aria-label="Voice language hint"
                placeholder="e.g. en"
                value={settings.language}
                disabled={live.active}
                onChange={(e) => setSettings((s) => ({ ...s, language: e.target.value }))}
                style={fieldStyle}
              />
            </label>

            <div style={{ gridColumn: "1 / -1", display: "flex", justifyContent: "flex-end" }}>
              <button
                type="button"
                onClick={() => setSettings(DEFAULT_VOICE_SETTINGS)}
                disabled={live.active}
                style={{
                  border: "1px solid var(--border)",
                  background: "var(--bg-elevated)",
                  color: "var(--fg-muted)",
                  borderRadius: 8,
                  padding: "6px 12px",
                  fontSize: "0.75em",
                  cursor: live.active ? "not-allowed" : "pointer",
                }}
              >
                Reset to defaults
              </button>
            </div>
          </div>
        )}

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
