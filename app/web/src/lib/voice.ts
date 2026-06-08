"use client";

// Browser microphone capture for speech-to-text. Encapsulates MediaRecorder
// lifecycle (start/stop, track cleanup), a start-race guard, and staleness
// handling so a transcription that resolves after unmount is dropped. The hook
// owns no UI: it exposes state + a single toggle and calls back with the text.
import { useCallback, useEffect, useRef, useState } from "react";

import { transcribeAudio, synthesizeSpeech } from "./api";

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

export function useVoiceRecorder(
  onTranscript: (text: string) => void,
  onError: (message: string) => void,
): VoiceRecorder {
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  // Resolved after mount to avoid an SSR/client hydration mismatch on the
  // button's disabled state.
  const [supported, setSupported] = useState(false);

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
    setSupported(
      typeof navigator !== "undefined" &&
        !!navigator.mediaDevices?.getUserMedia &&
        typeof window !== "undefined" &&
        typeof window.MediaRecorder !== "undefined",
    );
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
    } catch {
      stopTracks();
      onErrorRef.current("Microphone access was denied or unavailable.");
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

  const onErrorRef = useRef(onError);
  useEffect(() => {
    onErrorRef.current = onError;
  }, [onError]);

  const cleanup = useCallback(() => {
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

  const toggle = useCallback(
    (id: string, text: string) => {
      // Clicking the message that's already active/loading stops it.
      if (activeId === id || busyId === id) {
        stop();
        return;
      }
      stop(); // switching: tear down whatever was playing/loading first
      const trimmed = text.trim();
      if (!trimmed) {
        onErrorRef.current("There's nothing to read aloud.");
        return;
      }
      if (trimmed.length > 4000) {
        onErrorRef.current(
          "That message is too long to read aloud (4000 character limit).",
        );
        return;
      }
      const token = ++tokenRef.current;
      setBusyId(id);
      void (async () => {
        try {
          const blob = await synthesizeSpeech(trimmed);
          if (token !== tokenRef.current || !mountedRef.current) return;
          const url = URL.createObjectURL(blob);
          urlRef.current = url;
          const audio = new Audio(url);
          audioRef.current = audio;
          audio.onended = () => {
            if (token !== tokenRef.current) return;
            cleanup();
            if (mountedRef.current) setActiveId(null);
          };
          audio.onerror = () => {
            if (token !== tokenRef.current) return;
            cleanup();
            if (mountedRef.current) {
              setActiveId(null);
              onErrorRef.current("Couldn't play the synthesized audio.");
            }
          };
          setBusyId(null);
          setActiveId(id);
          await audio.play();
        } catch (e) {
          if (token === tokenRef.current && mountedRef.current) {
            setBusyId(null);
            setActiveId(null);
            onErrorRef.current((e as Error).message);
          }
        }
      })();
    },
    [activeId, busyId, stop, cleanup],
  );

  return { activeId, busyId, toggle };
}
