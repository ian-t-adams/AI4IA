// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// useSpeechPlayback only calls synthesizeSpeech (never fetch/apiFetch directly),
// so mock it the same way Composer.test.tsx mocks useVoiceRecorder: deterministic,
// no network, no MediaRecorder/getUserMedia plumbing to fake.
const { mockSynthesizeSpeech, mockReportClientEvent } = vi.hoisted(() => ({
  mockSynthesizeSpeech: vi.fn(),
  mockReportClientEvent: vi.fn(),
}));
vi.mock("./api", () => ({
  synthesizeSpeech: mockSynthesizeSpeech,
  transcribeAudio: vi.fn(),
}));
vi.mock("./clientTelemetry", () => ({
  reportClientEvent: mockReportClientEvent,
}));

import { useSpeechPlayback } from "./voice";

// jsdom's HTMLMediaElement.play()/pause() are stubbed as "not implemented", so
// (like the AudioContext/WebSocket fakes in voiceLive.lifecycle.test.tsx) a
// minimal, fully controllable double stands in for the real <audio> element.
class FakeAudio {
  static instances: FakeAudio[] = [];

  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  error: { code: number } | null = null;
  play = vi.fn(() => Promise.resolve());
  pause = vi.fn();

  constructor(public src: string) {
    FakeAudio.instances.push(this);
  }
}

beforeEach(() => {
  FakeAudio.instances = [];
  Object.defineProperty(window, "Audio", { configurable: true, value: FakeAudio });
  Object.defineProperty(globalThis, "Audio", { configurable: true, value: FakeAudio });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:fake-audio"),
  });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
});

afterEach(() => {
  cleanup();
  mockSynthesizeSpeech.mockReset();
  mockReportClientEvent.mockReset();
});

describe("useSpeechPlayback", () => {
  it("plays synthesized audio to completion and returns to idle without an error", async () => {
    mockSynthesizeSpeech.mockResolvedValue(new Blob(["a"], { type: "audio/mpeg" }));
    const onError = vi.fn();
    const { result } = renderHook(() => useSpeechPlayback(onError));

    act(() => {
      result.current.toggle("m1", "Hello there");
    });
    await waitFor(() => expect(FakeAudio.instances).toHaveLength(1));
    await waitFor(() => expect(result.current.activeId).toBe("m1"));
    expect(result.current.busyId).toBeNull();

    act(() => {
      FakeAudio.instances[0].onended?.();
    });
    await waitFor(() => expect(result.current.activeId).toBeNull());
    expect(onError).not.toHaveBeenCalled();
  });

  // Regression: the browser's <audio> onerror handler used to reject with a
  // single generic string no matter the cause, so a real decode/format
  // failure in production was indistinguishable from a stale request. It now
  // carries the MediaError code so the surfaced message is diagnosable.
  it("surfaces a MediaError-specific detail when browser playback fails", async () => {
    mockSynthesizeSpeech.mockResolvedValue(new Blob(["a"], { type: "audio/mpeg" }));
    const onError = vi.fn();
    const { result } = renderHook(() => useSpeechPlayback(onError));

    act(() => {
      result.current.toggle("m1", "Hello there");
    });
    await waitFor(() => expect(FakeAudio.instances).toHaveLength(1));
    const audio = FakeAudio.instances[0];
    audio.error = { code: 3 }; // MEDIA_ERR_DECODE
    act(() => {
      audio.onerror?.();
    });

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith(
        "Couldn't play the synthesized audio. The audio could not be decoded.",
      ),
    );
    expect(result.current.activeId).toBeNull();
    expect(result.current.busyId).toBeNull();
    expect(mockReportClientEvent).toHaveBeenCalledWith("media_playback_error");
  });

  it("falls back to the generic message when no MediaError code is available", async () => {
    mockSynthesizeSpeech.mockResolvedValue(new Blob(["a"], { type: "audio/mpeg" }));
    const onError = vi.fn();
    const { result } = renderHook(() => useSpeechPlayback(onError));

    act(() => {
      result.current.toggle("m1", "Hello there");
    });
    await waitFor(() => expect(FakeAudio.instances).toHaveLength(1));
    act(() => {
      FakeAudio.instances[0].onerror?.();
    });

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith("Couldn't play the synthesized audio."),
    );
  });

  // Regression: a bad TTS response (validated in api.speech.test.ts: wrong
  // content-type, empty body, non-OK) must surface its specific reason and
  // never reach an <audio> element at all.
  it("surfaces the synthesis error and never creates an audio element for a failed request", async () => {
    mockSynthesizeSpeech.mockRejectedValue(
      new Error("Speech synthesis returned text/html instead of audio. Try again."),
    );
    const onError = vi.fn();
    const { result } = renderHook(() => useSpeechPlayback(onError));

    act(() => {
      result.current.toggle("m1", "Hello there");
    });

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith(
        "Speech synthesis returned text/html instead of audio. Try again.",
      ),
    );
    expect(result.current.busyId).toBeNull();
    expect(result.current.activeId).toBeNull();
    expect(FakeAudio.instances).toHaveLength(0);
    expect(mockReportClientEvent).not.toHaveBeenCalled();
  });

  it("does not surface an error when playback is stopped deliberately mid-flight", async () => {
    mockSynthesizeSpeech.mockResolvedValue(new Blob(["a"], { type: "audio/mpeg" }));
    const onError = vi.fn();
    const { result } = renderHook(() => useSpeechPlayback(onError));

    act(() => {
      result.current.toggle("m1", "Hello there");
    });
    await waitFor(() => expect(result.current.activeId).toBe("m1"));

    // Clicking the already-active message stops it instead of restarting it.
    act(() => {
      result.current.toggle("m1", "Hello there");
    });
    await waitFor(() => expect(result.current.activeId).toBeNull());
    expect(onError).not.toHaveBeenCalled();
  });
});
