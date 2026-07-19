"use client";

// Browser microphone capture for speech-to-text. Encapsulates MediaRecorder
// lifecycle (start/stop, track cleanup), a start-race guard, and staleness
// handling so a transcription that resolves after unmount is dropped. The hook
// owns no UI: it exposes state + a single toggle and calls back with the text.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";

import { transcribeAudio, synthesizeSpeech } from "./api";
import { reportClientEvent } from "./clientTelemetry";

// Preference order. Chrome/Firefox favor webm/opus; Safari only does mp4.
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/ogg;codecs=opus",
  "audio/mp4",
  "video/webm",
];

function pickMime(): string | undefined {
  if (typeof MediaRecorder === "undefined") return undefined;
  for (const m of MIME_CANDIDATES) {
    try {
      if (MediaRecorder.isTypeSupported(m)) return m;
    } catch {
      /* isTypeSupported can throw on some engines; keep trying */
    }
  }
  return undefined;
}

function extForMime(mime: string): string {
  const base = (mime.split(";")[0] || "").toLowerCase();
  if (base.includes("webm")) return "webm";
  if (base.includes("ogg")) return "ogg";
  if (base.includes("mp4") || base.includes("mpeg") || base.includes("m4a")) {
    return "mp4";
  }
  if (base.includes("wav")) return "wav";
  return "webm";
}

export interface VoiceRecorder {
  recording: boolean;
  transcribing: boolean;
  supported: boolean;
  toggle: () => void;
}

// Whether this browser can capture voice input at all (getUserMedia +
// MediaRecorder). Capability can't change while the page is open, so this is
// read via useSyncExternalStore instead of an effect+setState: ordinary
// client renders call getClientVoiceInputSupport() directly (no extra
// commit-then-cascading-render pass), and only actual SSR/hydration falls
// back to getServerVoiceInputSupport()'s fixed `false` — matching the old
// "starts false, resolves after mount" behavior without needing a mount
// effect to flip it.
function subscribeToNothing(): () => void {
  return () => {};
}
function getClientVoiceInputSupport(): boolean {
  return (
    typeof navigator !== "undefined" &&
    !!navigator.mediaDevices?.getUserMedia &&
    typeof window !== "undefined" &&
    typeof window.MediaRecorder !== "undefined"
  );
}
function getServerVoiceInputSupport(): boolean {
  return false;
}

export function useVoiceRecorder(
  onTranscript: (text: string) => void,
  onError: (message: string) => void,
): VoiceRecorder {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  // Resolved after mount to avoid an SSR/client hydration mismatch on the
  // button's disabled state.
  const supported = useSyncExternalStore(
    subscribeToNothing,
    getClientVoiceInputSupport,
    getServerVoiceInputSupport,
  );

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const startingRef = useRef(false);
  const mountedRef = useRef(true);

  // Keep the latest callbacks in refs so the long-lived recorder handlers never
  // capture a stale closure (and so the start/stop callbacks stay referentially
  // stable, which matters for the cleanup effect).
  const onTranscriptRef = useRef(onTranscript);
  const onErrorRef = useRef(onError);
  useEffect(() => {
    onTranscriptRef.current = onTranscript;
    onErrorRef.current = onError;
  }, [onTranscript, onError]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        try {
          recorder.stop();
        } catch {
          /* ignore */
        }
      }
      const stream = streamRef.current;
      if (stream) for (const t of stream.getTracks()) t.stop();
      streamRef.current = null;
      recorderRef.current = null;
    };
  }, []);

  const stopTracks = useCallback(() => {
    const stream = streamRef.current;
    if (stream) for (const t of stream.getTracks()) t.stop();
    streamRef.current = null;
  }, []);

  const handleStop = useCallback(() => {
    const recorder = recorderRef.current;
    const mime = recorder?.mimeType || "audio/webm";
    const blob = new Blob(chunksRef.current, { type: mime });
    chunksRef.current = [];
    stopTracks();
    recorderRef.current = null;
    if (mountedRef.current) setRecording(false);

    if (!mountedRef.current) return;
    if (blob.size === 0) {
      onErrorRef.current("No audio was captured. Try again.");
      return;
    }
    setTranscribing(true);
    void (async () => {
      try {
        const result = await transcribeAudio(blob, {
          filename: `recording.${extForMime(mime)}`,
        });
        if (!mountedRef.current) return;
        const text = result.text.trim();
        if (text) onTranscriptRef.current(text);
        else onErrorRef.current("Couldn't make out any speech in that recording.");
      } catch (e) {
        if (mountedRef.current) onErrorRef.current((e as Error).message);
      } finally {
        if (mountedRef.current) setTranscribing(false);
      }
    })();
  }, [stopTracks]);

  const start = useCallback(async () => {
    if (startingRef.current || recorderRef.current) return;
    if (
      typeof navigator === "undefined" ||
      !navigator.mediaDevices?.getUserMedia ||
      typeof window === "undefined" ||
      typeof window.MediaRecorder === "undefined"
    ) {
      onErrorRef.current("Voice input isn't supported in this browser.");
      return;
    }
    startingRef.current = true;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mountedRef.current) {
        for (const t of stream.getTracks()) t.stop();
        return;
      }
      streamRef.current = stream;
      const mime = pickMime();
      const recorder = mime
        ? new MediaRecorder(stream, { mimeType: mime })
        : new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onerror = () => {
        if (mountedRef.current && recorderRef.current === recorder) {
          reportClientEvent("microphone_error");
        }
        stopTracks();
        recorderRef.current = null;
        if (mountedRef.current) {
          setRecording(false);
          onErrorRef.current("Recording failed.");
        }
      };
      recorder.onstop = handleStop;
      recorderRef.current = recorder;
      recorder.start();
      setRecording(true);
    } catch (error) {
      stopTracks();
      if (mountedRef.current) {
        reportClientEvent("microphone_error", {
          code:
            error instanceof Error || error instanceof DOMException
              ? error.name
              : null,
        });
        onErrorRef.current("Microphone access was denied or unavailable.");
      }
    } finally {
      startingRef.current = false;
    }
  }, [stopTracks, handleStop]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") recorder.stop();
  }, []);

  const toggle = useCallback(() => {
    if (recording) stop();
    else void start();
  }, [recording, start, stop]);

  return { recording, transcribing, supported, toggle };
}



export type SpeechState = "idle" | "busy" | "playing";

export interface SpeechPlayback {
  activeId: string | null;
  busyId: string | null;
  toggle: (id: string, text: string) => void;
}

// The TTS endpoint caps a single request near the provider's hard limit, so a
// long answer is spoken as an ordered sequence of sub-limit chunks. Kept a little
// under the server's 4000 so trimming/encoding can never push a chunk over.
export const TTS_CHUNK_LIMIT = 3500;

// Split text into speech-sized chunks (<= limit) at natural boundaries so a long
// message can be read aloud in order. Prefers sentence/paragraph breaks, falls
// back to word boundaries, and hard-splits only a single token longer than the
// limit. Exported for unit testing.
export function chunkForSpeech(text: string, limit = TTS_CHUNK_LIMIT): string[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  if (trimmed.length <= limit) return [trimmed];

  // Sentence-ish units: leading space + a run up to (and including) terminators,
  // or a run of newlines as its own break point.
  const units = trimmed.match(/\s*\S[^.!?\n]*[.!?]*|\n+/g) ?? [trimmed];
  const chunks: string[] = [];
  let cur = "";
  const flush = () => {
    const t = cur.trim();
    if (t) chunks.push(t);
    cur = "";
  };
  for (const raw of units) {
    for (const unit of splitOversized(raw, limit)) {
      if (cur && (cur + unit).length > limit) flush();
      cur += unit;
    }
  }
  flush();
  return chunks;
}

// Break a single unit longer than the limit on word boundaries, hard-cutting only
// when a lone "word" still exceeds it.
function splitOversized(unit: string, limit: number): string[] {
  if (unit.length <= limit) return [unit];
  const out: string[] = [];
  let rest = unit;
  while (rest.length > limit) {
    let cut = rest.lastIndexOf(" ", limit);
    if (cut <= 0) cut = limit;
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut);
  }
  if (rest) out.push(rest);
  return out;
}

// Numeric HTMLMediaElement/MediaError codes per the HTML spec (fixed values,
// unchanged across browsers), used directly instead of the `MediaError`
// global so this works the same in jsdom/test environments that may not
// define it. Appended to the generic "Couldn't play the synthesized audio."
// message so a real decode/format failure in production is distinguishable
// from a transient network blip instead of being one opaque, unhelpful string.
const MEDIA_ERR_ABORTED = 1;
const MEDIA_ERR_NETWORK = 2;
const MEDIA_ERR_DECODE = 3;
const MEDIA_ERR_SRC_NOT_SUPPORTED = 4;

function mediaErrorDetail(audio: HTMLAudioElement): string {
  switch (audio.error?.code) {
    case MEDIA_ERR_ABORTED:
      return " Playback was aborted.";
    case MEDIA_ERR_NETWORK:
      return " A network error interrupted the download. Check your connection and try again.";
    case MEDIA_ERR_DECODE:
      return " The audio could not be decoded.";
    case MEDIA_ERR_SRC_NOT_SUPPORTED:
      return " This browser can't play the returned audio format.";
    default:
      return "";
  }
}

// Single-active text-to-speech playback keyed by message id. Fetching a new clip
// stops/revokes the previous one; a request token drops results that resolve
// after the user moved on or the component unmounted.
export function useSpeechPlayback(
  onError: (message: string) => void,
): SpeechPlayback {
  const [activeId, setActiveId] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const tokenRef = useRef(0);
  const mountedRef = useRef(true);
  // Rejects the in-flight chunk's playback promise when stop()/cleanup() runs, so
  // the sequential player never hangs awaiting an ``ended`` that won't fire.
  const cancelPlaybackRef = useRef<(() => void) | null>(null);

  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  const cleanup = useCallback(() => {
    // Un-hang any awaited chunk playback before tearing down the element.
    cancelPlaybackRef.current?.();
    cancelPlaybackRef.current = null;
    const audio = audioRef.current;
    if (audio) {
      audio.pause();
      audio.onended = null;
      audio.onerror = null;
      audioRef.current = null;
    }
    if (urlRef.current) {
      URL.revokeObjectURL(urlRef.current);
      urlRef.current = null;
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      tokenRef.current += 1;
      cleanup();
    };
  }, [cleanup]);

  const stop = useCallback(() => {
    tokenRef.current += 1; // invalidate any in-flight fetch/playback
    cleanup();
    if (mountedRef.current) {
      setActiveId(null);
      setBusyId(null);
    }
  }, [cleanup]);

  // Play one already-fetched clip to its end. Resolves on ``ended``, rejects on a
  // playback error or when stop()/cleanup() cancels it (via cancelPlaybackRef).
  const playToEnd = useCallback((audio: HTMLAudioElement) => {
    return new Promise<void>((resolve, reject) => {
      cancelPlaybackRef.current = () => {
        cancelPlaybackRef.current = null;
        reject(new Error("__stopped__"));
      };
      audio.onended = () => {
        cancelPlaybackRef.current = null;
        resolve();
      };
      audio.onerror = () => {
        cancelPlaybackRef.current = null;
        if (mountedRef.current && audioRef.current === audio) {
          reportClientEvent("media_playback_error");
        }
        reject(new Error(`Couldn't play the synthesized audio.${mediaErrorDetail(audio)}`));
      };
      audio.play().catch((e) => {
        cancelPlaybackRef.current = null;
        if (mountedRef.current && audioRef.current === audio) {
          reportClientEvent("media_playback_error", {
            code: e instanceof Error ? e.name : null,
          });
        }
        reject(e as Error);
      });
    });
  }, []);

  const toggle = useCallback(
    (id: string, text: string) => {
      // Clicking the message that's already active/loading stops it.
      if (activeId === id || busyId === id) {
        stop();
        return;
      }
      stop(); // switching: tear down whatever was playing/loading first
      const chunks = chunkForSpeech(text);
      if (chunks.length === 0) {
        onErrorRef.current("There's nothing to read aloud.");
        return;
      }
      const token = ++tokenRef.current;
      setBusyId(id);
      void (async () => {
        try {
          // Prefetch pipeline: synthesize the next chunk while the current plays so
          // long answers read back with minimal gaps.
          let nextBlob: Promise<Blob> | null = synthesizeSpeech(chunks[0]);
          for (let i = 0; i < chunks.length; i++) {
            const blob = await nextBlob!;
            if (token !== tokenRef.current || !mountedRef.current) return;
            nextBlob =
              i + 1 < chunks.length ? synthesizeSpeech(chunks[i + 1]) : null;
            // Keep an unused prefetch from rejecting unhandled if playback stops.
            nextBlob?.catch(() => {});

            const url = URL.createObjectURL(blob);
            urlRef.current = url;
            const audio = new Audio(url);
            audioRef.current = audio;
            setBusyId(null);
            setActiveId(id);
            await playToEnd(audio);
            if (urlRef.current === url) {
              URL.revokeObjectURL(url);
              urlRef.current = null;
            }
            if (token !== tokenRef.current || !mountedRef.current) return;
          }
          cleanup();
          if (mountedRef.current) setActiveId(null);
        } catch (e) {
          // A stop/switch invalidates the token; only surface real failures.
          if (token === tokenRef.current && mountedRef.current) {
            setBusyId(null);
            setActiveId(null);
            const msg = (e as Error).message;
            if (msg !== "__stopped__") {
              onErrorRef.current(msg);
            }
          }
        }
      })();
    },
    [activeId, busyId, stop, cleanup, playToEnd],
  );

  return { activeId, busyId, toggle };
}
