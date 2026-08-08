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
} from "react";

import { synthesizeSpeech } from "./api";
import { reportClientEvent } from "./clientTelemetry";

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
