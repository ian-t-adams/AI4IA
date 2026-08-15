"use client";

// Voice Live: a real-time speech-to-speech client. The browser captures
// mic audio as 24 kHz mono PCM16 (via an AudioWorklet), base64-encodes it, and
// streams it over a WebSocket to the API's governed relay (`/api/voice/live`),
// which proxies to the upstream Azure realtime model. Inbound `response.audio.delta`
// frames are 24 kHz PCM16 and are scheduled back-to-back through the same
// AudioContext; barge-in stops playback the moment the user starts speaking.
//
// This module is feature-flagged and only ever exercised when the runtime config
// is enabled (see VoiceLiveProvider). With the flag off it is never imported into
// an active code path, so the default app behavior is unchanged.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { getApiAccessToken, isEntraEnabled } from "./auth";
import { reportClientEvent } from "./clientTelemetry";
import {
  DEFAULT_VOICE_PROVIDER_ID,
  voiceProviderCatalog,
  type VoiceProvider,
  type VoiceProviderId,
} from "./data/voice_provider_catalog";

// Azure realtime speaks 24 kHz mono PCM16 in both directions.
export const PCM_SAMPLE_RATE = 24000;
// Hold the first chunk (and the first chunk after an underrun) briefly so normal
// network jitter does not become an audible gap between otherwise contiguous
// response.audio.delta frames.
export const PLAYBACK_REBUFFER_SECONDS = 0.12;

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

export type { VoiceProvider, VoiceProviderId };
export const DEFAULT_VOICE_PROVIDER = DEFAULT_VOICE_PROVIDER_ID;
export type SpeechVoiceProvider = Extract<VoiceProvider, { id: "speech_voice_live" }>;
export type SpeechManagedModel = SpeechVoiceProvider["managedModels"][number];

const SPEECH_PROVIDER = voiceProviderCatalog.providers.find(
  (provider): provider is SpeechVoiceProvider => provider.id === "speech_voice_live",
);

export interface VoiceLiveProviderCatalogResponse {
  defaultProviderId: VoiceProviderId;
  enabledProviderIds: VoiceProviderId[];
  providers: VoiceProvider[];
}

export interface AuthorizedVoiceProviders {
  defaultProviderId: VoiceProviderId | null;
  providers: VoiceProvider[];
}

export function resolveAuthorizedVoiceProviders(
  config: VoiceLiveProviderCatalogResponse | null,
): AuthorizedVoiceProviders {
  if (!config) return { defaultProviderId: null, providers: [] };
  const enabled = new Set(config.enabledProviderIds);
  const providers = config.providers.filter((provider) => enabled.has(provider.id));
  const defaultProviderId = providers.some(
    (provider) => provider.id === config.defaultProviderId,
  )
    ? config.defaultProviderId
    : null;
  return { defaultProviderId, providers };
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

export const DEFAULT_SPEECH_VOICE = "en-US-Ava:DragonHDLatestNeural";
export const DEFAULT_SPEECH_LOCALE = "en-US";
export const DEFAULT_SPEECH_MODEL_ID =
  SPEECH_PROVIDER?.defaultManagedModelId ?? "gpt-realtime";
export const DEFAULT_SPEECH_TURN_DETECTION = "azure_semantic_vad";
export const DEFAULT_SPEECH_NOISE_SUPPRESSION = "azure_deep_noise_suppression";
export const DEFAULT_SPEECH_ECHO_CANCELLATION = "server_echo_cancellation";

export interface SpeechVoiceLiveSettings {
  temperature: number | null;
  voice: string;
  locale: string;
  turnDetection: "azure_semantic_vad" | "azure_semantic_vad_multilingual";
  noiseSuppression: "azure_deep_noise_suppression";
  echoCancellation: "server_echo_cancellation";
  interruptResponse: boolean;
  autoTruncate: boolean;
}

export const DEFAULT_SPEECH_VOICE_LIVE_SETTINGS: SpeechVoiceLiveSettings = {
  temperature: null,
  voice: DEFAULT_SPEECH_VOICE,
  locale: DEFAULT_SPEECH_LOCALE,
  turnDetection: DEFAULT_SPEECH_TURN_DETECTION,
  noiseSuppression: DEFAULT_SPEECH_NOISE_SUPPRESSION,
  echoCancellation: DEFAULT_SPEECH_ECHO_CANCELLATION,
  interruptResponse: true,
  autoTruncate: false,
};

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
  createdAt?: string;
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
  start: () => void;
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

export function int16ToFloat32(input: Int16Array): Float32Array<ArrayBuffer> {
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

// Same-origin static module so the production `script-src 'self'` CSP permits the
// AudioWorklet without adding executable `blob:` URLs.
export const CAPTURE_WORKLET_PATH = "/ai4ia-capture-worklet.js";

export function supportsVoiceLive(): boolean {
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

export function microphoneConstraints(
  providerId: VoiceProviderId,
): MediaTrackConstraints {
  if (providerId === "speech_voice_live") {
    // Speech Voice Live applies server-side deep noise suppression and echo
    // cancellation. Running browser DSP first produces the robotic/pumping
    // artifacts associated with two independent processors in series.
    return {
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    };
  }
  return {
    channelCount: 1,
    echoCancellation: true,
    noiseSuppression: true,
  };
}

function subscribeVoiceSupport(): () => void {
  return () => {};
}

function makeAudioContext(): AudioContext {
  const Ctor =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext: typeof AudioContext })
      .webkitAudioContext;
  return new Ctor({ sampleRate: PCM_SAMPLE_RATE });
}

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
  temperature: null,
  vadType: "server_vad",
  vadThreshold: null,
  vadSilenceMs: null,
  transcriptionModel: DEFAULT_TRANSCRIPTION_MODEL,
  language: "",
};

export function isSpeechVoiceProvider(
  provider: VoiceProvider | undefined,
): provider is SpeechVoiceProvider {
  return provider?.id === "speech_voice_live";
}

export function resolveSpeechManagedModel(
  modelId: string | null | undefined,
  provider: SpeechVoiceProvider | undefined = SPEECH_PROVIDER,
): SpeechManagedModel | undefined {
  if (!provider) return undefined;
  return (
    provider.managedModels.find((model) => model.id === modelId) ??
    provider.managedModels.find((model) => model.id === provider.defaultManagedModelId) ??
    provider.managedModels[0]
  );
}

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
// settings this is byte-for-byte the original payload: optional fields
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
    voice: selected,
    input_audio_format: "pcm16",
    output_audio_format: "pcm16",
    turn_detection: turnDetection,
    input_audio_transcription: transcription,
  };
  if (settings.temperature != null) session.temperature = settings.temperature;
  return JSON.stringify({ type: "session.update", session });
}

export function speechSessionUpdate(
  modelId: string | null | undefined,
  settings: SpeechVoiceLiveSettings = DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
): string {
  const provider = SPEECH_PROVIDER;
  const managedModel = resolveSpeechManagedModel(modelId, provider);
  const allowedVoices: readonly string[] =
    provider?.capabilities.voices.options ?? [DEFAULT_SPEECH_VOICE];
  const allowedLocales: readonly string[] =
    provider?.capabilities.locale?.options ?? [DEFAULT_SPEECH_LOCALE];
  const allowedTurnDetection =
    (provider?.capabilities.turnDetection.options ?? [
      DEFAULT_SPEECH_TURN_DETECTION,
      "azure_semantic_vad_multilingual",
    ]) as readonly string[];
  const allowedNoiseSuppression =
    (provider?.capabilities.noiseSuppression?.options ?? [
      DEFAULT_SPEECH_NOISE_SUPPRESSION,
    ]) as readonly string[];
  const allowedEchoCancellation =
    (provider?.capabilities.echoCancellation?.options ?? [
      DEFAULT_SPEECH_ECHO_CANCELLATION,
    ]) as readonly string[];
  const voice =
    typeof settings.voice === "string" && allowedVoices.includes(settings.voice)
      ? settings.voice
      : allowedVoices[0] ?? DEFAULT_SPEECH_VOICE;
  const locale =
    typeof settings.locale === "string" && allowedLocales.includes(settings.locale)
      ? settings.locale
      : allowedLocales[0] ?? DEFAULT_SPEECH_LOCALE;
  const turnDetection =
    typeof settings.turnDetection === "string" &&
    allowedTurnDetection.includes(settings.turnDetection)
      ? settings.turnDetection
      : allowedTurnDetection[0] ?? DEFAULT_SPEECH_TURN_DETECTION;
  const noiseSuppression =
    typeof settings.noiseSuppression === "string" &&
    allowedNoiseSuppression.includes(settings.noiseSuppression)
      ? settings.noiseSuppression
      : allowedNoiseSuppression[0] ?? DEFAULT_SPEECH_NOISE_SUPPRESSION;
  const echoCancellation =
    typeof settings.echoCancellation === "string" &&
    allowedEchoCancellation.includes(settings.echoCancellation)
      ? settings.echoCancellation
      : allowedEchoCancellation[0] ?? DEFAULT_SPEECH_ECHO_CANCELLATION;
  const session: Record<string, unknown> = {
    voice: {
      type: provider?.capabilities.voices.kind ?? "azure-standard",
      name: voice,
      locale,
    },
    input_audio_transcription: {
      model:
        managedModel?.inputTranscription.model ??
        (managedModel?.profile === "azure_speech_chain"
          ? "azure-speech"
          : "gpt-4o-transcribe"),
      language: locale,
    },
    turn_detection: {
      type: turnDetection,
      interrupt_response: settings.interruptResponse,
      auto_truncate: settings.autoTruncate,
    },
    input_audio_noise_reduction: { type: noiseSuppression },
    input_audio_echo_cancellation: { type: echoCancellation },
  };
  if (typeof settings.temperature === "number" && Number.isFinite(settings.temperature)) {
    session.temperature = Math.min(2, Math.max(0, settings.temperature));
  }
  return JSON.stringify({ type: "session.update", session });
}

export function buildVoiceLiveWebSocketUrl(
  baseUrl: string,
  input: {
    providerId: VoiceProviderId;
    model?: string | null;
    region?: string | null;
    sessionId?: string | null;
    agent?: string | null;
    tools?: boolean;
  },
): string {
  const params = new URLSearchParams();
  params.set("provider", input.providerId);
  if (input.sessionId) params.set("session", input.sessionId);
  if (input.providerId === "azure_openai") {
    if (input.model) params.set("model", input.model);
    if (input.region) params.set("region", input.region);
  } else if (input.providerId === "speech_voice_live" && input.model) {
    params.set("model", input.model);
  }
  if (input.agent) params.set("agent", input.agent);
  if (input.tools) params.set("tools", "1");
  const qs = params.toString();
  return qs ? `${baseUrl}?${qs}` : baseUrl;
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
export function seedFrames(history: VoiceSeedTurn[]): string[] {
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

export function buildInitialVoiceFrames(input: {
  providerId: VoiceProviderId;
  model?: string | null;
  voice: string;
  history?: VoiceSeedTurn[];
  settings?: VoiceSessionSettings;
  speechSettings?: SpeechVoiceLiveSettings;
}): string[] {
  const sessionFrame =
    input.providerId === "speech_voice_live"
      ? speechSessionUpdate(input.model, input.speechSettings)
      : sessionUpdate(input.voice, input.settings);
  return [sessionFrame, ...seedFrames(input.history ?? [])];
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
  // Set once ws.onopen fires. Distinguishes a handshake failure (the browser
  // can't surface the HTTP status of a rejected upgrade, e.g. a 503 from the
  // gateway) from a drop of an already-live session, so onerror/onclose can
  // report the right message.
  opened: boolean;
  cleaned: boolean;
  errorReported: boolean;
  protocolError: SafeProtocolError | null;
  // Bound to every captured track's "ended" event so cleanup can remove the
  // listener deterministically (see onTrackEnded wiring in start()).
  onTrackEnded: (() => void) | null;
  // Same deterministic-removal pattern as onTrackEnded, but for "mute" (see
  // MIC_TRACK_MUTED_MESSAGE for why this needs its own listener).
  onTrackMuted: (() => void) | null;
  // Bounded grace-period timer started when ctx.onstatechange observes the
  // AudioContext go "suspended" mid-session (see AUDIO_CONTEXT_SUSPENDED_MESSAGE).
  // Tracked on the session so cleanupSession can clear it deterministically.
  suspendRecoveryTimer: ReturnType<typeof setTimeout> | null;
}

// The message shown when the WebSocket fails or closes before ever reaching
// ws.onopen. A rejected upgrade handshake (e.g. the gateway or upstream
// realtime service returning a non-101 status) is invisible to the browser as
// anything but a bare error/close — there is no HTTP status to read — so this
// is the most specific, actionable message that can be shown.
const GATEWAY_UNAVAILABLE_MESSAGE =
  "Voice gateway or realtime service is unavailable. Try again.";
const LIVE_CONNECTION_ERROR_MESSAGE = "Live voice connection error.";
// The browser/OS can kill the mic track out from under an otherwise-healthy
// WebSocket (permission revoked from the address bar mid-call, input device
// unplugged/disconnected, another app taking exclusive access, a laptop lid
// close). Nothing else observes this — the socket stays open and "live" — so
// without an explicit `ended` listener the session silently stops hearing the
// user with no error at all. This is surfaced through the same finishSession
// path as a protocol error so it gets one consistent teardown + message.
const MIC_TRACK_ENDED_MESSAGE =
  "Microphone stopped providing audio (permission revoked or device disconnected). Reconnect to continue.";
// `track.muted` is a *different* failure mode than "ended": the track stays
// `readyState === "live"` (so `onended` never fires) while the underlying
// source stops delivering samples -- e.g. an OS-level privacy mute toggle, a
// hardware conflict with another app grabbing the mic, or a Bluetooth/OS
// audio-routing hiccup. Unlike "ended", `muted` doesn't consistently signal a
// *permanent* loss (browsers can unmute on their own), but silently staying
// "live" with a muted track is exactly the "voice no longer hears me" failure
// mode this reconnect message exists to make explicit rather than silent.
const MIC_TRACK_MUTED_MESSAGE =
  "Microphone stopped receiving audio (it may have been muted by your system or another app). Reconnect to continue.";
// Chrome (since v66) and Safari can and do suspend an active AudioContext --
// even one with a live MediaStreamTrack source feeding an AudioWorkletNode --
// when a tab is backgrounded, to save power; this is not reliably prevented
// just because audio capture is in progress. Suspension silently stops the
// capture worklet with no other observable signal (the mic track stays
// "live" and unmuted, the socket stays open) -- another way "voice no longer
// hears them" can happen with everything else looking healthy. A brief grace
// period lets a quick resume() (e.g. the user glances right back at the tab)
// self-heal silently; only a suspension that outlasts the grace period is
// treated as the same explicit, fatal failure as a dead/muted mic.
const AUDIO_CONTEXT_SUSPENDED_MESSAGE =
  "Live voice paused because the browser suspended audio processing (often from backgrounding the tab). Reconnect to continue.";
const AUDIO_CONTEXT_RESUME_GRACE_MS = 4000;

interface PendingLiveSession {
  ctx: AudioContext | null;
  stream: MediaStream | null;
  cleaned: boolean;
}

const MAX_SAFE_ERROR_CHARS = 512;
const CONTROL_CHARS_RE = /[\u0000-\u001f\u007f-\u009f]/g;
const JWT_RE = /\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b/g;
const BEARER_RE = /\bBearer\s+[A-Za-z0-9._~+/-]+=*/gi;
const SECRET_QUERY_RE =
  /([?&](?:api[_-]?key|access[_-]?token|token|sig)=)[^&#\s]+/gi;
const SECRET_VALUE_RE =
  /(\b(?:api[_-]?key|access[_-]?token|token|authorization)\b\s*[:=]\s*)("[^"]*"|'[^']*'|[^\s,;]+)/gi;

export function sanitizeVoiceErrorValue(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const safe = value
    .replace(CONTROL_CHARS_RE, " ")
    .replace(BEARER_RE, "Bearer [REDACTED]")
    .replace(JWT_RE, "[REDACTED]")
    .replace(SECRET_QUERY_RE, "$1[REDACTED]")
    .replace(SECRET_VALUE_RE, "$1[REDACTED]")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_SAFE_ERROR_CHARS);
  return safe || null;
}

export interface SafeProtocolError {
  type: string | null;
  code: string | null;
  param: string | null;
  eventId: string | null;
  message: string;
}

export function parseVoiceProtocolError(value: unknown): SafeProtocolError {
  const error =
    value && typeof value === "object" ? (value as Record<string, unknown>) : {};
  const metadataValue = (field: unknown) =>
    sanitizeVoiceErrorValue(field)?.slice(0, 96) ?? null;
  return {
    type: metadataValue(error.type),
    code: metadataValue(error.code),
    param: metadataValue(error.param),
    eventId: metadataValue(error.event_id),
    message:
      sanitizeVoiceErrorValue(error.message) ?? "Live voice reported an error.",
  };
}

export function formatVoiceProtocolError(error: SafeProtocolError): string {
  const metadata = [
    error.type ? `type: ${error.type}` : null,
    error.code ? `code: ${error.code}` : null,
    error.param ? `param: ${error.param}` : null,
    error.eventId ? `event_id: ${error.eventId}` : null,
  ].filter(Boolean);
  const suffix = metadata.length ? ` (${metadata.join("; ")})` : "";
  return `${error.message.slice(0, MAX_SAFE_ERROR_CHARS - suffix.length)}${suffix}`;
}

export function formatVoiceCloseError(
  opened: boolean,
  event?: Pick<CloseEvent, "code" | "reason"> | null,
): string {
  const base = opened ? LIVE_CONNECTION_ERROR_MESSAGE : GATEWAY_UNAVAILABLE_MESSAGE;
  const reason = sanitizeVoiceErrorValue(event?.reason);
  const details = [
    typeof event?.code === "number" && event.code > 0 ? `code: ${event.code}` : null,
    reason ? `reason: ${reason}` : null,
  ].filter(Boolean);
  return `${base}${details.length ? ` (${details.join("; ")})` : ""}`.slice(
    0,
    MAX_SAFE_ERROR_CHARS,
  );
}

// Real-time speech-to-speech controller. Owns the WS + mic capture + playback
// lifecycle and tears everything down on stop or unmount.
export function useVoiceLive(
  config: VoiceLiveConfig,
  providerId: VoiceProviderId,
  model: string | null,
  region: string | null,
  voice: string,
  onError: (message: string) => void,
  agent: string | null = null,
  history: VoiceSeedTurn[] = [],
  settings: VoiceSessionSettings = DEFAULT_VOICE_SETTINGS,
  speechSettings: SpeechVoiceLiveSettings = DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  tools: boolean = false,
  sessionId: string | null = null,
): VoiceLiveController {
  const [status, setStatus] = useState<VoiceLiveStatus>("idle");
  const supported = useSyncExternalStore(
    subscribeVoiceSupport,
    supportsVoiceLive,
    () => false,
  );
  const [userTranscript, setUserTranscript] = useState("");
  const [assistantTranscript, setAssistantTranscript] = useState("");
  const [toolActivity, setToolActivity] = useState("");
  const [turns, setTurns] = useState<LiveTurn[]>([]);
  const [listening, setListening] = useState(false);
  const [speaking, setSpeaking] = useState(false);

  const sessionRef = useRef<LiveSession | null>(null);
  const pendingRef = useRef<PendingLiveSession | null>(null);
  const startingRef = useRef(false);
  const attemptRef = useRef(0);
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
  const providerIdRef = useRef(providerId);
  useEffect(() => {
    providerIdRef.current = providerId;
  }, [providerId]);
  const regionRef = useRef(region);
  useEffect(() => {
    regionRef.current = region;
  }, [region]);
  const modelRef = useRef(model);
  useEffect(() => {
    modelRef.current = model;
  }, [model]);
  const sessionIdRef = useRef(sessionId);
  useEffect(() => {
    sessionIdRef.current = sessionId;
  }, [sessionId]);

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
  const speechSettingsRef = useRef(speechSettings);
  useEffect(() => {
    speechSettingsRef.current = speechSettings;
  }, [speechSettings]);
  const toolsRef = useRef(tools);
  useEffect(() => {
    toolsRef.current = tools;
  }, [tools]);

  const cleanupPending = useCallback((pending: PendingLiveSession) => {
    if (pending.cleaned) return;
    pending.cleaned = true;
    if (pendingRef.current === pending) pendingRef.current = null;
    if (pending?.stream) {
      for (const track of pending.stream.getTracks()) track.stop();
    }
    if (pending?.ctx) {
      void pending.ctx.close().catch(() => {});
    }
  }, []);

  const cleanupSession = useCallback((s: LiveSession) => {
    if (s.cleaned) return;
    s.cleaned = true;
    if (sessionRef.current === s) sessionRef.current = null;
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
    for (const t of s.stream.getTracks()) {
      if (typeof t.removeEventListener === "function") {
        if (s.onTrackEnded) t.removeEventListener("ended", s.onTrackEnded);
        if (s.onTrackMuted) t.removeEventListener("mute", s.onTrackMuted);
      }
      t.stop();
    }
    try {
      if (s.ws.readyState === WebSocket.OPEN || s.ws.readyState === WebSocket.CONNECTING) {
        s.ws.close(1000, "client closed");
      }
    } catch {
      /* ignore */
    }
    if (s.suspendRecoveryTimer !== null) {
      clearTimeout(s.suspendRecoveryTimer);
      s.suspendRecoveryTimer = null;
    }
    // Detach before close() so our own shutdown never re-enters this session
    // via a self-triggered "closed" statechange notification.
    s.ctx.onstatechange = null;
    void s.ctx.close().catch(() => {});
  }, []);

  const teardown = useCallback(() => {
    const pending = pendingRef.current;
    if (pending) cleanupPending(pending);
    const session = sessionRef.current;
    if (session) cleanupSession(session);
  }, [cleanupPending, cleanupSession]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      attemptRef.current += 1;
      startingRef.current = false;
      teardown();
    };
  }, [teardown]);

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
    const attempt = ++attemptRef.current;
    startingRef.current = true;
    setStatus("connecting");
    setUserTranscript("");
    setAssistantTranscript("");
    setToolActivity("");
    setTurns([]);
    setListening(false);
    setSpeaking(false);
    const pending: PendingLiveSession = { ctx: null, stream: null, cleaned: false };
    pendingRef.current = pending;
    try {
      // Begin permission-gated browser APIs before the first await so this work is
      // still directly attributable to the microphone button's user gesture.
      const streamPromise = navigator.mediaDevices
        .getUserMedia({
          audio: microphoneConstraints(providerIdRef.current),
        })
        .catch((error: unknown) => {
          if (attempt === attemptRef.current) {
            reportClientEvent("microphone_error", {
              code:
                error instanceof Error || error instanceof DOMException
                  ? error.name
                  : null,
            });
          }
          throw error;
        })
        .then((stream) => {
          if (attempt !== attemptRef.current) {
            for (const track of stream.getTracks()) track.stop();
            throw new DOMException("Voice start cancelled.", "AbortError");
          }
          pending.stream = stream;
          return stream;
        });
      const ctx = makeAudioContext();
      pending.ctx = ctx;
      const resumePromise = ctx.resume();
      const [subprotocols, stream] = await Promise.all([
        buildSubprotocols(config),
        streamPromise,
        resumePromise,
      ]).then(([protocols, mediaStream]) => [protocols, mediaStream] as const);

      if (attempt !== attemptRef.current) return;
      if (!subprotocols) {
        onErrorRef.current("Please sign in to use live voice.");
        teardown();
        setStatus("idle");
        return;
      }
      if (!mountedRef.current) {
        for (const t of stream.getTracks()) t.stop();
        return;
      }
      await ctx.audioWorklet.addModule(CAPTURE_WORKLET_PATH);
      if (attempt !== attemptRef.current) return;
      const source = ctx.createMediaStreamSource(stream);
      const worklet = new AudioWorkletNode(ctx, "ai4ia-capture");

      // The relay resolves the realtime deployment and (when ?agent= is set) the
      // agent's server-authoritative persona + tool allowlist; the browser only
      // names them. An unknown/disabled agent falls back to the generic assistant.
      const boundSessionId = sessionIdRef.current;
      const wsUrl = buildVoiceLiveWebSocketUrl(config.wsUrl, {
        providerId: providerIdRef.current,
        model: modelRef.current,
        region: regionRef.current,
        sessionId: boundSessionId,
        ...(boundSessionId ? {} : { agent, tools: toolsRef.current }),
      });
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
        opened: false,
        cleaned: false,
        errorReported: false,
        protocolError: null,
        onTrackEnded: null,
        onTrackMuted: null,
        suspendRecoveryTimer: null,
      };
      sessionRef.current = session;
      pendingRef.current = null;

      // The mic track can die out from under an otherwise-healthy socket
      // (permission revoked mid-call, device unplugged, another app taking
      // exclusive access, lid close). Without this, the session silently
      // stops hearing the user forever with no error. finishSession() is a
      // hoisted function declaration defined below in this same scope, so
      // it's safe to reference here. Guarded with a typeof check because
      // some test doubles for MediaStreamTrack only implement stop().
      const handleTrackEnded = () => finishSession(MIC_TRACK_ENDED_MESSAGE, true);
      // A track can also go silent while staying readyState === "live" (so
      // "ended" never fires): the spec requires the browser to flip `muted`
      // true and fire "mute" whenever it "receives no data from the source"
      // (OS-level privacy toggle, another app grabbing exclusive access,
      // audio-routing hiccups). Left unhandled, this reproduces "voice no
      // longer hears them" with the socket otherwise looking perfectly
      // healthy. Treated as fatal (same as "ended") rather than a recoverable
      // wait-for-unmute, matching this app's "capture failures are explicit"
      // requirement.
      const handleTrackMuted = () => finishSession(MIC_TRACK_MUTED_MESSAGE, true);
      session.onTrackEnded = handleTrackEnded;
      session.onTrackMuted = handleTrackMuted;
      for (const track of stream.getTracks()) {
        if (typeof track.addEventListener === "function") {
          track.addEventListener("ended", handleTrackEnded);
          track.addEventListener("mute", handleTrackMuted);
        }
      }
      // The stream was captured well before this point -- ctx.resume(),
      // buildSubprotocols() (an auth round-trip), and addModule() were all
      // awaited above with no listeners attached yet. A track that already
      // ended somewhere in that gap (permission revoked, device unplugged)
      // won't refire "ended" for the listener just attached above -- that
      // only catches a *future* transition. Query the current readyState
      // directly so a track that died during the gap is still caught before
      // ever reaching "live".
      const endedTrack = stream.getTracks().find(
        (track) => track.readyState === "ended",
      );
      if (endedTrack) {
        handleTrackEnded();
        return;
      }
      // Same gap, different failure mode: a track can likewise already be
      // muted -- from the OS toggle, another app grabbing exclusive access,
      // or simply having muted itself during the startup await above -- in
      // which case no "mute" *transition* event ever fires for the listener
      // just attached (there's nothing to transition from, or the
      // transition already happened). Catch that immediately instead of
      // reaching "live" with a track that will never deliver audio for the
      // whole call.
      const alreadyMutedTrack = stream.getTracks().find((track) => track.muted);
      if (alreadyMutedTrack) {
        handleTrackMuted();
        return;
      }

      // See AUDIO_CONTEXT_SUSPENDED_MESSAGE: browsers can suspend an active
      // AudioContext (silently halting the capture worklet) purely from tab
      // backgrounding, independent of mic/track/socket health. finishSession()
      // is a hoisted function declaration, safe to reference here (see the
      // comment on handleTrackEnded above).
      const clearSuspendRecoveryTimer = () => {
        if (session.suspendRecoveryTimer !== null) {
          clearTimeout(session.suspendRecoveryTimer);
          session.suspendRecoveryTimer = null;
        }
      };
      const handleContextStateChange = () => {
        if (sessionRef.current !== session) return;
        if (ctx.state === "running") {
          clearSuspendRecoveryTimer();
          return;
        }
        // "closed" only happens via our own cleanupSession() calling
        // ctx.close(); that path already tears everything down, so this is a
        // no-op rather than a second, redundant finishSession(). Every other
        // state falls through to the same bounded recovery below --
        // including Safari/WebKit's non-standard "interrupted" (fired e.g.
        // on a phone call or Siri taking the mic), which TypeScript's
        // AudioContextState type doesn't even know about. Treating anything
        // other than "running"/"closed" as recoverable-or-fatal, rather than
        // matching only the literal "suspended" string, ensures the client
        // never keeps reporting a live/open session against a context that
        // silently stopped producing audio for any reason.
        if (ctx.state === "closed" || session.suspendRecoveryTimer !== null) {
          return;
        }
        void ctx.resume().catch(() => {});
        session.suspendRecoveryTimer = setTimeout(() => {
          session.suspendRecoveryTimer = null;
          if (sessionRef.current === session && ctx.state !== "running") {
            finishSession(AUDIO_CONTEXT_SUSPENDED_MESSAGE);
          }
        }, AUDIO_CONTEXT_RESUME_GRACE_MS);
      };
      ctx.onstatechange = handleContextStateChange;
      // ctx.resume() was already awaited above, but some browsers defer or
      // silently ignore resume() while the tab is hidden, or require a fresh
      // user gesture -- onstatechange only fires on a *future* transition,
      // so a context that is already stuck "suspended" right now (rather
      // than transitioning to it later) would otherwise never start the
      // recovery grace period. Evaluate the current state immediately
      // through the same handler a real transition would use.
      handleContextStateChange();

      let activeResponseId: string | null = null;
      let activeAssistantItemId: string | null = null;
      let activeAssistantContentIndex: number | null = null;
      let responseAudioStartTime: number | null = null;
      let responseAudioDurationMs = 0;
      let responsePlaybackGapMs = 0;
      let cancellationRequested = false;

      const enqueuePlayback = (b64: string) => {
        const int16 = base64ToInt16(b64);
        if (int16.length === 0) return;
        const float = int16ToFloat32(int16);
        const buffer = ctx.createBuffer(1, float.length, PCM_SAMPLE_RATE);
        buffer.copyToChannel(float, 0);
        const node = ctx.createBufferSource();
        node.buffer = buffer;
        node.connect(ctx.destination);
        const starved =
          responseAudioStartTime !== null &&
          session.nextPlayTime > 0 &&
          session.nextPlayTime <= ctx.currentTime;
        const previousEnd = session.nextPlayTime;
        const startAt =
          session.nextPlayTime > ctx.currentTime
            ? session.nextPlayTime
            : ctx.currentTime + PLAYBACK_REBUFFER_SECONDS;
        if (starved) {
          responsePlaybackGapMs +=
            Math.max(0, startAt - previousEnd) * 1000;
          // The telemetry bridge intentionally emits one event of each shape per
          // page load. Presence diagnoses an underrun without turning audio chunk
          // timing into high-cardinality or flood-prone telemetry.
          reportClientEvent("voice_playback_rebuffer", {
            severity: "warning",
          });
        }
        if (responseAudioStartTime === null) responseAudioStartTime = startAt;
        responseAudioDurationMs += buffer.duration * 1000;
        node.start(startAt);
        session.nextPlayTime = startAt + buffer.duration;
        session.scheduled.add(node);
        node.onended = () => session.scheduled.delete(node);
      };

      const bargeIn = () => {
        if (
          providerIdRef.current === "speech_voice_live" &&
          !speechSettingsRef.current.interruptResponse
        ) {
          return;
        }
        const playedMs =
          responseAudioStartTime === null
            ? 0
            : Math.max(
                0,
                Math.min(
                  responseAudioDurationMs,
                  Math.round(
                    ctx.currentTime * 1000 -
                      responseAudioStartTime * 1000 -
                      responsePlaybackGapMs,
                  ),
                ),
              );
        for (const node of session.scheduled) {
          try {
            node.stop();
          } catch {
            /* already stopped */
          }
        }
        session.scheduled.clear();
        session.nextPlayTime = 0;
        if (activeResponseId && !cancellationRequested) {
          // Both Realtime server VAD and Speech Voice Live interrupt the active
          // response before emitting speech_started. Sending response.cancel
          // here races that server-owned cancellation and produces the fatal
          // response_cancel_not_active error observed in production.
          cancellationRequested = true;
          if (
            activeAssistantItemId &&
            activeAssistantContentIndex !== null &&
            !speechSettingsRef.current.autoTruncate &&
            playedMs > 0
          ) {
            ws.send(
              JSON.stringify({
                type: "conversation.item.truncate",
                item_id: activeAssistantItemId,
                content_index: activeAssistantContentIndex,
                audio_end_ms: Math.max(0, Math.min(Math.round(playedMs), responseAudioDurationMs)),
              }),
            );
          }
        }
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
        pushTurn({
          id,
          role: "assistant",
          text: "",
          streaming: true,
          pending: false,
          createdAt: new Date().toISOString(),
          tool: "",
        });
        return id;
      };

      const handleServerEvent = (ev: MessageEvent) => {
        if (session.cleaned || typeof ev.data !== "string") return;
        let msg: {
          type?: unknown;
          delta?: unknown;
          response_id?: unknown;
          id?: unknown;
          response?: { id?: unknown };
          item_id?: unknown;
          item?: { id?: unknown };
          content_index?: unknown;
          content?: { index?: unknown };
          name?: unknown;
          transcript?: unknown;
          error?: unknown;
        } | null = null;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (!msg) return;
        const type = typeof msg.type === "string" ? msg.type : "";
        switch (type) {
          case "response.audio.delta": {
            if (cancellationRequested) break;
            const delta = typeof msg.delta === "string" ? msg.delta : "";
            if (delta) {
              const responseId =
                typeof msg.response_id === "string"
                  ? msg.response_id
                  : typeof msg.response?.id === "string"
                    ? msg.response.id
                    : activeResponseId;
              if (responseId) activeResponseId = responseId;
              const itemId =
                typeof msg.item_id === "string"
                  ? msg.item_id
                  : typeof msg.item?.id === "string"
                    ? msg.item.id
                    : null;
              if (itemId) activeAssistantItemId = itemId;
              const contentIndex =
                typeof msg.content_index === "number"
                  ? msg.content_index
                  : typeof msg.content?.index === "number"
                    ? msg.content.index
                    : null;
              if (contentIndex !== null) activeAssistantContentIndex = contentIndex;
              enqueuePlayback(delta);
              if (mountedRef.current) setSpeaking(true);
            }
            break;
          }
          case "response.audio_transcript.delta": {
            if (cancellationRequested) break;
            const delta = typeof msg.delta === "string" ? msg.delta : "";
            if (delta && mountedRef.current) {
              setAssistantTranscript((p) => p + delta);
              const id = ensureAssistantTurn();
              patchTurn(id, (t) => ({ ...t, text: t.text + delta, streaming: true }));
            }
            const itemId =
              typeof msg.item_id === "string"
                ? msg.item_id
                : typeof msg.item?.id === "string"
                  ? msg.item.id
                  : null;
            if (itemId) activeAssistantItemId = itemId;
            const contentIndex =
              typeof msg.content_index === "number"
                ? msg.content_index
                : typeof msg.content?.index === "number"
                  ? msg.content.index
                  : null;
            if (contentIndex !== null) activeAssistantContentIndex = contentIndex;
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
                  createdAt: new Date().toISOString(),
                  tool: "",
                });
              }
            }
            userTurnId = null;
            break;
          }
          case "response.created": {
            cancellationRequested = false;
            responseAudioStartTime = null;
            responseAudioDurationMs = 0;
            responsePlaybackGapMs = 0;
            activeAssistantItemId = null;
            activeAssistantContentIndex = null;
            const responseId =
              typeof msg.response_id === "string"
                ? msg.response_id
                : typeof msg.response?.id === "string"
                  ? msg.response.id
                  : typeof msg.id === "string"
                    ? msg.id
                    : null;
            if (responseId) activeResponseId = responseId;
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
            activeResponseId = null;
            activeAssistantItemId = null;
            activeAssistantContentIndex = null;
            responseAudioStartTime = null;
            responseAudioDurationMs = 0;
            responsePlaybackGapMs = 0;
            cancellationRequested = false;
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
              createdAt: new Date().toISOString(),
              tool: "",
            });
            break;
          }
          case "input_audio_buffer.speech_stopped": {
            if (mountedRef.current) setListening(false);
            break;
          }
          case "error": {
            if (!session.protocolError) {
              session.protocolError = parseVoiceProtocolError(msg.error);
            }
            finishSession(formatVoiceProtocolError(session.protocolError));
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
        if (sessionRef.current !== session || session.cleaned) return;
        session.opened = true;
        for (const frame of
          buildInitialVoiceFrames({
            providerId: providerIdRef.current,
            model: modelRef.current,
            voice: voiceRef.current,
            history: historyRef.current,
            settings: settingsRef.current,
            speechSettings: speechSettingsRef.current,
          })) {
          ws.send(frame);
        }
        // Connect the capture graph. The worklet emits silence to the
        // destination (it only forwards mic frames via its port), so wiring it to
        // the destination keeps it in the active render graph without echo.
        source.connect(worklet);
        worklet.connect(ctx.destination);
        if (mountedRef.current) setStatus("live");
      };
      ws.onmessage = handleServerEvent;
      // A handshake the browser can't complete (e.g. the gateway or upstream
      // realtime service rejects the upgrade with a 503) surfaces only as a
      // bare error/close — the HTTP status is not readable from WebSocket. Both
      // onerror and onclose can fire for the same failure (and both fire for a
      // clean, user-initiated stop() too), so this is guarded on
      // ``sessionRef.current === session``: stop()'s teardown() nulls that ref
      // synchronously before closing the socket, so a later, expected onclose
      // is a no-op here, while an unhandled failure (whichever of
      // onerror/onclose fires first) tears down and reports exactly once. The
      // message depends on whether the session ever reached "live"
      // (session.opened).
      function finishSession(message: string, reportMicrophoneError = false) {
        if (sessionRef.current !== session) return;
        if (!session.errorReported) {
          session.errorReported = true;
          if (reportMicrophoneError) reportClientEvent("microphone_error");
          onErrorRef.current(message);
        }
        cleanupSession(session);
        if (mountedRef.current) {
          setListening(false);
          setSpeaking(false);
          setStatus("idle");
        }
      }
      ws.onerror = () =>
        finishSession(
          session.protocolError
            ? formatVoiceProtocolError(session.protocolError)
            : formatVoiceCloseError(session.opened),
        );
      ws.onclose = (event) =>
        finishSession(
          session.protocolError
            ? formatVoiceProtocolError(session.protocolError)
            : formatVoiceCloseError(session.opened, event),
        );
    } catch (e) {
      const cancelled =
        attempt !== attemptRef.current ||
        (e instanceof DOMException && e.name === "AbortError");
      if (cancelled) {
        // A later retry may already own the shared refs. Only clean resources that
        // still belong to this cancelled attempt; never tear down the newer start.
        if (pendingRef.current === pending) {
          cleanupPending(pending);
        }
      } else {
        onErrorRef.current((e as Error).message || "Couldn't start live voice.");
        // Invalidate this attempt before releasing known resources. If the mic
        // permission promise resolves later, its continuation sees the mismatch
        // and stops the newly returned track instead of retaining it.
        if (attempt === attemptRef.current) {
          attemptRef.current += 1;
          startingRef.current = false;
        }
        teardown();
        if (mountedRef.current) setStatus("idle");
      }
    } finally {
      if (attempt === attemptRef.current) startingRef.current = false;
    }
  }, [agent, cleanupPending, cleanupSession, config, teardown]);

  const stop = useCallback(() => {
    attemptRef.current += 1;
    startingRef.current = false;
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
    start,
    toggle,
    stop,
  };
}
