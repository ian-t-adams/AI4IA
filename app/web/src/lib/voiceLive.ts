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

export interface VoiceLiveConfig {
  enabled: boolean;
  // wss:// URL of the relay endpoint (API external ingress + /api/voice/live).
  wsUrl: string;
  // Dev-auth fallback identity (ignored under Entra).
  devUser: string;
}

export type VoiceLiveStatus = "idle" | "connecting" | "live" | "closing";

export interface VoiceLiveController {
  status: VoiceLiveStatus;
  active: boolean;
  supported: boolean;
  userTranscript: string;
  assistantTranscript: string;
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

function sessionUpdate(): string {
  return JSON.stringify({
    type: "session.update",
    session: {
      instructions: DEFAULT_INSTRUCTIONS,
      voice: "alloy",
      input_audio_format: "pcm16",
      output_audio_format: "pcm16",
      turn_detection: { type: "server_vad" },
      input_audio_transcription: { model: "whisper-1" },
    },
  });
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
  return [DEV_SUBPROTOCOL, config.devUser || "dev"];
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
  onError: (message: string) => void,
): VoiceLiveController {
  const [status, setStatus] = useState<VoiceLiveStatus>("idle");
  const [supported, setSupported] = useState(false);
  const [userTranscript, setUserTranscript] = useState("");
  const [assistantTranscript, setAssistantTranscript] = useState("");

  const sessionRef = useRef<LiveSession | null>(null);
  const startingRef = useRef(false);
  const mountedRef = useRef(true);

  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

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
      if (!mountedRef.current) {
        for (const t of stream.getTracks()) t.stop();
        return;
      }
      const ctx = makeAudioContext();
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

      const wsUrl = model
        ? `${config.wsUrl}?model=${encodeURIComponent(model)}`
        : config.wsUrl;
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
            if (delta) enqueuePlayback(delta);
            break;
          }
          case "response.audio_transcript.delta": {
            const delta = typeof msg.delta === "string" ? msg.delta : "";
            if (delta && mountedRef.current) {
              setAssistantTranscript((p) => p + delta);
            }
            break;
          }
          case "conversation.item.input_audio_transcription.completed": {
            const t = typeof msg.transcript === "string" ? msg.transcript : "";
            if (t && mountedRef.current) {
              setUserTranscript((p) => (p ? `${p} ` : "") + t.trim());
            }
            break;
          }
          case "response.created": {
            if (mountedRef.current) setAssistantTranscript("");
            break;
          }
          case "input_audio_buffer.speech_started": {
            bargeIn();
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
        ws.send(sessionUpdate());
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
          if (mountedRef.current) setStatus("idle");
        }
      };
    } catch (e) {
      onErrorRef.current((e as Error).message || "Couldn't start live voice.");
      teardown();
      if (mountedRef.current) setStatus("idle");
    } finally {
      startingRef.current = false;
    }
  }, [config, model, teardown]);

  const stop = useCallback(() => {
    setStatus("closing");
    teardown();
    if (mountedRef.current) setStatus("idle");
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
    toggle,
    stop,
  };
}
