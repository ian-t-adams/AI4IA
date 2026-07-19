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
const telemetry = vi.hoisted(() => ({
  reportClientEvent: vi.fn(),
}));

vi.mock("./auth", () => ({
  isEntraEnabled: () => true,
  getApiAccessToken: () => auth.getToken(),
}));
vi.mock("./clientTelemetry", () => telemetry);

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
  // Real AudioContexts start "suspended" until a user-gesture-driven
  // resume(); voiceLive.ts's start() awaits that resume before proceeding,
  // so by the time any of this file's tests reach a running session the
  // context is already "running" -- matching every existing test's implicit
  // assumption. onstatechange models the real "statechange" event so tests
  // can drive AudioContext suspension (e.g. tab backgrounding) explicitly.
  // "interrupted" is Safari/WebKit's non-standard state (e.g. a phone call or
  // Siri taking the mic) that TypeScript's built-in AudioContextState type
  // doesn't know about -- included here so tests can drive it explicitly too.
  state: "running" | "suspended" | "interrupted" | "closed" = "running";
  onstatechange: (() => void) | null = null;
  resume = vi.fn(async () => {
    this.state = "running";
    this.onstatechange?.();
  });
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

// A minimal MediaStreamTrack double with real (not stubbed) addEventListener/
// removeEventListener semantics, so tests can prove the "ended"/"mute"
// listeners voiceLive.ts attaches in start() are both invoked and later
// detached — jsdom does not implement MediaStreamTrack at all, and the plain
// `{ stop }` objects used elsewhere in this file intentionally have no event
// target behavior (some browsers' tracks don't extend EventTarget either).
class FakeMediaStreamTrack {
  stop = vi.fn();
  muted = false;
  readyState: "live" | "ended" = "live";
  private listeners = new Map<string, Set<() => void>>();

  addEventListener(type: string, listener: () => void) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type)!.add(listener);
  }

  removeEventListener(type: string, listener: () => void) {
    this.listeners.get(type)?.delete(listener);
  }

  listenerCount(type: string): number {
    return this.listeners.get(type)?.size ?? 0;
  }

  dispatchEnded() {
    this.readyState = "ended";
    for (const listener of [...(this.listeners.get("ended") ?? [])]) listener();
  }

  dispatchMute() {
    this.muted = true;
    for (const listener of [...(this.listeners.get("mute") ?? [])]) listener();
  }
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
  onclose: ((event?: Pick<CloseEvent, "code" | "reason">) => void) | null = null;
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

    it("retains and reports the first safe protocol error exactly once before close", async () => {
      auth.getToken.mockResolvedValue("token");
      const track = { stop: vi.fn() };
      Object.defineProperty(navigator, "mediaDevices", {
        configurable: true,
        value: {
          getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }),
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
        socket.readyState = FakeWebSocket.OPEN;
        socket.onopen?.();
        socket.onmessage?.(
          new MessageEvent("message", {
            data: JSON.stringify({
              type: "error",
              error: {
                type: "invalid_request_error",
                code: "invalid_value",
                param: "session.voice",
                event_id: "evt-1",
                message:
                  "Rejected Bearer secret eyJaaaa.bbbbb.ccccc api_key=supersecret\u0000",
                transcript: "must never be retained",
              },
            }),
          }),
        );
        socket.onmessage?.(
          new MessageEvent("message", {
            data: JSON.stringify({
              type: "error",
              error: { message: "second error must not win" },
            }),
          }),
        );
        socket.onclose?.({ code: 1011, reason: "token=also-secret" });
      });

      expect(onError).toHaveBeenCalledTimes(1);
      expect(onError).toHaveBeenCalledWith(
        "Rejected Bearer [REDACTED] [REDACTED] api_key=[REDACTED] (type: invalid_request_error; code: invalid_value; param: session.voice; event_id: evt-1)",
      );
      expect(onError.mock.calls[0][0]).not.toContain("transcript");
      expect(track.stop).toHaveBeenCalledTimes(1);
      expect(FakeAudioContext.instances[0].close).toHaveBeenCalledTimes(1);
      expect(socket.close).toHaveBeenCalledTimes(1);
    });

    it("reports bounded and redacted close metadata when no protocol error exists", async () => {
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
        socket.readyState = FakeWebSocket.OPEN;
        socket.onopen?.();
        socket.onclose?.({ code: 1011, reason: "token=supersecret\nupstream closed" });
      });

      expect(onError).toHaveBeenCalledWith(
        "Live voice connection error. (code: 1011; reason: token=[REDACTED] upstream closed)",
      );
      expect(onError.mock.calls[0][0].length).toBeLessThanOrEqual(512);
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
      ({
        provider,
        model,
      }: {
        provider: "azure_openai" | "speech_voice_live";
        model: string;
      }) => useVoiceLive(CONFIG, provider, model, "eastus2", "alloy", vi.fn()),
      {
        initialProps: {
          provider: "azure_openai" as "azure_openai" | "speech_voice_live",
          model: "catalog-model",
        },
      },
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    expect(FakeWebSocket.instances[0].url).toContain("provider=azure_openai");
    rerender({ provider: "speech_voice_live", model: "gpt-4.1" });
    expect(FakeWebSocket.instances).toHaveLength(1);

    act(() => result.current.stop());
    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(2));
    expect(FakeWebSocket.instances[1].url).toContain("provider=speech_voice_live");
    expect(FakeWebSocket.instances[1].url).toContain("model=gpt-4.1");
    expect(FakeWebSocket.instances[1].url).not.toContain("region=");
  });

  // Regression: the mic track can die out from under an otherwise-healthy
  // socket (permission revoked mid-call, device unplugged, another app
  // taking exclusive access, lid close). Previously nothing observed this —
  // the socket stayed open and status stayed "live" — so the session
  // silently stopped hearing the user with no error and no way to know
  // reconnecting was needed.
  it("reports an actionable error and fully tears down when the microphone track ends unexpectedly", async () => {
    auth.getToken.mockResolvedValue("token");
    const track = new FakeMediaStreamTrack();
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
    act(() => socket.onopen?.());
    await waitFor(() => expect(result.current.status).toBe("live"));

    act(() => track.dispatchEnded());

    expect(onError).toHaveBeenCalledWith(
      "Microphone stopped providing audio (permission revoked or device disconnected). Reconnect to continue.",
    );
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(socket.close).toHaveBeenCalledTimes(1);
    expect(FakeAudioContext.instances[0].close).toHaveBeenCalledTimes(1);
    expect(telemetry.reportClientEvent).toHaveBeenCalledWith("microphone_error");
    await waitFor(() => expect(result.current.status).toBe("idle"));

    // A late, expected onclose for the socket we just asked to close must
    // not report a second, spurious error.
    act(() => socket.onclose?.());
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("does not leave a stale track listener that could fire after a clean stop", async () => {
    auth.getToken.mockResolvedValue("token");
    const track = new FakeMediaStreamTrack();
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
    act(() => FakeWebSocket.instances[0].onopen?.());
    await waitFor(() => expect(result.current.status).toBe("live"));

    act(() => {
      result.current.stop();
    });
    expect(result.current.status).toBe("idle");

    // stop() already called track.stop(); the browser can still fire "ended"
    // afterwards for a track that's already stopped. Because cleanupSession
    // removes the listener before stopping the track, this must be a no-op.
    act(() => track.dispatchEnded());
    expect(onError).not.toHaveBeenCalled();
  });

  // Regression: a track can go silent while staying readyState === "live" —
  // no "ended" event ever fires for this — via the browser flipping `muted`
  // true and firing "mute" (OS-level privacy toggle, another app grabbing
  // exclusive device access, audio-routing hiccups). Previously nothing
  // observed this either, so a muted-mid-call track reproduced "voice no
  // longer hears them" with status staying "live" and the socket looking
  // perfectly healthy throughout.
  it("reports an actionable error and fully tears down when the microphone track is muted mid-session", async () => {
    auth.getToken.mockResolvedValue("token");
    const track = new FakeMediaStreamTrack();
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
    act(() => socket.onopen?.());
    await waitFor(() => expect(result.current.status).toBe("live"));

    act(() => track.dispatchMute());

    expect(onError).toHaveBeenCalledWith(
      "Microphone stopped receiving audio (it may have been muted by your system or another app). Reconnect to continue.",
    );
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(socket.close).toHaveBeenCalledTimes(1);
    expect(FakeAudioContext.instances[0].close).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current.status).toBe("idle"));

    // A late, expected onclose for the socket we just asked to close must
    // not report a second, spurious error.
    act(() => socket.onclose?.());
    expect(onError).toHaveBeenCalledTimes(1);
  });

  it("does not leave a stale mute listener that could fire after a clean stop", async () => {
    auth.getToken.mockResolvedValue("token");
    const track = new FakeMediaStreamTrack();
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
    act(() => FakeWebSocket.instances[0].onopen?.());
    await waitFor(() => expect(result.current.status).toBe("live"));

    act(() => {
      result.current.stop();
    });
    expect(result.current.status).toBe("idle");

    // stop() already called track.stop(); a device driver can still fire
    // "mute" afterwards for a track that's already stopped. Because
    // cleanupSession removes the listener before stopping the track, this
    // must be a no-op.
    act(() => track.dispatchMute());
    expect(onError).not.toHaveBeenCalled();
  });

  // Regression: unlike "ended", `muted` can already be true the moment
  // getUserMedia resolves (another app already held exclusive access, or the
  // OS mic toggle was off when permission was granted). No "mute"
  // *transition* event ever fires for that case — there's nothing to
  // transition from — so relying on the event alone would let a
  // dead-on-arrival mic reach "live" and stay there for the whole call.
  it("reports an actionable error immediately when the microphone track starts already muted", async () => {
    auth.getToken.mockResolvedValue("token");
    const track = new FakeMediaStreamTrack();
    track.muted = true;
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

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith(
        "Microphone stopped receiving audio (it may have been muted by your system or another app). Reconnect to continue.",
      ),
    );
    expect(track.stop).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(FakeAudioContext.instances[0]?.close).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.status).toBe("idle"));
    // The session must never have been allowed to reach "live" with a mic
    // that was already known to be dead.
    expect(result.current.status).not.toBe("live");
  });

  // Regression (independent re-review, MEDIUM): the "ended"/"mute" listeners
  // are only attached once getUserMedia(), ctx.resume(), and
  // buildSubprotocols() (an auth round-trip) have ALL settled, followed by
  // addModule(). A track that already ended somewhere in that gap won't
  // refire "ended" for a listener attached afterward -- that only catches a
  // *future* transition. The post-wiring readyState check must catch it
  // anyway by querying the track's current state directly, so a track that
  // died during the startup gap is still caught before ever reaching "live".
  it("catches a microphone track that already ended during the auth/addModule startup gap", async () => {
    const token = deferred<string | null>();
    auth.getToken.mockReturnValue(token.promise);
    const track = new FakeMediaStreamTrack();
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
    // getUserMedia already resolved (it has no auth dependency), but the
    // auth round-trip is still pending, so no "ended" listener has been
    // attached to the track yet -- exactly the startup gap the finding
    // describes.
    await waitFor(() => expect(track.listenerCount("ended")).toBe(0));

    // The track ends while nothing is listening for it. Nothing re-fires
    // "ended" once a listener is attached later -- only the track's current
    // readyState reflects that this already happened.
    track.readyState = "ended";

    act(() => token.resolve("token"));

    await waitFor(() =>
      expect(onError).toHaveBeenCalledWith(
        "Microphone stopped providing audio (permission revoked or device disconnected). Reconnect to continue.",
      ),
    );
    await waitFor(() => expect(FakeWebSocket.instances[0]?.close).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(FakeAudioContext.instances[0]?.close).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(result.current.status).toBe("idle"));
    // The session must never have been allowed to reach "live" with a mic
    // that was already known to be dead.
    expect(result.current.status).not.toBe("live");
  });

  // Regression: Chrome/Safari can suspend an active AudioContext purely from
  // backgrounding the tab -- even with a live, unmuted mic track and a
  // healthy socket -- silently halting the capture worklet with no other
  // observable signal. A brief glance away and back must self-heal silently
  // rather than tearing the whole session down.
  it("self-heals silently when the AudioContext resumes within the grace period", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      auth.getToken.mockResolvedValue("token");
      const track = new FakeMediaStreamTrack();
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
      act(() => socket.onopen?.());
      await waitFor(() => expect(result.current.status).toBe("live"));

      const ctx = FakeAudioContext.instances[0];
      // Drive the "browser recovered" notification explicitly, independently
      // of resume()'s own promise -- a real UA's resume() resolving and its
      // next genuine "statechange" event are two separate (if usually
      // near-simultaneous) things, so the fake shouldn't conflate them here.
      ctx.resume = vi.fn(async () => {});

      // The tab gets backgrounded; the UA suspends the context.
      act(() => {
        ctx.state = "suspended";
        ctx.onstatechange?.();
      });
      expect(ctx.resume).toHaveBeenCalledTimes(1);
      expect(onError).not.toHaveBeenCalled();

      // The user glances back well within the grace period; the UA reports
      // the context running again.
      act(() => {
        ctx.state = "running";
        ctx.onstatechange?.();
      });

      // Advancing all the way past the *original* 4s grace period must not
      // trigger the fatal path -- proving the recovery timer was actually
      // cleared, not just "hasn't fired yet".
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(onError).not.toHaveBeenCalled();
      expect(result.current.status).toBe("live");
      expect(ctx.close).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  // Regression: if the suspension outlasts the grace period -- the tab stays
  // backgrounded, or the browser defers/ignores resume() while hidden -- the
  // session must not stay silently stuck "live" forever. It must report an
  // explicit, actionable error and fully tear down, the same as a dead or
  // muted mic.
  it("reports an actionable error and fully tears down when AudioContext suspension outlasts the grace period", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      auth.getToken.mockResolvedValue("token");
      const track = new FakeMediaStreamTrack();
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
      act(() => socket.onopen?.());
      await waitFor(() => expect(result.current.status).toBe("live"));

      const ctx = FakeAudioContext.instances[0];
      // Simulate a browser that defers/ignores resume() entirely while the
      // tab stays hidden -- resume() never actually flips state back.
      ctx.resume = vi.fn(async () => {});

      act(() => {
        ctx.state = "suspended";
        ctx.onstatechange?.();
      });
      expect(onError).not.toHaveBeenCalled();

      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(onError).toHaveBeenCalledWith(
        "Live voice paused because the browser suspended audio processing (often from backgrounding the tab). Reconnect to continue.",
      );
      expect(track.stop).toHaveBeenCalledTimes(1);
      expect(socket.close).toHaveBeenCalledTimes(1);
      expect(ctx.close).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(result.current.status).toBe("idle"));

      // A late, expected onclose for the socket we just asked to close must
      // not report a second, spurious error.
      act(() => socket.onclose?.());
      expect(onError).toHaveBeenCalledTimes(1);
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not leave a stale AudioContext suspend-recovery timer that could fire after a clean stop", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      auth.getToken.mockResolvedValue("token");
      const track = new FakeMediaStreamTrack();
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
      act(() => socket.onopen?.());
      await waitFor(() => expect(result.current.status).toBe("live"));

      const ctx = FakeAudioContext.instances[0];
      ctx.resume = vi.fn(async () => {});
      act(() => {
        ctx.state = "suspended";
        ctx.onstatechange?.();
      });

      act(() => {
        result.current.stop();
      });
      expect(result.current.status).toBe("idle");

      // The pending grace-period timer must not survive a clean, user-
      // initiated stop -- it must never fire a stale, spurious error for a
      // session that is already gone.
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });
      expect(onError).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  // Regression (independent re-review, MEDIUM): onstatechange is assigned
  // only after the same auth/addModule startup gap as the track listeners
  // above, and only reacts to a *future* transition. A context that browsers
  // defer/ignore resume() for while the tab stays hidden can already be
  // stuck "suspended" the moment onstatechange is finally assigned, with no
  // further transition ever occurring to trigger it -- previously that meant
  // the recovery grace period (and eventual fatal teardown) never started at
  // all, so the client could report a live/open socket with an AudioContext
  // that had been silently dead since before the session even connected.
  it("starts the suspend-recovery grace period immediately if the AudioContext is already suspended the moment monitoring is wired up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const token = deferred<string | null>();
      auth.getToken.mockReturnValue(token.promise);
      const track = new FakeMediaStreamTrack();
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
      // ctx.resume() already ran synchronously (per the fake's default
      // behavior) and flipped state to "running". Simulate a browser that
      // defers/ignores resume() while the tab is hidden: the context is
      // (still, or again) "suspended" throughout the auth/addModule startup
      // gap, and a neutered resume() means nothing will ever bring it back
      // on its own.
      const ctx = FakeAudioContext.instances[0];
      ctx.resume = vi.fn(async () => {});
      ctx.state = "suspended";

      // onstatechange has not been assigned yet -- the auth round-trip is
      // still pending -- so there is nothing to observe this pre-existing
      // suspension until wiring completes a little later. No transition
      // into "suspended" ever happens in this test; it starts that way.
      expect(ctx.onstatechange).toBeNull();

      act(() => token.resolve("token"));
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
      act(() => FakeWebSocket.instances[0].onopen?.());
      await waitFor(() => expect(result.current.status).toBe("live"));

      // The grace period must already be running from the immediate
      // post-wiring evaluation, not waiting on a transition that will never
      // come -- advancing past it alone (with no manual onstatechange call)
      // must reach the same fatal, fully-torn-down outcome as an observed
      // mid-session suspension.
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(onError).toHaveBeenCalledWith(
        "Live voice paused because the browser suspended audio processing (often from backgrounding the tab). Reconnect to continue.",
      );
      expect(track.stop).toHaveBeenCalledTimes(1);
      expect(FakeWebSocket.instances[0].close).toHaveBeenCalledTimes(1);
      expect(ctx.close).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(result.current.status).toBe("idle"));
    } finally {
      vi.useRealTimers();
    }
  });

  // Regression (final acceptance review, MEDIUM): Safari/WebKit's
  // non-standard "interrupted" AudioContext state (e.g. a phone call or Siri
  // taking the mic) used to be ignored entirely -- handleContextStateChange
  // only ever matched "running" or "closed" and no-op'd on anything else it
  // didn't recognize as a real transition trigger, when in fact it already
  // falls through to the same bounded resume+grace-period recovery as
  // "suspended". This proves that path handles "interrupted" the same way,
  // and eventually fails safe (actionable error + full teardown) if it
  // outlasts the grace period, the same as an ordinary suspension would.
  it("reports an actionable error and fully tears down when an interrupted AudioContext (Safari/WebKit) outlasts the grace period", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      auth.getToken.mockResolvedValue("token");
      const track = new FakeMediaStreamTrack();
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
      act(() => socket.onopen?.());
      await waitFor(() => expect(result.current.status).toBe("live"));

      const ctx = FakeAudioContext.instances[0];
      // Simulate a browser that defers/ignores resume() entirely while the
      // interruption (e.g. a phone call) is ongoing -- resume() never
      // actually flips state back.
      ctx.resume = vi.fn(async () => {});

      act(() => {
        ctx.state = "interrupted";
        ctx.onstatechange?.();
      });
      expect(ctx.resume).toHaveBeenCalledTimes(1);
      expect(onError).not.toHaveBeenCalled();

      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(onError).toHaveBeenCalledWith(
        "Live voice paused because the browser suspended audio processing (often from backgrounding the tab). Reconnect to continue.",
      );
      expect(track.stop).toHaveBeenCalledTimes(1);
      expect(socket.close).toHaveBeenCalledTimes(1);
      expect(ctx.close).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(result.current.status).toBe("idle"));
    } finally {
      vi.useRealTimers();
    }
  });

  it("starts the suspend-recovery grace period immediately if the AudioContext is already interrupted (Safari/WebKit) the moment monitoring is wired up", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const token = deferred<string | null>();
      auth.getToken.mockReturnValue(token.promise);
      const track = new FakeMediaStreamTrack();
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
      // Simulate a browser that is already mid-interruption (e.g. a phone
      // call started right as the session was opening) throughout the
      // auth/addModule startup gap, with a neutered resume() so nothing
      // brings it back on its own.
      const ctx = FakeAudioContext.instances[0];
      ctx.resume = vi.fn(async () => {});
      ctx.state = "interrupted";

      // onstatechange has not been assigned yet -- the auth round-trip is
      // still pending -- so there is nothing to observe this pre-existing
      // interruption until wiring completes a little later. No transition
      // into "interrupted" ever happens in this test; it starts that way.
      expect(ctx.onstatechange).toBeNull();

      act(() => token.resolve("token"));
      await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
      act(() => FakeWebSocket.instances[0].onopen?.());
      await waitFor(() => expect(result.current.status).toBe("live"));

      // The grace period must already be running from the immediate
      // post-wiring evaluation, not waiting on a transition that will never
      // come -- advancing past it alone (with no manual onstatechange call)
      // must reach the same fatal, fully-torn-down outcome as an observed
      // mid-session interruption.
      await act(async () => {
        vi.advanceTimersByTime(4000);
      });

      expect(onError).toHaveBeenCalledWith(
        "Live voice paused because the browser suspended audio processing (often from backgrounding the tab). Reconnect to continue.",
      );
      expect(track.stop).toHaveBeenCalledTimes(1);
      expect(FakeWebSocket.instances[0].close).toHaveBeenCalledTimes(1);
      expect(ctx.close).toHaveBeenCalledTimes(1);
      await waitFor(() => expect(result.current.status).toBe("idle"));
    } finally {
      vi.useRealTimers();
    }
  });

  it("deterministically tears down the microphone, audio context, and socket on unmount while live", async () => {
    auth.getToken.mockResolvedValue("token");
    const track = new FakeMediaStreamTrack();
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: { getUserMedia: vi.fn().mockResolvedValue({ getTracks: () => [track] }) },
    });
    const { result, unmount } = renderHook(() =>
      useVoiceLive(CONFIG, "azure_openai", "catalog-model", "eastus2", "alloy", vi.fn()),
    );

    act(() => {
      result.current.start();
    });
    await waitFor(() => expect(FakeWebSocket.instances).toHaveLength(1));
    const socket = FakeWebSocket.instances[0];
    act(() => socket.onopen?.());
    await waitFor(() => expect(result.current.status).toBe("live"));

    unmount();

    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(FakeAudioContext.instances[0].close).toHaveBeenCalledTimes(1);
    expect(socket.close).toHaveBeenCalledTimes(1);
    // The track listeners must also be detached so a post-unmount "ended" or
    // "mute" event (the component is gone, but the OS/browser event can still
    // fire) can never touch React state on an unmounted hook.
    expect(track.listenerCount("ended")).toBe(0);
    expect(track.listenerCount("mute")).toBe(0);
  });
});
