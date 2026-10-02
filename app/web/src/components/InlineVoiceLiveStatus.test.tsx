// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InlineVoiceLiveStatus, type InlineVoiceLiveState } from "./InlineVoiceLive";

afterEach(() => {
  cleanup();
});

function failedVoice(): InlineVoiceLiveState {
  return {
    messages: [],
    enabled: true,
    supported: true,
    active: false,
    saving: false,
    phase: "idle",
    statusLabel: "Voice Live ready",
    agentLabel: "",
    error: "Voice Live couldn't connect.",
    notice: null,
    persistenceError: null,
    hasUnsavedTurns: false,
    exitLocked: false,
    boundSessionId: null,
    avatar: null,
    start: vi.fn(),
    stop: vi.fn(),
    sendText: vi.fn(() => false),
    retryPersistence: vi.fn(),
    discardPersistence: vi.fn(),
  };
}

describe("InlineVoiceLiveStatus retry", () => {
  it("retries through the caller's gated start, so a retry is refused like any start", async () => {
    const voice = failedVoice();
    const onRetry = vi.fn();
    render(<InlineVoiceLiveStatus voice={voice} onRetry={onRetry} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(voice.start).not.toHaveBeenCalled();
  });

  it("falls back to starting the session directly when no gate is supplied", async () => {
    const voice = failedVoice();
    render(<InlineVoiceLiveStatus voice={voice} />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Retry" }));
    expect(voice.start).toHaveBeenCalledTimes(1);
  });
});
