// @vitest-environment jsdom
import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useCallback, useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DisplayMessage } from "./MessageList";
import { MessageList } from "./MessageList";
import { Composer } from "./Composer";
import {
  InlineVoiceLiveStatus,
  mergeDisplayMessages,
  useInlineVoiceLive,
  voiceMessagesForSession,
} from "./InlineVoiceLive";
import { ApiError } from "@/lib/api";
import type { VoiceTurnInput } from "@/lib/types";
import {
  DEFAULT_VOICE,
  DEFAULT_VOICE_SETTINGS,
  type RealtimeVoice,
  type VoiceProviderId,
  type VoiceLiveController,
  type VoiceSessionSettings,
} from "@/lib/voiceLive";

const mocks = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  toggle: vi.fn(),
  useVoiceLive: vi.fn(),
  recorderToggle: vi.fn(),
}));

vi.mock("@/lib/voiceLive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/voiceLive")>();
  return { ...actual, useVoiceLive: mocks.useVoiceLive };
});

vi.mock("@/lib/voice", () => ({
  useVoiceRecorder: () => ({
    recording: false,
    transcribing: false,
    supported: false,
    toggle: mocks.recorderToggle,
  }),
  useSpeechPlayback: () => ({
    activeId: null,
    busyId: null,
    toggle: vi.fn(),
  }),
}));

const CONFIG = {
  enabled: true,
  wsUrl: "wss://api.example.test/api/voice/live",
  devUser: "dev",
  toolsAvailable: true,
};

const AGENTS = [
  {
    name: "analyst",
    displayName: "Analyst",
    description: "Finds the signal",
    enabled: true,
  },
];

let controller: VoiceLiveController;

function makeController(
  overrides: Partial<VoiceLiveController> = {},
): VoiceLiveController {
  return {
    status: "idle",
    active: false,
    supported: true,
    userTranscript: "",
    assistantTranscript: "",
    toolActivity: "",
    turns: [],
    listening: false,
    speaking: false,
    start: mocks.start,
    stop: mocks.stop,
    toggle: mocks.toggle,
    ...overrides,
  };
}

function Harness({
  persist = vi.fn(async () => {}),
  onSend = vi.fn(),
  ensureSession = async () => "session-1",
  activeSessionId = null,
  providerId,
  voice: voiceOverride,
  settings: settingsOverride,
  tools: toolsOverride,
}: {
  persist?: (
    sessionId: string,
    conversationId: string,
    turns: VoiceTurnInput[],
    isStillValid: () => boolean,
  ) => Promise<void>;
  onSend?: (text: string) => void;
  ensureSession?: () => Promise<string>;
  activeSessionId?: string | null;
  providerId?: VoiceProviderId;
  voice?: RealtimeVoice;
  settings?: VoiceSessionSettings;
  tools?: boolean;
}) {
  const [persistedMessages, setPersistedMessages] = useState<DisplayMessage[]>([]);
  const persistConversation = useCallback(
    async (
      sessionId: string,
      conversationId: string,
      turns: VoiceTurnInput[],
      isStillValid: () => boolean,
    ) => {
      await persist(sessionId, conversationId, turns, isStillValid);
      setPersistedMessages(
        turns.map((turn, index) => ({
          id: `persisted-${index}`,
          role: turn.role,
          content: turn.text,
          source: "voice",
          agent: turn.role === "assistant" ? "analyst" : null,
        })),
      );
    },
    [persist],
  );
  const voice = useInlineVoiceLive({
    config: CONFIG,
    providerId,
    model: "catalog-realtime-model",
    agent: "analyst",
    agents: AGENTS,
    history: [{ role: "user", text: "Earlier text turn" }],
    voice: voiceOverride,
    settings: settingsOverride,
    tools: toolsOverride,
    activeSessionId,
    ensureSession,
    persistConversation,
  });

  return (
    <>
      <MessageList messages={[...persistedMessages, ...voice.messages]} />
      <InlineVoiceLiveStatus voice={voice} />
      <Composer
        disabled={false}
        streaming={false}
        agents={AGENTS}
        documents={[]}
        uploading={false}
        capabilities={{
          ingestPath: "library",
          maxBytes: 1_000_000,
          maxPerUserDocuments: 100,
          maxPerSessionDocuments: 8,
          extensions: [".pdf", ".mp3"],
          mimeTypes: ["application/pdf", "audio/*"],
          modalities: ["document", "audio"],
        }}
        onSend={onSend}
        onStop={vi.fn()}
        onUpload={vi.fn()}
        onRemoveDocument={vi.fn()}
        voiceLive={{
          active: voice.active,
          supported: voice.supported,
          connecting: voice.phase === "connecting",
          ending: voice.phase === "ending",
          saving: voice.saving,
          saveBlocked: Boolean(voice.persistenceError),
          retrying: Boolean(voice.error),
          start: voice.start,
          stop: voice.stop,
        }}
      />
    </>
  );
}

beforeEach(() => {
  Object.defineProperty(Element.prototype, "scrollIntoView", {
    configurable: true,
    value: vi.fn(),
  });
  controller = makeController();
  mocks.useVoiceLive.mockImplementation(() => controller);
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("inline Voice Live chat", () => {
  it("keeps interleaved typed and spoken turns in chronological order", () => {
    const merged = mergeDisplayMessages(
      [
        {
          id: "typed-later",
          role: "user",
          content: "Typed later",
          createdAt: "2026-07-15T12:00:02Z",
        },
      ],
      [
        {
          id: "voice-earlier",
          role: "assistant",
          content: "Spoken earlier",
          createdAt: "2026-07-15T12:00:01Z",
          source: "voice",
        },
      ],
    );

    expect(merged.map((message) => message.id)).toEqual([
      "voice-earlier",
      "typed-later",
    ]);
  });

  it("shows live turns only in the chat captured when voice started", () => {
    const turns: DisplayMessage[] = [
      { id: "voice", role: "user", content: "Bound turn", source: "voice" },
    ];

    expect(voiceMessagesForSession(turns, "chat-a", "chat-a")).toBe(turns);
    expect(voiceMessagesForSession(turns, "chat-a", "chat-b")).toEqual([]);
    expect(voiceMessagesForSession(turns, null, "chat-b")).toEqual([]);
    expect(voiceMessagesForSession(turns, null, null)).toBe(turns);
  });

  it("starts on the microphone click without opening a dialog or replacing the transcript", async () => {
    render(<Harness />);

    const transcript = screen.getByRole("log", { name: "Conversation" });
    await userEvent.click(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    );

    expect(mocks.start).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("log", { name: "Conversation" })).toBe(transcript);
  });

  it("passes the active agent, realtime model, history, defaults, and tool posture", () => {
    render(<Harness />);

    const args = mocks.useVoiceLive.mock.calls.at(-1);
    expect(args?.[1]).toBe("azure_openai");
    expect(args?.[2]).toBe("catalog-realtime-model");
    expect(args?.[3]).toBeNull();
    expect(args?.[4]).toBe(DEFAULT_VOICE);
    expect(args?.[6]).toBe("analyst");
    expect(args?.[7]).toEqual([{ role: "user", text: "Earlier text turn" }]);
    expect(args?.[8]).toEqual(DEFAULT_VOICE_SETTINGS);
    expect(args?.[10]).toBe(false);
  });

  it("threads a custom voice, session settings, and the tools opt-in into useVoiceLive", () => {
    const customSettings: VoiceSessionSettings = {
      ...DEFAULT_VOICE_SETTINGS,
      temperature: 0.4,
    };
    render(<Harness voice="marin" settings={customSettings} tools={true} />);

    const args = mocks.useVoiceLive.mock.calls.at(-1);
    expect(args?.[4]).toBe("marin");
    expect(args?.[8]).toEqual(customSettings);
    expect(args?.[10]).toBe(true);
  });

  it("shows inline connecting, listening, thinking, and speaking phases", () => {
    const { rerender } = render(<Harness />);

    controller = makeController({ status: "connecting", active: true });
    rerender(<Harness />);
    expect(screen.getByText("Connecting")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    ).toHaveAttribute("aria-busy", "true");

    controller = makeController({
      status: "live",
      active: true,
      listening: true,
    });
    rerender(<Harness />);
    expect(screen.getByText("Listening")).toBeInTheDocument();

    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Question",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    rerender(<Harness />);
    expect(screen.getByText("Thinking")).toBeInTheDocument();

    controller = makeController({
      status: "live",
      active: true,
      speaking: true,
    });
    rerender(<Harness />);
    expect(screen.getByText("Speaking")).toBeInTheDocument();
  });

  it("keeps the composer usable for typed sends during live voice", async () => {
    const onSend = vi.fn();
    controller = makeController({ status: "live", active: true });
    render(<Harness onSend={onSend} />);

    const composer = screen.getByRole("combobox", { name: "Message" });
    await userEvent.type(composer, "Typed while listening{Enter}");

    expect(onSend).toHaveBeenCalledWith("Typed while listening");
    expect(screen.getByText("You can keep typing in this chat.")).toBeInTheDocument();
  });

  it("stops, persists finalized turns once, removes local duplicates, and returns idle", async () => {
    const persist = vi.fn(async () => {});
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Hello",
          pending: false,
          streaming: false,
          tool: "",
        },
        {
          id: "a1",
          role: "assistant",
          text: "Hi there",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(<Harness persist={persist} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    await waitFor(() =>
      expect(persist).toHaveBeenCalledWith(
        "session-1",
        expect.any(String),
        [
          { role: "user", text: "Hello" },
          { role: "assistant", text: "Hi there" },
        ],
        expect.any(Function),
      ),
    );
    expect(mocks.stop).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.getAllByText("Hello")).toHaveLength(1));
    expect(screen.getAllByText("Hi there")).toHaveLength(1);

    controller = makeController({ status: "idle", active: false });
    rerender(<Harness persist={persist} />);
    await waitFor(() => expect(persist).toHaveBeenCalledTimes(1));
    expect(screen.getByText("Voice Live ready")).toBeInTheDocument();
  });

  it("prevents a new voice cycle until the previous transcript finishes saving", async () => {
    let finishSave!: () => void;
    const persist = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishSave = resolve;
        }),
    );
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Save me",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(<Harness persist={persist} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    controller = makeController();
    rerender(<Harness persist={persist} />);

    expect(
      screen.getByRole("button", { name: "Saving live voice transcript" }),
    ).toBeDisabled();
    expect(mocks.start).not.toHaveBeenCalled();

    act(() => finishSave());
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Start live voice conversation" }),
      ).toBeEnabled(),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    );
    expect(mocks.start).toHaveBeenCalledTimes(1);
  });

  it("shows a one-click inline retry after a connection error", async () => {
    let reportError: ((message: string) => void) | undefined;
    mocks.useVoiceLive.mockImplementation(
      (
        _config: unknown,
        _providerId: unknown,
        _model: unknown,
        _region: unknown,
        _voice: unknown,
        onError: (message: string) => void,
      ) => {
        reportError = onError;
        return controller;
      },
    );
    render(<Harness />);

    act(() => reportError?.("Live voice connection error."));
    expect(screen.getByText("Live voice connection error.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(mocks.start).toHaveBeenCalledTimes(1);
  });

  it("denied microphone permission / a gateway failure with no turns never creates a session", async () => {
    const ensureSession = vi.fn().mockRejectedValue(new Error("store unavailable"));
    render(<Harness ensureSession={ensureSession} />);

    const microphone = screen.getByRole("button", {
      name: "Start live voice conversation",
    });
    await userEvent.click(microphone);
    // No finalized turns exist yet, so ensureSession is never called on start —
    // it is only ever invoked lazily by persist() once a turn needs saving.
    expect(ensureSession).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Retry saving" })).toBeNull();

    // A bare retry with still no turns behaves identically: no session created.
    await userEvent.click(microphone);
    expect(mocks.start).toHaveBeenCalledTimes(2);
    expect(ensureSession).not.toHaveBeenCalled();
  });

  it("switching providers without a finalized turn does not create an empty session", () => {
    const ensureSession = vi.fn().mockResolvedValue("session-1");
    const { rerender } = render(
      <Harness providerId="azure_openai" ensureSession={ensureSession} />,
    );

    rerender(
      <Harness providerId="speech_voice_live" ensureSession={ensureSession} />,
    );

    expect(ensureSession).not.toHaveBeenCalled();
    expect(mocks.start).not.toHaveBeenCalled();
    expect(mocks.useVoiceLive).toHaveBeenLastCalledWith(
      CONFIG,
      "speech_voice_live",
      "catalog-realtime-model",
      null,
      DEFAULT_VOICE,
      expect.any(Function),
      "analyst",
      [{ role: "user", text: "Earlier text turn" }],
      DEFAULT_VOICE_SETTINGS,
      expect.any(Object),
      false,
      null,
    );
  });

  it("creates exactly one session on the first finalized turn and does not duplicate on repeated stop clicks", async () => {
    const ensureSession = vi.fn().mockResolvedValue("session-1");
    const persist = vi.fn(async () => {});
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Keep this turn",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    render(<Harness ensureSession={ensureSession} persist={persist} />);

    const stopButton = screen.getByRole("button", {
      name: "Stop live voice conversation",
    });
    // Two rapid stop clicks (e.g. an impatient double-click) must not create
    // two sessions or persist twice — persist()'s in-flight/persisted guards
    // dedupe them to a single ensureSession + persistConversation call.
    await userEvent.click(stopButton);
    await userEvent.click(stopButton);

    await waitFor(() => expect(ensureSession).toHaveBeenCalledTimes(1));
    expect(persist).toHaveBeenCalledTimes(1);
    expect(mocks.stop).toHaveBeenCalledTimes(2);
  });

  it("binds finalized turns to the chat that was active when voice started", async () => {
    const ensureSession = vi.fn(async () => "different-session");
    const persist = vi.fn(async () => {});
    controller = makeController();
    const { rerender } = render(
      <Harness
        activeSessionId="original-session"
        ensureSession={ensureSession}
        persist={persist}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    );
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Stay with the original chat",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    rerender(
      <Harness
        activeSessionId="different-session"
        ensureSession={ensureSession}
        persist={persist}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );

    await waitFor(() =>
      expect(persist).toHaveBeenCalledWith(
        "original-session",
        expect.any(String),
        [{ role: "user", text: "Stay with the original chat" }],
        expect.any(Function),
      ),
    );
    expect(ensureSession).not.toHaveBeenCalled();
  });

  it("retries session creation after a persistence-time creation failure without duplicating", async () => {
    const ensureSession = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new Error("save create failed"))
      .mockResolvedValue("session-1");
    const persist = vi.fn(async () => {});
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Keep this turn",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    render(<Harness ensureSession={ensureSession} persist={persist} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Retry saving" })).toBeEnabled(),
    );
    expect(ensureSession).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByRole("button", { name: "Retry saving" }));
    await waitFor(() => expect(persist).toHaveBeenCalledTimes(1));
    expect(ensureSession).toHaveBeenCalledTimes(2);
  });

  // Regression: ensureSession()/persistConversation() are plain fetches with
  // no AbortController plumbed through them, so a hung request (dropped
  // connection, backend stall) previously left `saving` true forever, which
  // permanently disabled the Composer's voice button and permanently blocked
  // chat/session navigation with no feedback at all.
  it("resolves a stuck save to a diagnosable error after the bounded timeout instead of hanging forever", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const persist = vi.fn(() => new Promise<void>(() => {}));
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Stuck turn",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(<Harness persist={persist} />);

    await user.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    controller = makeController();
    rerender(<Harness persist={persist} />);

    expect(
      screen.getByRole("button", { name: "Saving live voice transcript" }),
    ).toBeDisabled();

    await act(async () => {
      vi.advanceTimersByTime(20_000);
    });

    expect(
      screen.getByText(
        "Saving the voice transcript is taking too long. Retry, or stop waiting to continue.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry saving" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop waiting" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Retry saving the voice transcript below" }),
    ).toBeInTheDocument();
    expect(mocks.start).not.toHaveBeenCalled();

    vi.useRealTimers();
  });

  // Regression: timing out only means the UI gave up *waiting* to report a
  // diagnosable state -- the underlying save is never aborted (no
  // AbortSignal is wired through ensureSession/persistConversation), so it
  // can still complete afterward. A prior "settled" latch in finish() made
  // that first (timeout) call permanent, silently discarding a later real
  // outcome for the SAME still-current attempt: a save that truly succeeded
  // slowly left the user staring at a permanent error/lock with no way to
  // know their data was actually safe. finish() must stay reentrant and gate
  // only on attempt currency, not on "have I already reported once."
  it("clears a timeout-driven persistence error once the underlying save actually completes for the same attempt", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    let resolvePersist: (() => void) | undefined;
    const persist = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          resolvePersist = resolve;
        }),
    );
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Slow but successful turn",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(<Harness persist={persist} />);

    await user.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    controller = makeController();
    rerender(<Harness persist={persist} />);

    await act(async () => {
      vi.advanceTimersByTime(20_000);
    });

    // Same timeout-driven error state as the test above.
    expect(
      screen.getByText(
        "Saving the voice transcript is taking too long. Retry, or stop waiting to continue.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry saving" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop waiting" })).toBeEnabled();

    // The real save -- never cancelled, just slow -- now finally resolves.
    // Flush a generous number of microtask ticks (the promise chain hops
    // through persistConversation -> the session .then() -> finish()) so the
    // late completion has every chance to be applied before asserting.
    await act(async () => {
      resolvePersist?.();
      for (let i = 0; i < 20; i += 1) {
        await Promise.resolve();
      }
    });

    expect(
      screen.queryByText(
        "Saving the voice transcript is taking too long. Retry, or stop waiting to continue.",
      ),
    ).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry saving" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Stop waiting" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    ).toBeEnabled();
    // The turn the timeout almost lost track of actually landed, exactly
    // once -- not silently dropped and not duplicated.
    expect(screen.getByText("Slow but successful turn")).toBeInTheDocument();

    vi.useRealTimers();
  });

  // Regression: discardPersistence() is the user's explicit escape hatch for
  // a stuck/failed save. It must unlock the UI synchronously — it cannot
  // wait on the network request it is abandoning.
  it("discardPersistence unlocks saving/persistenceError/exitLocked immediately even with a still in-flight save", async () => {
    const persist = vi.fn(() => new Promise<void>(() => {}));
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Abandon me",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(<Harness persist={persist} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    controller = makeController();
    rerender(<Harness persist={persist} />);
    expect(
      screen.getByRole("button", { name: "Saving live voice transcript" }),
    ).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Stop waiting" }));

    expect(screen.getByText("Voice Live ready")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop waiting" })).toBeNull();
    expect(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    ).toBeEnabled();

    await userEvent.click(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    );
    expect(mocks.start).toHaveBeenCalledTimes(1);
  });

  // Regression: production correlation showed session PATCH/POST calls
  // failing fast with HTTP 500 (a backend type-mismatch defect, tracked
  // separately). A fast failure must resolve exactly like a hung request --
  // persistenceError set, saving cleared, Retry/Discard available -- without
  // waiting on the 20s timeout, and it must never gate the live connection's
  // own teardown, which stop() already fires unconditionally before persist()
  // settles either way.
  it("surfaces a fast-failing save (e.g. a session-update 500) as persistenceError immediately, without waiting on the timeout", async () => {
    const persist = vi.fn(() =>
      Promise.reject(new ApiError(500, "Internal Server Error")),
    );
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Fails fast",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(<Harness persist={persist} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    // The underlying WS/mic/AudioContext teardown is unconditional: it runs
    // synchronously inside stop() and never waits on the background save.
    expect(mocks.stop).toHaveBeenCalledTimes(1);

    controller = makeController();
    rerender(<Harness persist={persist} />);

    await waitFor(() =>
      expect(
        screen.getByText("500: Internal Server Error"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Retry saving" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Stop waiting" })).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Saving live voice transcript" }),
    ).toBeNull();
    expect(mocks.start).not.toHaveBeenCalled();

    // The same escape hatch that recovers a hung save also recovers this one.
    await userEvent.click(screen.getByRole("button", { name: "Stop waiting" }));
    expect(screen.getByText("Voice Live ready")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    ).toBeEnabled();
  });

  // Regression (independent review, HIGH): persist()/discardPersistence()
  // used to share a single `abandonedRef` boolean. Resetting it for a new
  // cycle also un-silenced any still in-flight save from a PREVIOUS,
  // already-discarded cycle: when that old request finally resolved, its
  // continuation used the then-current saving/persisted/persistenceError
  // setters and conversationIdRef.current, letting a stale attempt
  // overwrite state that by then belonged to a newer cycle, or persist its
  // turns under the newer cycle's conversation id. The fix replaces the
  // boolean with a monotonic attemptIdRef token captured at invocation, so
  // a superseded attempt's eventual settlement is always a no-op.
  it("prevents a hung save from a discarded cycle from contaminating a newer cycle's saving/persisted state or conversation id", async () => {
    const ensureSession = vi.fn(async () => "session-1");
    const resolvers: Array<() => void> = [];
    const persistCalls: Array<{
      sessionId: string;
      conversationId: string;
      turns: VoiceTurnInput[];
    }> = [];
    const persist = vi.fn(
      (sessionId: string, conversationId: string, turns: VoiceTurnInput[]) => {
        persistCalls.push({ sessionId, conversationId, turns });
        return new Promise<void>((resolve) => {
          resolvers.push(resolve);
        });
      },
    );

    // Cycle 1 (never explicitly start()-ed, so conversationIdRef stays at
    // its initial "") produces one finalized turn; stop() begins saving it,
    // and that save is left hanging indefinitely.
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "First cycle turn",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(
      <Harness ensureSession={ensureSession} persist={persist} />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    controller = makeController();
    rerender(<Harness ensureSession={ensureSession} persist={persist} />);
    await waitFor(() => expect(persistCalls).toHaveLength(1));
    expect(
      screen.getByRole("button", { name: "Saving live voice transcript" }),
    ).toBeDisabled();

    // "Stop waiting" abandons the still in-flight save (bumps attemptIdRef)
    // and returns the UI to ready without waiting on it.
    await userEvent.click(screen.getByRole("button", { name: "Stop waiting" }));
    expect(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    ).toBeEnabled();

    // Cycle 2 actually calls start(), generating a real, distinct
    // conversation id, produces its own finalized turn, then stop() begins
    // its own, independent save attempt.
    await userEvent.click(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    );
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u2",
          role: "user",
          text: "Second cycle turn",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    rerender(<Harness ensureSession={ensureSession} persist={persist} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    controller = makeController();
    rerender(<Harness ensureSession={ensureSession} persist={persist} />);
    await waitFor(() => expect(persistCalls).toHaveLength(2));
    expect(
      screen.getByRole("button", { name: "Saving live voice transcript" }),
    ).toBeDisabled();

    // The abandoned cycle 1 request finally resolves late. Its captured
    // attemptId no longer matches (discard, then cycle 2's own persist(),
    // both bumped the token past it), so this must be a no-op: cycle 2's
    // own in-flight save is untouched and still shows "Saving...".
    await act(async () => {
      resolvers[0]?.();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(
      screen.getByRole("button", { name: "Saving live voice transcript" }),
    ).toBeDisabled();
    // Discard is legitimately available any time saving is true (it's the
    // escape hatch for cycle 2's own in-flight save, not evidence of a
    // regression) -- what matters is that cycle 2 hasn't been prematurely
    // marked persisted/ready by the stale attempt's resolution.
    expect(screen.queryByText("Voice Live ready")).toBeNull();

    // Cycle 2's own save now completes normally, unaffected by cycle 1's
    // stale resolution.
    await act(async () => {
      resolvers[1]?.();
    });
    await waitFor(() =>
      expect(screen.getByText("Voice Live ready")).toBeInTheDocument(),
    );

    expect(persistCalls[0]).toMatchObject({
      sessionId: "session-1",
      turns: [{ role: "user", text: "First cycle turn" }],
    });
    expect(persistCalls[1]).toMatchObject({
      sessionId: "session-1",
      turns: [{ role: "user", text: "Second cycle turn" }],
    });
    // Each attempt's turns were saved under its OWN cycle's conversation id
    // (captured at invocation time), never the other cycle's -- proving the
    // stale first attempt could not have persisted under, nor been confused
    // with, cycle 2's conversation id.
    expect(persistCalls[0].conversationId).not.toBe(
      persistCalls[1].conversationId,
    );
  });

  // Regression (independent re-review, HIGH): discardPersistence() only
  // invalidated this hook's OWN reaction to ensureSession()'s promise -- it
  // never told the CALLER (ChatApp's ensureSession) that the attempt asking
  // for a session had been abandoned. A discarded attempt's already-pending
  // ensureSession() (e.g. still awaiting createSession() over the network)
  // could then still unconditionally force-navigate ChatApp to the newly
  // created session once it resolved, or get treated as "current" even
  // after the user discarded and moved on. ensureSession() now accepts an
  // optional isStillWanted predicate; persist() must pass one that reflects
  // discard/supersession the instant it's queried, however long the
  // underlying network call takes to settle.
  it("passes ensureSession a predicate that turns false the instant this attempt is discarded", async () => {
    let capturedIsStillWanted: (() => boolean) | undefined;
    const ensureSession = vi.fn((isStillWanted?: () => boolean) => {
      capturedIsStillWanted = isStillWanted;
      // Never resolves in this test: the point is to observe the predicate's
      // answer change while the "network call" it would gate is still
      // pending, exactly the window the production race exploited.
      return new Promise<string>(() => {});
    });
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Abandon me too",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    render(<Harness ensureSession={ensureSession} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );

    await waitFor(() => expect(ensureSession).toHaveBeenCalledTimes(1));
    expect(capturedIsStillWanted).toBeInstanceOf(Function);
    // Still the current (only) attempt: the predicate must say so.
    expect(capturedIsStillWanted?.()).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: "Stop waiting" }));

    // Same predicate instance, re-queried after discard: it must now report
    // false so ChatApp's ensureSession knows not to commit navigation once
    // its still-pending createSession() call eventually resolves.
    expect(capturedIsStillWanted?.()).toBe(false);
  });

  // Regression (final acceptance review, Finding 2): persist()'s isStillWanted
  // predicate only gated whether ChatApp's ensureSession() could force-
  // navigate to a lazily-created session -- it was never propagated INTO
  // persistConversation() itself. A discarded attempt's still-in-flight
  // appendVoiceTurns() call could then have its result blindly applied by
  // ChatApp (setMessages/setInspectorVersion/reconcile) even though the user
  // had already discarded this exact voice attempt. persistConversation()
  // now accepts a 4th isStillValid predicate that reports false the instant
  // this attempt is discarded or superseded, mirroring ensureSession's
  // isStillWanted above, so ChatApp can gate its own client-side commits
  // without ever needing to (or being able to) cancel the underlying save.
  it("passes persistConversation a predicate that turns false the instant this attempt is discarded", async () => {
    let capturedIsStillValid: (() => boolean) | undefined;
    const persist = vi.fn(
      (
        _sessionId: string,
        _conversationId: string,
        _turns: VoiceTurnInput[],
        isStillValid: () => boolean,
      ) => {
        capturedIsStillValid = isStillValid;
        // Never resolves in this test: mirrors a slow/hung appendVoiceTurns()
        // call so the predicate's answer can be observed changing while it's
        // still "in flight" from ChatApp's perspective.
        return new Promise<void>(() => {});
      },
    );
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Discard me too",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    render(<Harness activeSessionId="session-1" persist={persist} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );

    await waitFor(() => expect(persist).toHaveBeenCalledTimes(1));
    expect(capturedIsStillValid).toBeInstanceOf(Function);
    // Still the current (only) attempt: the predicate must say so.
    expect(capturedIsStillValid?.()).toBe(true);

    await userEvent.click(screen.getByRole("button", { name: "Stop waiting" }));

    // Same predicate instance, re-queried after discard: it must now report
    // false so ChatApp knows not to commit any state from this save once its
    // still-pending appendVoiceTurns() call eventually resolves.
    expect(capturedIsStillValid?.()).toBe(false);
  });

  // Regression: ensureSession() is contractually async (Promise<string>), but
  // persist() cannot rely on that alone -- it is always invoked as
  // `void persist()` immediately followed by more code in the same scope,
  // most critically stop()'s subsequent stopLive() teardown call. A
  // synchronous throw here (a contract violation, mirroring the kind of
  // type mismatch already seen causing backend session-update failures)
  // must not propagate out of persist() and skip that teardown.
  it("never lets a synchronously-throwing ensureSession skip the underlying stop teardown", async () => {
    const ensureSession = vi.fn(() => {
      throw new Error("ensureSession contract violation");
    });
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "Needs a session",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender } = render(<Harness ensureSession={ensureSession} />);

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );

    expect(mocks.stop).toHaveBeenCalledTimes(1);
    controller = makeController();
    rerender(<Harness ensureSession={ensureSession} />);

    await waitFor(() =>
      expect(
        screen.getByText("ensureSession contract violation"),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: "Stop waiting" })).toBeEnabled();
  });

  it("exposes hasUnsavedTurns/exitLocked only once real data would be lost, not merely while live", () => {
    function LockHarness() {
      const voice = useInlineVoiceLive({
        config: CONFIG,
        model: "catalog-realtime-model",
        agent: "analyst",
        agents: AGENTS,
        history: [],
        ensureSession: async () => "session-1",
        persistConversation: async () => {},
      });
      return <span data-testid="locked">{String(voice.exitLocked)}</span>;
    }

    controller = makeController({ status: "live", active: true, turns: [] });
    const { rerender, getByTestId } = render(<LockHarness />);
    // A live connection with zero exchanges never blocks navigation.
    expect(getByTestId("locked").textContent).toBe("false");

    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "",
          pending: true,
          streaming: false,
          tool: "",
        },
      ],
    });
    rerender(<LockHarness />);
    // Speech has started, so even its pending turn is real unsaved data.
    expect(getByTestId("locked").textContent).toBe("true");
  });

  // Regression: stopping mid-utterance -- before the first word is ever
  // finalized -- previously left exitLocked stuck true forever once the
  // call ended. persist() only ever saves finalizedTurns(), so a
  // still-pending turn can never be completed or saved after teardown; but
  // nothing set saving/persistenceError either (persist() found nothing to
  // save and returned immediately), so no Retry/Discard control could ever
  // appear to explain or clear the lock -- a dead end with no recovery
  // path. Once the call is inactive, an unfinalizable turn must stop
  // counting as unsaved.
  it("clears exitLocked after stop when the only turn was still pending and never got finalized", () => {
    function LockHarness() {
      const voice = useInlineVoiceLive({
        config: CONFIG,
        model: "catalog-realtime-model",
        agent: "analyst",
        agents: AGENTS,
        history: [],
        ensureSession: async () => "session-1",
        persistConversation: async () => {},
      });
      return <span data-testid="locked">{String(voice.exitLocked)}</span>;
    }

    const pendingTurn = {
      id: "u1",
      role: "user" as const,
      text: "",
      pending: true,
      streaming: false,
      tool: "",
    };
    controller = makeController({
      status: "live",
      active: true,
      turns: [pendingTurn],
    });
    const { rerender, getByTestId } = render(<LockHarness />);
    // While still connected, even a pending-only turn blocks navigation:
    // stopping now would cut it off mid-word.
    expect(getByTestId("locked").textContent).toBe("true");

    // stop() tears the connection down synchronously, but voiceLive.ts
    // never clears `turns` -- the same never-finalized turn remains in the
    // array afterward, exactly as it would after a real stop() call.
    controller = makeController({
      status: "idle",
      active: false,
      turns: [pendingTurn],
    });
    rerender(<LockHarness />);
    // Inactive + nothing finalized: this turn can never be completed or
    // saved, so it must not block navigation forever with no recovery UI.
    expect(getByTestId("locked").textContent).toBe("false");
  });

  // Regression (voice acceptance round 9, MEDIUM companion): unlike the
  // empty-pending-turn case above, a still-open turn that already holds
  // real text (an assistant reply cut off mid-stream) CAN be completed --
  // finalizedTurns() now saves it once inactive -- so exitLocked must stay
  // true until that save actually resolves, not flip false the instant the
  // connection ends merely because the turn was never explicitly finalized.
  it("keeps exitLocked true after stop for a still-streaming turn with real content, until its save resolves", async () => {
    let resolvePersist: (() => void) | undefined;
    function LockHarness() {
      const voice = useInlineVoiceLive({
        config: CONFIG,
        model: "catalog-realtime-model",
        agent: "analyst",
        agents: AGENTS,
        history: [],
        activeSessionId: "session-1",
        ensureSession: async () => "session-1",
        persistConversation: () =>
          new Promise<void>((resolve) => {
            resolvePersist = resolve;
          }),
      });
      return <span data-testid="locked">{String(voice.exitLocked)}</span>;
    }

    const partialTurn = {
      id: "a1",
      role: "assistant" as const,
      text: "Cut off mid-sentence",
      pending: false,
      streaming: true,
      tool: "",
    };
    controller = makeController({
      status: "live",
      active: true,
      turns: [partialTurn],
    });
    const { rerender, getByTestId } = render(<LockHarness />);
    expect(getByTestId("locked").textContent).toBe("true");

    // stop() tears the connection down synchronously, but voiceLive.ts
    // never settles a turn that was still streaming -- the same turn
    // remains, `streaming: true`, exactly as it would after a real
    // mid-response stop() call.
    controller = makeController({
      status: "idle",
      active: false,
      turns: [partialTurn],
    });
    rerender(<LockHarness />);
    // Inactive, but this turn has real content and is now being saved:
    // navigation must stay locked rather than unlocking over unsaved data.
    expect(getByTestId("locked").textContent).toBe("true");

    // The save is dispatched through a microtask chain (ensureSession's
    // resolved-session promise, then persistConversation itself), so
    // resolvePersist isn't captured synchronously after rerender().
    await waitFor(() => expect(resolvePersist).toBeInstanceOf(Function));
    resolvePersist?.();
    // Once the save actually resolves, the lock clears normally.
    await waitFor(() => expect(getByTestId("locked").textContent).toBe("false"));
  });

  // Regression (final acceptance review, Finding 4): the test above proves
  // exitLocked recovers, but the rendered transcript previously still kept
  // showing a permanent "Listening…" bubble for this same never-finalized
  // turn -- clearing the navigation lock alone didn't fix what the user
  // actually saw on screen. Once live becomes inactive, a turn that is
  // still open with no real text must disappear from the transcript
  // entirely, not just stop blocking navigation.
  it("clears the rendered 'Listening…' placeholder once stop() ends the call with an incomplete turn", async () => {
    const pendingTurn = {
      id: "u1",
      role: "user" as const,
      text: "",
      pending: true,
      streaming: false,
      tool: "",
    };
    controller = makeController({
      status: "live",
      active: true,
      turns: [pendingTurn],
    });
    const { rerender } = render(<Harness />);
    expect(screen.getByText("Listening…")).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    // stop() tears the connection down synchronously, but voiceLive.ts never
    // clears `turns` -- the same never-finalized turn remains afterward,
    // exactly as it would after a real stop() call.
    controller = makeController({
      status: "idle",
      active: false,
      turns: [pendingTurn],
    });
    rerender(<Harness />);

    expect(screen.queryByText("Listening…")).not.toBeInTheDocument();
  });

  // Regression (final acceptance review, Finding 4 companion): the fix must
  // not throw away turns that captured real content before the connection
  // ended -- only the still-open placeholder case (no content at all) is
  // dropped. A turn with genuine partial content keeps that content and
  // simply stops being presented as in-progress.
  it("keeps a turn's real partial content after stop() while turning off its live indicator", async () => {
    const partialTurn = {
      id: "a1",
      role: "assistant" as const,
      text: "Partial answer before the call",
      pending: false,
      streaming: true,
      tool: "",
    };
    controller = makeController({
      status: "live",
      active: true,
      turns: [partialTurn],
    });
    const { rerender } = render(<Harness />);
    expect(
      screen.getByText("Partial answer before the call"),
    ).toBeInTheDocument();

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    controller = makeController({
      status: "idle",
      active: false,
      turns: [partialTurn],
    });
    rerender(<Harness />);

    // Content survives -- it is not a placeholder, so it must never be
    // dropped just because the connection closed while still "streaming".
    expect(
      screen.getByText("Partial answer before the call"),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Generating")).not.toBeInTheDocument();
  });

  // Regression (voice acceptance round 9, MEDIUM): the test above proves the
  // partial content stays on screen, but finalizedTurns() still excluded a
  // still-open (pending/streaming) turn even once the connection had ended
  // -- voiceLive.ts never flips that flag after a mid-turn teardown, so it
  // never settles. The turn was therefore never included in the append
  // payload, never counted as unsaved, and navigation could unlock with
  // real, visible-but-unsaved history silently left behind. finalizedTurns()
  // now finalizes a still-open turn's real text once the connection has
  // ended, so it (a) reaches persistConversation()'s payload, (b) keeps
  // hasUnsavedTurns/exitLocked true until that save actually completes, and
  // (c) drops from this transient overlay once persisted, so it doesn't
  // duplicate the durable copy ChatApp's own `messages` state then shows
  // alongside it.
  it("includes a still-streaming turn's real content in the save payload once stop() ends the call, keeps navigation locked until it saves, and avoids a duplicate once saved", async () => {
    const partialTurn = {
      id: "a1",
      role: "assistant" as const,
      text: "Here is the partial ans",
      pending: false,
      streaming: true,
      tool: "",
    };
    const persistCalls: VoiceTurnInput[][] = [];
    let resolvePersist: (() => void) | undefined;
    const persist = vi.fn((_sessionId: string, _conversationId: string, turns: VoiceTurnInput[]) => {
      persistCalls.push(turns);
      return new Promise<void>((resolve) => {
        resolvePersist = resolve;
      });
    });
    controller = makeController({
      status: "live",
      active: true,
      turns: [partialTurn],
    });
    const { rerender } = render(
      <Harness activeSessionId="session-1" persist={persist} />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    // stop() tears the connection down synchronously, but voiceLive.ts never
    // clears/settles `turns` -- the same still-streaming turn remains,
    // exactly as it would after a real stop() call mid-response.
    controller = makeController({
      status: "idle",
      active: false,
      turns: [partialTurn],
    });
    rerender(<Harness activeSessionId="session-1" persist={persist} />);

    // The append payload includes the partial content -- it is not silently
    // lost just because the turn was still "streaming" when torn down.
    await waitFor(() => expect(persistCalls).toHaveLength(1));
    expect(persistCalls[0]).toEqual([
      expect.objectContaining({
        role: "assistant",
        text: "Here is the partial ans",
      }),
    ]);

    // The save is genuinely in flight: navigation must stay locked rather
    // than unlocking over content that hasn't been saved yet.
    expect(
      screen.getByRole("button", { name: "Saving live voice transcript" }),
    ).toBeDisabled();
    // Exactly one copy on screen so far -- only this transient overlay has
    // it; the durable side hasn't produced its own copy yet.
    expect(screen.getAllByText("Here is the partial ans")).toHaveLength(1);

    resolvePersist?.();
    await waitFor(() =>
      expect(screen.getByText("Voice Live ready")).toBeInTheDocument(),
    );

    // Once persisted, exactly one copy remains: the durable copy that
    // ChatApp's own `messages` state now shows (simulated here by the
    // Harness's persistedMessages), not a duplicate left behind by this
    // transient overlay.
    expect(screen.getAllByText("Here is the partial ans")).toHaveLength(1);
  });

  // Regression (independent review of round 9's own MEDIUM fix): the test
  // above only has a single, still-streaming turn in `turns`, which passed
  // even before this fix by coincidence -- finalizedTurns(turns, /* active
  // */ true) computes an *empty* array when that lone turn is the only one
  // present (it's neither settled nor is `active` false yet), so persist()
  // takes its `turns.length === 0` early return and never claims the
  // in-flight guard, leaving it open for the correct, later call. That
  // coincidence breaks completely once an earlier, already-settled exchange
  // also exists: stop() itself calls persist() synchronously, before
  // voiceLive.ts's teardown propagates through a render, so at that instant
  // finalizedTurns(turns, true) is non-empty -- it contains the settled
  // exchange but omits the still-open cutoff turn -- so persist() proceeds
  // past the early return and claims persistenceRef.current with an
  // *incomplete* payload. The later, correct call (from the wasActiveRef
  // effect, once `active` genuinely reads false) then hits the
  // `if (persistenceRef.current) return persistenceRef.current;` guard and
  // is discarded without ever recomputing finalizedTurns -- permanently
  // dropping the cutoff turn's content, with no retry surfaced. stop() now
  // writes liveActiveRef.current = false itself before calling persist(), so
  // its own synchronous first call already computes the same complete
  // result the later effect-driven call would.
  it("includes a still-streaming turn's content in the save payload alongside an earlier settled exchange, instead of losing it to a stale in-flight guard", async () => {
    const settledUserTurn = {
      id: "u1",
      role: "user" as const,
      text: "First question",
      pending: false,
      streaming: false,
      tool: "",
    };
    const settledAssistantTurn = {
      id: "a1",
      role: "assistant" as const,
      text: "First answer",
      pending: false,
      streaming: false,
      tool: "",
    };
    const cutoffTurn = {
      id: "a2",
      role: "assistant" as const,
      text: "Here is the second, partial ans",
      pending: false,
      streaming: true,
      tool: "",
    };
    const persistCalls: VoiceTurnInput[][] = [];
    let resolvePersist: (() => void) | undefined;
    const persist = vi.fn((_sessionId: string, _conversationId: string, turns: VoiceTurnInput[]) => {
      persistCalls.push(turns);
      return new Promise<void>((resolve) => {
        resolvePersist = resolve;
      });
    });
    controller = makeController({
      status: "live",
      active: true,
      turns: [settledUserTurn, settledAssistantTurn, cutoffTurn],
    });
    const { rerender } = render(
      <Harness activeSessionId="session-1" persist={persist} />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    // stop() tears the connection down synchronously, but voiceLive.ts never
    // clears/settles `turns` -- the same still-streaming cutoff turn remains
    // alongside the two already-settled ones, exactly as it would after a
    // real stop() call mid-response to a second question.
    controller = makeController({
      status: "idle",
      active: false,
      turns: [settledUserTurn, settledAssistantTurn, cutoffTurn],
    });
    rerender(<Harness activeSessionId="session-1" persist={persist} />);

    // Exactly one save call is made -- not a stale/incomplete first call
    // that wins the guard, followed by a discarded correct one -- and it
    // carries every turn's real content, including the cutoff turn's
    // partial answer.
    await waitFor(() => expect(persistCalls.length).toBeGreaterThan(0));
    expect(persist).toHaveBeenCalledTimes(1);
    expect(persistCalls[0]).toEqual([
      expect.objectContaining({ role: "user", text: "First question" }),
      expect.objectContaining({ role: "assistant", text: "First answer" }),
      expect.objectContaining({
        role: "assistant",
        text: "Here is the second, partial ans",
      }),
    ]);

    resolvePersist?.();
    await waitFor(() =>
      expect(screen.getByText("Voice Live ready")).toBeInTheDocument(),
    );

    // The cutoff turn's content genuinely reached the durable side
    // (simulated by the Harness's persistedMessages) instead of being
    // silently discarded by a guard that returned a stale, incomplete
    // promise.
    expect(
      screen.getAllByText("Here is the second, partial ans"),
    ).toHaveLength(1);
  });

  it("adopts a session created by a text send in the same empty chat", () => {
    function BindingHarness({
      activeSessionId,
    }: {
      activeSessionId: string | null;
    }) {
      const voice = useInlineVoiceLive({
        config: CONFIG,
        model: "catalog-realtime-model",
        agent: "analyst",
        agents: AGENTS,
        history: [],
        activeSessionId,
        ensureSession: async () => "created-session",
        persistConversation: async () => {},
      });
      return <span data-testid="binding">{voice.boundSessionId ?? "empty"}</span>;
    }

    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "",
          pending: true,
          streaming: false,
          tool: "",
        },
      ],
    });
    const { rerender, getByTestId } = render(
      <BindingHarness activeSessionId={null} />,
    );
    expect(getByTestId("binding").textContent).toBe("empty");

    rerender(<BindingHarness activeSessionId="created-session" />);
    // Regression: this bound one effect-cycle after the rerender when the
    // commit lived in a useEffect, leaving a frame where the transcript could
    // look unbound even though the session already existed. It's now a
    // render-time adjustment, so the DOM already reflects it synchronously —
    // no waitFor needed to observe it.
    expect(getByTestId("binding").textContent).toBe("created-session");
  });

  it("keeps the transcript bound to its original chat once committed, even if navigation changes the active chat", () => {
    function BindingHarness({
      activeSessionId,
    }: {
      activeSessionId: string | null;
    }) {
      const voice = useInlineVoiceLive({
        config: CONFIG,
        model: "catalog-realtime-model",
        agent: "analyst",
        agents: AGENTS,
        history: [],
        activeSessionId,
        ensureSession: async () => "created-session",
        persistConversation: async () => {},
      });
      return <span data-testid="binding">{voice.boundSessionId ?? "empty"}</span>;
    }

    controller = makeController({ status: "live", active: true, turns: [] });
    const { rerender, getByTestId } = render(
      <BindingHarness activeSessionId="chat-a" />,
    );
    expect(getByTestId("binding").textContent).toBe("chat-a");

    // The first real turn commits the binding to whichever chat is active
    // right now (chat-a) -- synchronously, in the same render as the turn.
    controller = makeController({
      status: "live",
      active: true,
      turns: [
        {
          id: "u1",
          role: "user",
          text: "hello",
          pending: false,
          streaming: false,
          tool: "",
        },
      ],
    });
    rerender(<BindingHarness activeSessionId="chat-a" />);
    expect(getByTestId("binding").textContent).toBe("chat-a");

    // Regression: navigating to a different chat afterward must not drag the
    // still-unsaved live transcript along with it. A late/duplicate upstream
    // event or prop change cannot re-lock onto the new chat because
    // boundSessionId is sticky once a real turn has committed it.
    rerender(<BindingHarness activeSessionId="chat-b" />);
    expect(getByTestId("binding").textContent).toBe("chat-a");
  });
});
