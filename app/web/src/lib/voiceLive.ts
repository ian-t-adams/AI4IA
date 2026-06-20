"use client";

// Phase 10 Voice Live: a real-time speech-to-speech client. The browser captures
// mic audio as 24 kHz mono PCM16 (via an AudioWorklet), base64-encodes it, and
// streams it over a WebSocket to the API's governed relay (`/api/voice/live`),
// which proxies to the upstream Azure realtime model. Inbound `response.audio.delta`
// frames are 24 kHz PCM16 and are scheduled back-to-back through the same
// AudioContext; barge-in stops playback the moment the user starts speaking.
//
// This module is feature-flagged and only ever exercised when the runtime config
// is enabled (see VoiceLiveProvider). With the flag off it is never imported into
// an active code path, so the default app behavior is unchanged.
import { useCallback, useEffect, useRef, useState } from "react";

import { getApiAccessToken, isEntraEnabled } from "./auth";

// Azure realtime speaks 24 kHz mono PCM16 in both directions.
export const PCM_SAMPLE_RATE = 24000;

// WebSocket subprotocol markers the relay understands (see routers/realtime.py).
const BEARER_SUBPROTOCOL = "ai4ia-bearer";
const DEV_SUBPROTOCOL = "ai4ia-dev";

// WebSocket subprotocol values must be RFC 7230 tokens (the browser's WebSocket
// constructor throws "The subprotocol '...' is invalid" otherwise). A bearer JWT
// is already token-safe (base64url segments joined by "."), but the dev identity
// can be an email like "dev@ai4ia.local" whose "@" is NOT a valid token char. So
// we base64url-encode the dev id (prefixed "b64u.") whenever it isn't already a
// bare token; the relay's decode_dev_credential() reverses it so the live session
// resolves to the same user as the HTTP path. Plain ids (e.g. "alice") pass
// through unencoded, keeping the wire human-readable and back-compatible.
const SUBPROTOCOL_TOKEN_RE = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/;
const DEV_CREDENTIAL_B64URL_PREFIX = "b64u.";

function toBase64Url(value: string): string {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

// Returns a dev identity that is always a valid WebSocket subprotocol token.
export function encodeDevCredential(devUser: string): string {
  return SUBPROTOCOL_TOKEN_RE.test(devUser)
    ? devUser
    : `${DEV_CREDENTIAL_B64URL_PREFIX}${toBase64Url(devUser)}`;
}

export interface VoiceLiveConfig {
  enabled: boolean;
  // wss:// URL of the relay endpoint (API external ingress + /api/voice/live).
  wsUrl: string;
  // Dev-auth fallback identity (ignored under Entra).
  devUser: string;
  // Whether the server advertises governed tool calling for live sessions (the
  // API's realtime_tools_enabled, surfaced to the browser). Only when true does
  // the panel offer the opt-in "Allow tools in voice" toggle. Default false.
  toolsAvailable: boolean;
}

export type VoiceLiveStatus = "idle" | "connecting" | "live" | "closing";

// The voices the gpt-realtime / gpt-realtime-mini models support. An unsupported
// value errors upstream, so the picker is constrained to this set and any stored
// value is validated against it before use. ``marin`` and ``cedar`` are the newest
// gpt-realtime voices.
export const REALTIME_VOICES = [
  "alloy",
  "ash",
  "ballad",
  "coral",
  "echo",
  "sage",
  "shimmer",
  "verse",
  "marin",
  "cedar",
] as const;

export type RealtimeVoice = (typeof REALTIME_VOICES)[number];

export const DEFAULT_VOICE: RealtimeVoice = "alloy";

export function isRealtimeVoice(value: string): value is RealtimeVoice {
  return (REALTIME_VOICES as readonly string[]).includes(value);
}

export type LiveTurnRole = "user" | "assistant";

// One entry in the live-conversation timeline. A *user* turn holds a whole
// transcribed utterance (or stays ``pending`` while the model transcribes it); an
// *assistant* turn accumulates the spoken reply's transcript across one exchange
// and carries an optional governed-tool label. ``streaming`` marks the assistant
// turn that is still producing output.
export interface LiveTurn {
  id: string;
  role: LiveTurnRole;
  text: string;
  streaming: boolean;
  pending: boolean;
  // Friendly label of a governed tool the assistant invoked this turn, or "".
  tool: string;
}

export interface VoiceLiveController {
  status: VoiceLiveStatus;
  active: boolean;
  supported: boolean;
  userTranscript: string;
  assistantTranscript: string;
  // The most recent tool the assistant invoked in this session (governed,
  // server-executed), or "" when none. Surfaced as a small activity hint.
  toolActivity: string;
  // Turn-by-turn timeline of the live conversation (user vs assistant), with
  // live-updating partial transcripts. Reset at the start of each session.
  turns: LiveTurn[];
  // The user is currently speaking (between server-VAD speech start/stop).
  listening: boolean;
  // The assistant is currently producing audio (speaking) for its reply.
  speaking: boolean;
  toggle: () => void;
  stop: () => void;
}

// --- PCM <-> base64 helpers (pure; exported for unit tests) ---

export function floatTo16BitPCM(input: Float32Array): Int16Array {
  const out = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return out;
}

export function int16ToFloat32(input: Int16Array): Float32Array {
  const out = new Float32Array(input.length);
  for (let i = 0; i < input.length; i++) {
    out[i] = input[i] / (input[i] < 0 ? 0x8000 : 0x7fff);
  }
  return out;
}

export function int16ToBase64(int16: Int16Array): string {
  const bytes = new Uint8Array(int16.buffer, int16.byteOffset, int16.byteLength);
  let binary = "";
  // Frames are small (~100 ms); a simple per-byte loop avoids spread-arg limits.
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

export function base64ToInt16(b64: string): Int16Array {
  const binary = atob(b64);
  const len = binary.length;
  const bytes = new Uint8Array(len);
  for (let i = 0; i < len; i++) bytes[i] = binary.charCodeAt(i);
  const sampleCount = Math.floor(len / 2);
  const out = new Int16Array(sampleCount);
  const view = new DataView(bytes.buffer);
  for (let i = 0; i < sampleCount; i++) out[i] = view.getInt16(i * 2, true);
  return out;
}

// AudioWorklet processor source (loaded via a Blob URL so it needs no separate
// public asset and stays bundled with this module). It accumulates ~100 ms of
// 24 kHz mono frames and posts each batch to the main thread, which keeps the
// outbound WebSocket message rate low (~10/s) instead of one per 128-sample render.
const CAPTURE_WORKLET_SRC = `
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._count = 0;
    this._target = 2400; // ~100 ms at 24 kHz
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (ch && ch.length) {
      this._chunks.push(ch.slice(0));
      this._count += ch.length;
      if (this._count >= this._target) {
        const merged = new Float32Array(this._count);
        let o = 0;
        for (const c of this._chunks) { merged.set(c, o); o += c.length; }
        this.port.postMessage(merged, [merged.buffer]);
        this._chunks = [];
        this._count = 0;
      }
    }
    return true;
  }
}
registerProcessor('ai4ia-capture', CaptureProcessor);
`;

function supportsVoiceLive(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.WebSocket !== "undefined" &&
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof window.AudioWorkletNode !== "undefined" &&
    (typeof window.AudioContext !== "undefined" ||
      typeof (window as unknown as { webkitAudioContext?: unknown })
        .webkitAudioContext !== "undefined")
  );
}

function makeAudioContext(): AudioContext {
  const Ctor =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext: typeof AudioContext })
      .webkitAudioContext;
  return new Ctor({ sampleRate: PCM_SAMPLE_RATE });
}

// The default server-side instructions; the relay leaves the conversation shape
// to the client, so the browser owns the session.update it sends on connect.
const DEFAULT_INSTRUCTIONS =
  "You are a helpful, concise voice assistant. Keep spoken replies brief and natural.";

// The default input-audio transcription model (Azure realtime supports whisper-1).
const DEFAULT_TRANSCRIPTION_MODEL = "whisper-1";

// The voice-activity-detection modes Azure realtime understands. ``server_vad``
// is the energy-threshold default; ``semantic_vad`` ends a turn on semantic
// completeness (no threshold/silence knobs).
export const VAD_TYPES = ["server_vad", "semantic_vad"] as const;
export type VadType = (typeof VAD_TYPES)[number];

// User-tunable live-session settings, surfaced in the panel's settings disclosure
// and threaded into the session.update. Every field DEFAULTS to the value the relay
// has always sent, so an untouched session is byte-for-byte identical to before.
// ``null``/empty fields are omitted from the payload entirely (the model applies
// its own default), which is how today's payload omits e.g. temperature.
export interface VoiceSessionSettings {
  // System instructions. Ignored by the relay when an agent persona is bound (the
  // agent's prompt is server-authoritative), as today.
  instructions: string;
  // Sampling temperature, or null to omit (model default — today's behavior).
  temperature: number | null;
  vadType: VadType;
  // server_vad energy threshold (0–1), or null to omit (model default).
  vadThreshold: number | null;
  // server_vad trailing-silence cutoff in ms, or null to omit (model default).
  vadSilenceMs: number | null;
  // Input-audio transcription model.
  transcriptionModel: string;
  // Optional transcription language hint (ISO-639-1, e.g. "en"), or "" to omit.
  language: string;
}

export const DEFAULT_VOICE_SETTINGS: VoiceSessionSettings = {
  instructions: DEFAULT_INSTRUCTIONS,
  temperature: null,
  vadType: "server_vad",
  vadThreshold: null,
  vadSilenceMs: null,
  transcriptionModel: DEFAULT_TRANSCRIPTION_MODEL,
  language: "",
};

export function isVadType(value: string): value is VadType {
  return (VAD_TYPES as readonly string[]).includes(value);
}

// The realtime-category models offered in the voice model picker. The voice panel
// (and its model picker) only ever deals with realtime models; chat/capability
// models are reached through their own surfaces. Pure + structural so it is unit
// testable without the full ModelEntry type.
export function realtimeModels<T extends { category: string }>(models: T[]): T[] {
  return models.filter((m) => m.category === "realtime");
}

// Builds the session.update frame the browser sends on connect. With the default
// settings this is byte-for-byte the original Phase 10 payload: optional fields
// (temperature, VAD threshold/silence, language) are only added when explicitly
// set, so the relay's transparent-pump behavior is preserved until a user opts in.
export function sessionUpdate(
  voice: string,
  settings: VoiceSessionSettings = DEFAULT_VOICE_SETTINGS,
): string {
  // Guard against a stale/invalid stored value reaching the upstream model.
  const selected = isRealtimeVoice(voice) ? voice : DEFAULT_VOICE;
  const turnDetection: Record<string, unknown> = { type: settings.vadType };
  // threshold / silence are server_vad knobs; semantic_vad ignores them, so only
  // attach them for server_vad (and only when explicitly set).
  if (settings.vadType === "server_vad") {
    if (settings.vadThreshold != null) turnDetection.threshold = settings.vadThreshold;
    if (settings.vadSilenceMs != null) {
      turnDetection.silence_duration_ms = settings.vadSilenceMs;
    }
  }
  const transcription: Record<string, unknown> = {
    model: settings.transcriptionModel || DEFAULT_TRANSCRIPTION_MODEL,
  };
  if (settings.language) transcription.language = settings.language;
  const session: Record<string, unknown> = {
    instructions: settings.instructions,
    voice: selected,
    input_audio_format: "pcm16",
    output_audio_format: "pcm16",
    turn_detection: turnDetection,
    input_audio_transcription: transcription,
  };
  if (settings.temperature != null) session.temperature = settings.temperature;
  return JSON.stringify({ type: "session.update", session });
}

// A readable label for a server-executed tool name (e.g. "get_current_time" ->
// "get current time") for the live-voice activity hint.
export function friendlyToolName(name: string): string {
  return name.replace(/[_-]+/g, " ").trim() || name;
}

// One prior text-chat turn used to seed a fresh live session with context.
export interface VoiceSeedTurn {
  role: "user" | "assistant";
  text: string;
}

// Bounds on how much prior text history we replay into a new live session: seed
// useful context without flooding the model or the seed payload.
const MAX_SEED_TURNS = 20;
const MAX_SEED_CHARS = 6000;

// Builds the conversation.item.create frames that seed a fresh live session with
// recent text-chat history so voice continues the SAME conversation. These pass
// through the relay verbatim (it only rewrites session.update). The newest turns
// are kept within the char budget, then emitted oldest-first to preserve order.
// Seeded items are passive context — they do not trigger a model response (only
// the user speaking does), so the session opens silently with memory of the chat.
function seedFrames(history: VoiceSeedTurn[]): string[] {
  const recent = history
    .filter((t) => t.text && t.text.trim())
    .slice(-MAX_SEED_TURNS);
  const selected: VoiceSeedTurn[] = [];
  let budget = MAX_SEED_CHARS;
  for (let i = recent.length - 1; i >= 0 && budget > 0; i--) {
    const text = recent[i].text.trim().slice(0, budget);
    selected.unshift({ role: recent[i].role, text });
    budget -= text.length;
  }
  return selected.map((t) =>
    JSON.stringify({
      type: "conversation.item.create",
      item: {
        type: "message",
        role: t.role,
        content: [
          { type: t.role === "user" ? "input_text" : "text", text: t.text },
        ],
      },
    }),
  );
}

// Builds the WebSocket auth subprotocols. Under Entra we pass a real bearer token;
// otherwise we carry the dev identity (honored by the relay only when dev auth is
// permitted). Returns null when Entra is on but no token can be acquired (the
// caller surfaces a sign-in error rather than opening an unauthenticated socket).
async function buildSubprotocols(
  config: VoiceLiveConfig,
): Promise<string[] | null> {
  if (isEntraEnabled()) {
    const token = await getApiAccessToken();
    if (!token) return null;
    return [BEARER_SUBPROTOCOL, token];
  }
  return [DEV_SUBPROTOCOL, encodeDevCredential(config.devUser || "dev")];
}

interface LiveSession {
  ws: WebSocket;
  ctx: AudioContext;
  stream: MediaStream;
  worklet: AudioWorkletNode;
  source: MediaStreamAudioSourceNode;
  scheduled: Set<AudioBufferSourceNode>;
  nextPlayTime: number;
}

// Real-time speech-to-speech controller. Owns the WS + mic capture + playback
// lifecycle and tears everything down on stop or unmount.
export function useVoiceLive(
  config: VoiceLiveConfig,
  model: string | null,
  voice: string,
  onError: (message: string) => void,
  agent: string | null = null,
  history: VoiceSeedTurn[] = [],
  settings: VoiceSessionSettings = DEFAULT_VOICE_SETTINGS,
  tools: boolean = false,
): VoiceLiveController {
  const [status, setStatus] = useState<VoiceLiveStatus>("idle");
  const [supported, setSupported] = useState(false);
  const [userTranscript, setUserTranscript] = useState("");
  const [assistantTranscript, setAssistantTranscript] = useState("");
  const [toolActivity, setToolActivity] = useState("");
  const [turns, setTurns] = useState<LiveTurn[]>([]);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);

  const sessionRef = useRef<LiveSession | null>(null);
  const startingRef = useRef(false);
  const mountedRef = useRef(true);

  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  // The voice is read at connect time (it locks for the session after the first
  // audio reply). A ref keeps the latest selection without re-creating ``start``
  // or restarting a live session when the picker changes.
  const voiceRef = useRef(voice);
  useEffect(() => {
    voiceRef.current = voice;
  }, [voice]);

  // Recent text-chat history to seed into the next live session, kept in a ref so
  // updates don't re-create ``start`` or restart a live session mid-conversation.
  const historyRef = useRef(history);
  useEffect(() => {
    historyRef.current = history;
  }, [history]);

  // Session settings + the per-session tools opt-in are read at connect time only,
  // so refs keep ``start`` stable while the user edits them in the panel.
  const settingsRef = useRef(settings);
  useEffect(() => {
    settingsRef.current = settings;
  }, [settings]);
  const toolsRef = useRef(tools);
  useEffect(() => {
    toolsRef.current = tools;
  }, [tools]);

  useEffect(() => {
    mountedRef.current = true;
    setSupported(supportsVoiceLive());
    return () => {
      mountedRef.current = false;
      teardown();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const teardown = useCallback(() => {
    const s = sessionRef.current;
    sessionRef.current = null;
    if (!s) return;
    try {
      for (const node of s.scheduled) {
        try {
          node.stop();
        } catch {
          /* already stopped */
        }
      }
      s.scheduled.clear();
    } catch {
      /* ignore */
    }
    try {
      s.worklet.port.onmessage = null;
      s.worklet.disconnect();
      s.source.disconnect();
    } catch {
      /* ignore */
    }
    for (const t of s.stream.getTracks()) t.stop();
    try {
      if (s.ws.readyState === WebSocket.OPEN || s.ws.readyState === WebSocket.CONNECTING) {
        s.ws.close(1000, "client closed");
      }
    } catch {
      /* ignore */
    }
    void s.ctx.close().catch(() => {});
  }, []);

  const start = useCallback(async () => {
    if (startingRef.current || sessionRef.current) return;
    if (!config.enabled || !config.wsUrl) {
      onErrorRef.current("Live voice isn't available.");
      return;
    }
    if (!supportsVoiceLive()) {
      onErrorRef.current("Live voice isn't supported in this browser.");
      return;
    }
    startingRef.current = true;
    setStatus("connecting");
    setUserTranscript("");
    setAssistantTranscript("");
    setToolActivity("");
    setTurns([]);
    setListening(false);
    setSpeaking(false);
    // Raw resources are acquired before the session is wired into sessionRef; if
    // init fails before that, teardown() can't see them (it early-returns on a
    // null sessionRef), so the catch path releases these directly.
    let pendingStream: MediaStream | null = null;
    let pendingCtx: AudioContext | null = null;
    try {
      const subprotocols = await buildSubprotocols(config);
      if (!subprotocols) {
        onErrorRef.current("Please sign in to use live voice.");
        setStatus("idle");
        return;
      }
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true },
      });
      pendingStream = stream;
      if (!mountedRef.current) {
        for (const t of stream.getTracks()) t.stop();
        return;
      }
      const ctx = makeAudioContext();
      pendingCtx = ctx;
      await ctx.resume();
      const blob = new Blob([CAPTURE_WORKLET_SRC], { type: "application/javascript" });
      const moduleUrl = URL.createObjectURL(blob);
      try {
        await ctx.audioWorklet.addModule(moduleUrl);
      } finally {
        URL.revokeObjectURL(moduleUrl);
      }
      const source = ctx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(ctx, "ai4ia-capture");

      // The relay resolves the realtime deployment and (when ?agent= is set) the
      // agent's server-authoritative persona + tool allowlist; the browser only
      // names them. An unknown/disabled agent falls back to the generic assistant.
      const params = new URLSearchParams();
      if (model) params.set("model", model);
      if (agent) params.set("agent", agent);
      // Per-session governed-tools opt-in. The relay also requires the server flag,
      // so this only matters when tools are advertised available.
      if (toolsRef.current) params.set("tools", "1");
      const qs = params.toString();
      const wsUrl = qs ? `${config.wsUrl}?${qs}` : config.wsUrl;
      const ws = new WebSocket(wsUrl, subprotocols);
      ws.binaryType = "arraybuffer";

      const session: LiveSession = {
        ws,
        ctx,
        stream,
        worklet,
        source,
        scheduled: new Set(),
        nextPlayTime: 0,
      };
      sessionRef.current = session;

      const enqueuePlayback = (b64: string) => {
        const int16 = base64ToInt16(b64);
        if (int16.length === 0) return;
        const float = int16ToFloat32(int16);
        const buffer = ctx.createBuffer(1, float.length, PCM_SAMPLE_RATE);
        buffer.copyToChannel(float, 0);
        const node = ctx.createBufferSource();
        node.buffer = buffer;
        node.connect(ctx.destination);
        const startAt = Math.max(ctx.currentTime, session.nextPlayTime);
        node.start(startAt);
        session.nextPlayTime = startAt + buffer.duration;
        session.scheduled.add(node);
        node.onended = () => session.scheduled.delete(node);
      };

      const bargeIn = () => {
        for (const node of session.scheduled) {
          try {
            node.stop();
          } catch {
            /* already stopped */
          }
        }
        session.scheduled.clear();
        session.nextPlayTime = 0;
      };

      // --- live timeline state (per session; closed over by the event handler) ---
      // The id of the user turn awaiting its transcription, and of the assistant
      // turn currently open (it spans one exchange: it accumulates the spoken
      // reply + any tool label until the user speaks again, so each exchange is a
      // single user bubble + single assistant bubble even when a tool call splits
      // the model's output into two upstream responses).
      let userTurnId: string | null = null;
      let assistantTurnId: string | null = null;
      let turnSeq = 0;
      const nextTurnId = () => `lt${++turnSeq}`;

      const pushTurn = (turn: LiveTurn) => {
        if (mountedRef.current) setTurns((prev) => [...prev, turn]);
      };
      const patchTurn = (id: string, patch: (t: LiveTurn) => LiveTurn) => {
        if (mountedRef.current) {
          setTurns((prev) => prev.map((t) => (t.id === id ? patch(t) : t)));
        }
      };
      const dropTurn = (id: string) => {
        if (mountedRef.current) setTurns((prev) => prev.filter((t) => t.id !== id));
      };
      // Get (or lazily open) the assistant turn for the current exchange.
      const ensureAssistantTurn = (): string => {
        if (assistantTurnId) return assistantTurnId;
        const id = nextTurnId();
        assistantTurnId = id;
        pushTurn({ id, role: "assistant", text: "", streaming: true, pending: false, tool: "" });
        return id;
      };

      const handleServerEvent = (ev: MessageEvent) => {
        if (typeof ev.data !== "string") return;
        let msg: Record<string, unknown>;
        try {
          msg = JSON.parse(ev.data) as Record<string, unknown>;
        } catch {
          return;
        }
        const type = typeof msg.type === "string" ? msg.type : "";
        switch (type) {
          case "response.audio.delta": {
            const delta = typeof msg.delta === "string" ? msg.delta : "";
            if (delta) {
              enqueuePlayback(delta);
              if (mountedRef.current) setSpeaking(true);
            }
            break;
          }
          case "response.audio_transcript.delta": {
            const delta = typeof msg.delta === "string" ? msg.delta : "";
            if (delta && mountedRef.current) {
              setAssistantTranscript((p) => p + delta);
              const id = ensureAssistantTurn();
              patchTurn(id, (t) => ({ ...t, text: t.text + delta, streaming: true }));
            }
            break;
          }
          case "response.function_call_arguments.done": {
            // The relay executes governed tools server-side; show a brief hint so
            // the user understands why the assistant paused / what grounded its
            // answer. The spoken reply still arrives as normal audio deltas.
            const name = typeof msg.name === "string" ? msg.name : "";
            if (name && mountedRef.current) {
              const label = friendlyToolName(name);
              setToolActivity(label);
              const id = ensureAssistantTurn();
              patchTurn(id, (t) => ({ ...t, tool: label }));
            }
            break;
          }
          case "conversation.item.input_audio_transcription.completed": {
            const t = typeof msg.transcript === "string" ? msg.transcript : "";
            const trimmed = t.trim();
            if (mountedRef.current) {
              if (trimmed) setUserTranscript((p) => (p ? `${p} ` : "") + trimmed);
              // Resolve the pending user bubble created on speech start, or push a
              // completed one if none is open. Empty transcripts drop the bubble.
              if (userTurnId) {
                const id = userTurnId;
                if (trimmed) patchTurn(id, (u) => ({ ...u, text: trimmed, pending: false }));
                else dropTurn(id);
              } else if (trimmed) {
                pushTurn({
                  id: nextTurnId(),
                  role: "user",
                  text: trimmed,
                  streaming: false,
                  pending: false,
                  tool: "",
                });
              }
            }
            userTurnId = null;
            break;
          }
          case "response.created": {
            if (mountedRef.current) setAssistantTranscript("");
            break;
          }
          case "response.done": {
            // The model finished this response. Mark the open assistant turn idle
            // and stop the speaking indicator; the turn stays open (a tool call can
            // chain a second response into the same bubble) until the user speaks.
            if (mountedRef.current) {
              setSpeaking(false);
              if (assistantTurnId) patchTurn(assistantTurnId, (t) => ({ ...t, streaming: false }));
            }
            break;
          }
          case "input_audio_buffer.speech_started": {
            bargeIn();
            // A new user turn supersedes the last tool hint, closes the assistant
            // turn, and stops playback/indicators.
            if (mountedRef.current) {
              setToolActivity("");
              setListening(true);
              setSpeaking(false);
              if (assistantTurnId) {
                patchTurn(assistantTurnId, (t) => ({ ...t, streaming: false }));
              }
            }
            assistantTurnId = null;
            userTurnId = nextTurnId();
            pushTurn({
              id: userTurnId,
              role: "user",
              text: "",
              streaming: false,
              pending: true,
              tool: "",
            });
            break;
          }
          case "input_audio_buffer.speech_stopped": {
            if (mountedRef.current) setListening(false);
            break;
          }
          case "error": {
            const err = msg.error as { message?: string } | undefined;
            onErrorRef.current(err?.message || "Live voice reported an error.");
            break;
          }
          default:
            break;
        }
      };

      worklet.port.onmessage = (ev: MessageEvent) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        const samples = ev.data as Float32Array;
        const pcm = floatTo16BitPCM(samples);
        ws.send(
          JSON.stringify({
            type: "input_audio_buffer.append",
            audio: int16ToBase64(pcm),
          }),
        );
      };

      ws.onopen = () => {
        ws.send(sessionUpdate(voiceRef.current, settingsRef.current));
        // Seed the session with recent text history so voice continues the same
        // conversation. Sent after session.update; passes through the relay as-is.
        for (const frame of seedFrames(historyRef.current)) ws.send(frame);
        // Connect the capture graph. The worklet emits silence to the
        // destination (it only forwards mic frames via its port), so wiring it to
        // the destination keeps it in the active render graph without echo.
        source.connect(worklet);
        worklet.connect(ctx.destination);
        if (mountedRef.current) setStatus("live");
      };
      ws.onmessage = handleServerEvent;
      ws.onerror = () => {
        onErrorRef.current("Live voice connection error.");
      };
      ws.onclose = () => {
        if (sessionRef.current === session) {
          teardown();
          if (mountedRef.current) {
            setListening(false);
            setSpeaking(false);
            setStatus("idle");
          }
        }
      };
    } catch (e) {
      onErrorRef.current((e as Error).message || "Couldn't start live voice.");
      // If the session was never wired, teardown() early-returns, so release the
      // raw mic/AudioContext here — otherwise the mic track stays live (the OS
      // mic indicator stays lit) and the AudioContext leaks.
      if (!sessionRef.current) {
        if (pendingStream) for (const t of pendingStream.getTracks()) t.stop();
        if (pendingCtx) void pendingCtx.close().catch(() => {});
      }
      teardown();
      if (mountedRef.current) setStatus("idle");
    } finally {
      startingRef.current = false;
    }
  }, [config, model, agent, teardown]);

  const stop = useCallback(() => {
    setStatus("closing");
    teardown();
    if (mountedRef.current) {
      setListening(false);
      setSpeaking(false);
      setStatus("idle");
    }
  }, [teardown]);

  const toggle = useCallback(() => {
    if (sessionRef.current || status !== "idle") stop();
    else void start();
  }, [status, start, stop]);

  return {
    status,
    active: status === "live" || status === "connecting",
    supported,
    userTranscript,
    assistantTranscript,
    toolActivity,
    turns,
    listening,
    speaking,
    toggle,
    stop,
  };
}
