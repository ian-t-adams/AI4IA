// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const telemetry = vi.hoisted(() => ({
  reportClientEvent: vi.fn(),
}));
vi.mock("./clientTelemetry", () => telemetry);

// useVoiceRecorder only calls transcribeAudio/synthesizeSpeech while actually
// recording; this file exercises just the `supported` capability flag, so
// mock the API client the same way voice.playback.test.tsx does: deterministic,
// no network.
vi.mock("./api", () => ({
  transcribeAudio: vi.fn(),
  synthesizeSpeech: vi.fn(),
}));

import { useVoiceRecorder } from "./voice";

// Regression: `supported` used to be seeded via useState(false) and flipped by
// a setState call inside the mount effect (a react-hooks/set-state-in-effect
// lint violation, plus an extra commit-then-effect-then-recommit round trip
// before the recorder button's disabled state ever reflected reality). It is
// now read straight from navigator/window via useSyncExternalStore, so a
// capable browser reports `supported: true` on the very first render -- no
// waitFor/act needed to observe a post-mount transition.
describe("useVoiceRecorder supported flag", () => {
  const originalMediaDevices = navigator.mediaDevices;
  const originalMediaRecorder = (window as { MediaRecorder?: unknown })
    .MediaRecorder;

  afterEach(() => {
    cleanup();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: originalMediaDevices,
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: originalMediaRecorder,
    });
    telemetry.reportClientEvent.mockReset();
  });

  it("reports true when the browser exposes getUserMedia and MediaRecorder", () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn() },
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: class {},
    });

    const { result } = renderHook(() => useVoiceRecorder(vi.fn(), vi.fn()));

    expect(result.current.supported).toBe(true);
  });

  it("reports false when the browser has no mediaDevices.getUserMedia", () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {},
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: class {},
    });

    const { result } = renderHook(() => useVoiceRecorder(vi.fn(), vi.fn()));

    expect(result.current.supported).toBe(false);
  });

  it("reports false when the browser has no MediaRecorder", () => {
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn() },
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: undefined,
    });

    const { result } = renderHook(() => useVoiceRecorder(vi.fn(), vi.fn()));

    expect(result.current.supported).toBe(false);
  });

  it("reports microphone access failures through content-free telemetry", async () => {
    const getUserMedia = vi
      .fn()
      .mockRejectedValue(new DOMException("Permission denied.", "NotAllowedError"));
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: class {},
    });
    const onError = vi.fn();
    const { result } = renderHook(() => useVoiceRecorder(vi.fn(), onError));

    act(() => result.current.toggle());

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith(
        "Microphone access was denied or unavailable.",
      ),
    );
    expect(telemetry.reportClientEvent).toHaveBeenCalledWith(
      "microphone_error",
      { code: "NotAllowedError" },
    );
  });

  it("does not report a stale microphone failure after unmount", async () => {
    let rejectMicrophone!: (reason: unknown) => void;
    const microphone = new Promise<MediaStream>((_resolve, reject) => {
      rejectMicrophone = reject;
    });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockReturnValue(microphone) },
    });
    Object.defineProperty(window, "MediaRecorder", {
      configurable: true,
      value: class {},
    });
    const onError = vi.fn();
    const { result, unmount } = renderHook(() =>
      useVoiceRecorder(vi.fn(), onError),
    );

    act(() => result.current.toggle());
    unmount();
    await act(async () => {
      rejectMicrophone(new DOMException("Permission denied.", "NotAllowedError"));
      await microphone.catch(() => undefined);
    });

    expect(telemetry.reportClientEvent).not.toHaveBeenCalled();
    expect(onError).not.toHaveBeenCalled();
  });
});
