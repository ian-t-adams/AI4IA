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
} from "./InlineVoiceLive";
import type { VoiceTurnInput } from "@/lib/types";
import type { VoiceLiveController } from "@/lib/voiceLive";

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
}: {
  persist?: (
    sessionId: string,
    conversationId: string,
    turns: VoiceTurnInput[],
  ) => Promise<void>;
  onSend?: (text: string) => void;
  ensureSession?: () => Promise<string>;
}) {
  const [persistedMessages, setPersistedMessages] = useState<DisplayMessage[]>([]);
  const persistConversation = useCallback(
    async (
      sessionId: string,
      conversationId: string,
      turns: VoiceTurnInput[],
    ) => {
      await persist(sessionId, conversationId, turns);
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
    model: "catalog-realtime-model",
    agent: "analyst",
    agents: AGENTS,
    history: [{ role: "user", text: "Earlier text turn" }],
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
    expect(args?.[1]).toBe("catalog-realtime-model");
    expect(args?.[4]).toBe("analyst");
    expect(args?.[5]).toEqual([{ role: "user", text: "Earlier text turn" }]);
    expect(args?.[7]).toBe(false);
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
        _model: unknown,
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

  it("does not strand retry when eager session creation fails before any voice turn", async () => {
    const ensureSession = vi.fn().mockRejectedValue(new Error("store unavailable"));
    render(<Harness ensureSession={ensureSession} />);

    const microphone = screen.getByRole("button", {
      name: "Start live voice conversation",
    });
    await userEvent.click(microphone);
    await waitFor(() => expect(ensureSession).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole("button", { name: "Retry saving" })).toBeNull();

    await userEvent.click(microphone);
    expect(mocks.start).toHaveBeenCalledTimes(2);
    expect(ensureSession).toHaveBeenCalledTimes(2);
  });

  it("retries session creation after persistence-time creation fails", async () => {
    const ensureSession = vi
      .fn<() => Promise<string>>()
      .mockRejectedValueOnce(new Error("eager create failed"))
      .mockRejectedValueOnce(new Error("save create failed"))
      .mockResolvedValue("session-1");
    const persist = vi.fn(async () => {});
    const { rerender } = render(
      <Harness ensureSession={ensureSession} persist={persist} />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "Start live voice conversation" }),
    );
    await waitFor(() => expect(ensureSession).toHaveBeenCalledTimes(1));

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
    rerender(<Harness ensureSession={ensureSession} persist={persist} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Stop live voice conversation" }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Retry saving" })).toBeEnabled(),
    );
    expect(
      screen.getByRole("button", {
        name: "Retry saving the voice transcript below",
      }),
    ).toBeDisabled();

    await userEvent.click(screen.getByRole("button", { name: "Retry saving" }));
    await waitFor(() => expect(persist).toHaveBeenCalledTimes(1));
    expect(ensureSession).toHaveBeenCalledTimes(3);
  });
});
