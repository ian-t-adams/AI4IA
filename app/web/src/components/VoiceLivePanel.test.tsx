// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ModelEntry } from "@/lib/types";
import { VoiceLivePanel } from "./VoiceLivePanel";

const mocks = vi.hoisted(() => ({
  start: vi.fn(),
  stop: vi.fn(),
  toggle: vi.fn(),
  useVoiceLive: vi.fn(),
}));

vi.mock("@/lib/voiceLive", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/voiceLive")>();
  return { ...actual, useVoiceLive: mocks.useVoiceLive };
});

const CONFIG = {
  enabled: true,
  wsUrl: "wss://api.example.test/api/voice/live",
  devUser: "dev",
  toolsAvailable: false,
};

const MODEL: ModelEntry = {
  id: "gpt-realtime",
  displayName: "GPT Realtime",
  category: "realtime",
  format: "OpenAI",
  conversational: false,
  contextWindow: null,
  maxOutputTokens: null,
  options: [],
};

function controller(overrides: Record<string, unknown> = {}) {
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

beforeEach(() => {
  const values = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
      clear: () => values.clear(),
    },
  });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("VoiceLivePanel", () => {
  it("starts automatically with hydrated settings and the active chat agent", async () => {
    mocks.useVoiceLive.mockReturnValue(controller());
    window.localStorage.setItem("ai4ia.voiceLive.voice", "verse");
    window.localStorage.setItem("ai4ia.voiceLive.agent", "saved-agent");
    window.localStorage.setItem("ai4ia.voiceLive.model", "stale-model");

    render(
      <VoiceLivePanel
        config={CONFIG}
        models={[MODEL]}
        agents={[
          { name: "analyst", displayName: "Analyst", description: "", enabled: true },
        ]}
        initialAgent="analyst"
        onClose={vi.fn()}
      />,
    );

    await waitFor(() => expect(mocks.start).toHaveBeenCalledTimes(1));
    expect(mocks.useVoiceLive.mock.calls.at(-1)?.[2]).toBe("verse");
    expect(mocks.useVoiceLive.mock.calls.at(-1)?.[4]).toBe("analyst");
    expect(mocks.useVoiceLive.mock.calls.at(-1)?.[1]).toBe("gpt-realtime");
    expect(screen.queryByRole("combobox", { name: "Live voice agent" })).toBeNull();
    expect(screen.getByRole("button", { name: "Voice settings" })).toBeInTheDocument();
  });

  it("ends the session, persists finalized turns, and returns to chat", async () => {
    const onClose = vi.fn();
    const onConversation = vi.fn();
    mocks.useVoiceLive.mockReturnValue(
      controller({
        status: "live",
        active: true,
        turns: [
          {
            id: "u1",
            role: "user",
            text: "Hello",
            streaming: false,
            pending: false,
            tool: "",
          },
          {
            id: "a1",
            role: "assistant",
            text: "Hi there",
            streaming: false,
            pending: false,
            tool: "",
          },
        ],
      }),
    );

    render(
      <VoiceLivePanel
        config={CONFIG}
        models={[MODEL]}
        agents={[]}
        onClose={onClose}
        onConversation={onConversation}
      />,
    );

    await userEvent.click(
      screen.getByRole("button", { name: "End and return to chat" }),
    );

    expect(mocks.stop).toHaveBeenCalledTimes(1);
    expect(onConversation).toHaveBeenCalledWith([
      { role: "user", text: "Hello" },
      { role: "assistant", text: "Hi there" },
    ]);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("shows connection errors inside the voice surface", async () => {
    let reportConnectionError: ((message: string) => void) | null = null;
    mocks.useVoiceLive.mockImplementation(
      (
        _config: unknown,
        _model: unknown,
        _voice: unknown,
        reportError: (message: string) => void,
      ) => {
        reportConnectionError = reportError;
        return controller();
      },
    );
    render(
      <VoiceLivePanel
        config={CONFIG}
        models={[MODEL]}
        agents={[]}
        onClose={vi.fn()}
      />,
    );

    expect(reportConnectionError).not.toBeNull();
    act(() => {
      (reportConnectionError as (message: string) => void)("Voice connection failed.");
    });
    expect(await screen.findByRole("alert")).toHaveTextContent("Voice connection failed.");
  });
});
