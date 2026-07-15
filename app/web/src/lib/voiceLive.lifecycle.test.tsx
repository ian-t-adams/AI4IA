// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { supportsVoiceLive, useVoiceLive } from "./voiceLive";

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
  createBuffer = vi.fn();
  createBufferSource = vi.fn();

  constructor() {
    FakeAudioContext.instances.push(this);
  }
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
      useVoiceLive(CONFIG, "catalog-model", "alloy", onError),
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
      useVoiceLive(CONFIG, "catalog-model", "alloy", onError),
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

    expect(onError).toHaveBeenCalledWith("Live voice connection error.");
    expect(result.current.status).toBe("idle");
    expect(secondTrack.stop).toHaveBeenCalledTimes(1);
    expect(FakeAudioContext.instances[1].close).toHaveBeenCalledTimes(1);
  });
});
