// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { supportsVoiceLive, useVoiceLive } from "./voiceLive";
import {
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_VOICE_SETTINGS,
} from "./voiceLive";

const auth = vi.hoisted(() => ({
  getToken: vi.fn<() => Promise<string | null>>(),
}));

vi.mock("./auth", () => ({
  isEntraEnabled: () => true,
  getApiAccessToken: () => auth.getToken(),
}));

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

class FakeAudioContext {
  static instances: FakeAudioContext[] = [];

  currentTime = 0;
  destination = {};
  audioWorklet = { addModule: vi.fn(async () => {}) };
  resume = vi.fn(async () => {});
  close = vi.fn(async () => {});
  createMediaStreamSource = vi.fn(() => ({
    connect: vi.fn(),
    disconnect: vi.fn(),
  }));
  bufferSources: FakeAudioBufferSource[] = [];
  createBuffer = vi.fn(() => ({
    duration: 0.1,
    copyToChannel: vi.fn(),
  }));
  createBufferSource = vi.fn(() => {
    const source = new FakeAudioBufferSource();
    this.bufferSources.push(source);
    return source;
  });

  constructor() {
    FakeAudioContext.instances.push(this);
  }
}

class FakeAudioBufferSource {
  buffer: unknown = null;
  onended: (() => void) | null = null;
  connect = vi.fn();
  start = vi.fn();
  stop = vi.fn();
}

class FakeAudioWorkletNode {
  port = { onmessage: null as ((event: MessageEvent) => void) | null };
  connect = vi.fn();
  disconnect = vi.fn();
}

class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  binaryType = "";
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  send = vi.fn();
  close = vi.fn(() => {
    this.readyState = FakeWebSocket.CLOSED;
  });

  constructor(
    public readonly url: string,
    public readonly protocols: string[],
  ) {
    FakeWebSocket.instances.push(this);
  }
}

const CONFIG = {
  enabled: true,
  wsUrl: "wss://api.example.test/api/voice/live",
  devUser: "dev",
  toolsAvailable: false,
};

beforeEach(() => {
  FakeAudioContext.instances = [];
  FakeWebSocket.instances = [];
  Object.defineProperty(window, "AudioContext", {
    configurable: true,
    value: FakeAudioContext,
  });
  Object.defineProperty(window, "AudioWorkletNode", {
    configurable: true,
    value: FakeAudioWorkletNode,
  });
  Object.defineProperty(window, "WebSocket", {
    configurable: true,
    value: FakeWebSocket,
  });
  Object.defineProperty(globalThis, "AudioWorkletNode", {
    configurable: true,
    value: FakeAudioWorkletNode,
  });
  Object.defineProperty(globalThis, "WebSocket", {
    configurable: true,
    value: FakeWebSocket,
  });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:worklet"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("useVoiceLive lifecycle", () => {
  it("does not request auth, microphone access, or an Azure OpenAI socket while disabled", () => {
    const getUserMedia = vi.fn();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useVoiceLive(
        { ...CONFIG, enabled: false },
        "azure_openai",
        "gpt-realtime",
        "eastus2",
        "alloy",
        onError,
      ),
    );

    act(() => {
      result.current.start();
    });

    expect(onError).toHaveBeenCalledWith("Live voice isn't available.");
    expect(auth.getToken).not.toHaveBeenCalled();
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(FakeAudioContext.instances).toHaveLength(0);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it("stops a microphone stream that resolves after another startup step fails", async () => {
    auth.getToken.mockRejectedValue(new Error("token acquisition failed"));
    const microphone = deferred<{ getTracks: () => { stop: () => void }[] }>();
    const track = { stop: vi.fn() };
    const getUserMedia = vi.fn(() => microphone.promise);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useVoiceLive(CONFIG, "azure_openai", "catalog-model", "eastus2", "alloy", onError),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith("token acquisition failed"),
    );
    expect(result.current.status).toBe("idle");
    expect(FakeAudioContext.instances[0].close).toHaveBeenCalledTimes(1);

    act(() => microphone.resolve({ getTracks: () => [track] }));
    await waitFor(() => expect(track.stop).toHaveBeenCalledTimes(1));
  });

  it("starts permission work in the click stack, cancels pending resources, and retries after failure", async () => {
    const token = deferred<string | null>();
    auth.getToken.mockReturnValue(token.promise);
    const firstTrack = { stop: vi.fn() };
    const secondTrack = { stop: vi.fn() };
    const getUserMedia = vi
      .fn()
      .mockResolvedValueOnce({ getTracks: () => [firstTrack] })
      .mockResolvedValueOnce({ getTracks: () => [secondTrack] });
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });

    const onError = vi.fn();
    const { result } = renderHook(() =>
      useVoiceLive(CONFIG, "azure_openai", "catalog-model", "eastus2", "alloy", onError),
    );
    expect(supportsVoiceLive()).toBe(true);

    act(() => {
      result.current.start();
    });
    expect(getUserMedia).toHaveBeenCalledTimes(1);
    expect(FakeAudioContext.instances).toHaveLength(1);
    expect(result.current.status).toBe("connecting");

    act(() => {
      result.current.stop();
    });
    await waitFor(() => expect(firstTrack.stop).toHaveBeenCalledTimes(1));
    expect(FakeAudioContext.instances[0].close).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe("idle");

    act(() => {
      result.current.start();
    });
    expect(getUserMedia).toHaveBeenCalledTimes(2);
    token.resolve("token");
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(FakeAudioContext.instances[1].close).not.toHaveBeenCalled();
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onerror?.());

    // The socket never reached onopen, so the browser-invisible handshake
    // failure (e.g. a 503 from the gateway) gets the actionable message
    // instead of the generic "connection error" text.
    expect(onError).toHaveBeenCalledWith(
      "Voice gateway or realtime service is unavailable. Try again.",
    );
    expect(result.current.status).toBe("idle");
    expect(secondTrack.stop).toHaveBeenCalledTimes(1);
    expect(FakeAudioContext.instances[1].close).toHaveBeenCalledTimes(1);
  });

  it("reports the actionable gateway message and fully cleans up on a pre-open close with no onerror", async () => {
    auth.getToken.mockResolvedValue("token");
    const track = { stop: vi.fn() };
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) },
    });
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useVoiceLive(CONFIG, "azure_openai", "catalog-model", "eastus2", "alloy", onError),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];

    // Some browsers fire only onclose (no onerror) for a rejected upgrade.
    act(() => socket.onclose?.());

    expect(onError).toHaveBeenCalledTimes(1);
    expect(onError).toHaveBeenCalledWith(
      "Voice gateway or realtime service is unavailable. Try again.",
    );
    expect(result.current.status).toBe("idle");
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(FakeAudioContext.instances[0].close).toHaveBeenCalledTimes(1);
    expect(socket.close).toHaveBeenCalled();
  });

  it("reports the pre-open message exactly once when both onerror and onclose fire", async () => {
    auth.getToken.mockResolvedValue("token");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useVoiceLive(CONFIG, "azure_openai", "catalog-model", "eastus2", "alloy", onError),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];

    act(() => {
      socket.onerror?.();
      socket.onclose?.();
    });

    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("reports the generic connection-error message once the session is live", async () => {
    auth.getToken.mockResolvedValue("token");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useVoiceLive(CONFIG, "azure_openai", "catalog-model", "eastus2", "alloy", onError),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onopen?.());
    await waitFor(() => expect(result.current.status).toBe("live"));

    act(() => socket.onerror?.());

    expect(onError).toHaveBeenCalledWith("Live voice connection error.");
    expect(result.current.status).toBe("idle");
  });

  it("does not report a spurious error when the user stops a live session cleanly", async () => {
    auth.getToken.mockResolvedValue("token");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    const onError = vi.fn();
    const { result } = renderHook(() =>
      useVoiceLive(CONFIG, "azure_openai", "catalog-model", "eastus2", "alloy", onError),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onopen?.());
    await waitFor(() => expect(result.current.status).toBe("live"));

    act(() => {
      result.current.stop();
    });
    // The browser eventually fires close for the socket we asked to close;
    // it must not be mistaken for an unexpected failure.
    act(() => socket.onclose?.());

    expect(onError).not.toHaveBeenCalled();
    expect(result.current.status).toBe("idle");
  });

  it("does not stop, cancel, or truncate Speech playback when interruption is disabled", async () => {
    auth.getToken.mockResolvedValue("token");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    const { result } = renderHook(() =>
      useVoiceLive(
        CONFIG,
        "speech_voice_live",
        null,
        null,
        "ignored",
        vi.fn(),
        null,
        [],
        DEFAULT_VOICE_SETTINGS,
        { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS, interruptResponse: false },
      ),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => {
      socket.readyState = FakeWebSocket.OPEN;
      socket.onopen?.();
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "response.created", response: { id: "r1" } }),
        }),
      );
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "response.audio.delta",
            response_id: "r1",
            item_id: "a1",
            content_index: 0,
            delta: "AQACAA==",
          }),
        }),
      );
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "input_audio_buffer.speech_started" }),
        }),
      );
    });

    expect(FakeAudioContext.instances[0].bufferSources[0].stop).not.toHaveBeenCalled();
    const sentTypes = socket.send.mock.calls.map(([frame]) => JSON.parse(frame).type);
    expect(sentTypes).not.toContain("response.cancel");
    expect(sentTypes).not.toContain("conversation.item.truncate");
  });

  it("cancels and truncates Speech playback on interruption and suppresses late deltas", async () => {
    auth.getToken.mockResolvedValue("token");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    const { result } = renderHook(() =>
      useVoiceLive(
        CONFIG,
        "speech_voice_live",
        null,
        null,
        "ignored",
        vi.fn(),
        null,
        [],
        DEFAULT_VOICE_SETTINGS,
        { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS, autoTruncate: false },
      ),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    const context = FakeAudioContext.instances[0];
    act(() => {
      socket.readyState = FakeWebSocket.OPEN;
      socket.onopen?.();
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "response.created", response: { id: "r1" } }),
        }),
      );
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "response.audio.delta",
            response_id: "r1",
            item_id: "a1",
            content_index: 2,
            delta: "AQACAA==",
          }),
        }),
      );
      context.currentTime = 0.05;
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "input_audio_buffer.speech_started" }),
        }),
      );
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "response.audio.delta", delta: "AQACAA==" }),
        }),
      );
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "response.audio_transcript.delta",
            delta: "late",
          }),
        }),
      );
    });

    expect(context.bufferSources[0].stop).toHaveBeenCalledTimes(1);
    expect(context.createBuffer).toHaveBeenCalledTimes(1);
    expect(result.current.assistantTranscript).toBe("");
    const sent = socket.send.mock.calls.map(([frame]) => JSON.parse(frame));
    expect(sent).toContainEqual({ type: "response.cancel" });
    expect(sent).toContainEqual({
      type: "conversation.item.truncate",
      item_id: "a1",
      content_index: 2,
      audio_end_ms: 50,
    });
  });

  it("relies on Speech auto-truncate instead of sending a duplicate truncate", async () => {
    auth.getToken.mockResolvedValue("token");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    const { result } = renderHook(() =>
      useVoiceLive(
        CONFIG,
        "speech_voice_live",
        null,
        null,
        "ignored",
        vi.fn(),
        null,
        [],
        DEFAULT_VOICE_SETTINGS,
        { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS, autoTruncate: true },
      ),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => {
      socket.readyState = FakeWebSocket.OPEN;
      socket.onopen?.();
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "response.created", response: { id: "r1" } }),
        }),
      );
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({
            type: "response.audio.delta",
            response_id: "r1",
            item_id: "a1",
            content_index: 0,
            delta: "AQACAA==",
          }),
        }),
      );
      FakeAudioContext.instances[0].currentTime = 0.05;
      socket.onmessage?.(
        new MessageEvent("message", {
          data: JSON.stringify({ type: "input_audio_buffer.speech_started" }),
        }),
      );
    });

    const sentTypes = socket.send.mock.calls.map(([frame]) => JSON.parse(frame).type);
    expect(sentTypes).toContain("response.cancel");
    expect(sentTypes).not.toContain("conversation.item.truncate");
  });

  it("applies a provider switch to the next connection without reconnecting the live one", async () => {
    auth.getToken.mockResolvedValue("token");
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [{ stop: vi.fn() }] }),
      },
    });
    const { result, rerender } = renderHook(
      ({ provider }: { provider: "azure_openai" | "speech_voice_live" }) =>
        useVoiceLive(CONFIG, provider, "catalog-model", "eastus2", "alloy", vi.fn()),
      {
        initialProps: {
          provider: "azure_openai" as "azure_openai" | "speech_voice_live",
        },
      },
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(FakeWebSocket.instances[0].url).toContain("provider=azure_openai");
    rerender({ provider: "speech_voice_live" });
    expect(FakeWebSocket.instances).toHaveLength(1);

    act(() => result.current.stop());
    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    expect(FakeWebSocket.instances[1].url).toContain("provider=speech_voice_live");
    expect(FakeWebSocket.instances[1].url).not.toContain("model=");
    expect(FakeWebSocket.instances[1].url).not.toContain("region=");
  });
});
