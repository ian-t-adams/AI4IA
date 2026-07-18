// @vitest-environment jsdom
import { cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

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
});
