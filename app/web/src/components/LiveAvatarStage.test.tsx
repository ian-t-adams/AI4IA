// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { LiveAvatarView } from "@/lib/voiceLive";
import { LiveAvatarStage } from "./LiveAvatarStage";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function view(overrides: Partial<LiveAvatarView> = {}): LiveAvatarView {
  return {
    element: document.createElement("video"),
    label: "AI-generated",
    unsupported: false,
    failure: null,
    started: true,
    speaking: false,
    idleEndsAt: null,
    sessionEndsAt: null,
    playbackBlocked: false,
    resume: vi.fn(),
    micPaused: false,
    interrupt: vi.fn(),
    ...overrides,
  };
}

describe("LiveAvatarStage", () => {
  it("shows the avatar video with a persistent AI-generated label and an end control", async () => {
    const avatar = view();
    const onEnd = vi.fn();
    render(<LiveAvatarStage avatar={avatar} active onEnd={onEnd} />);
    const stage = screen.getByRole("region", { name: "Live avatar" });
    expect(stage).toContainElement(avatar.element);
    expect(screen.getByText("AI-generated")).toBeInTheDocument();
    expect(screen.getByText("Listening")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "End session" }));
    expect(onEnd).toHaveBeenCalledTimes(1);
  });

  it("renders nothing when the session is not live, and releases the video", () => {
    const avatar = view();
    const { rerender } = render(<LiveAvatarStage avatar={avatar} active onEnd={vi.fn()} />);
    expect(avatar.element?.isConnected).toBe(true);
    rerender(<LiveAvatarStage avatar={avatar} active={false} onEnd={vi.fn()} />);
    expect(screen.queryByRole("region", { name: "Live avatar" })).toBeNull();
    expect(avatar.element?.isConnected).toBe(false);
  });

  it("counts down the idle timeout and the session cap", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-26T12:00:00Z"));
    const now = Date.now();
    render(
      <LiveAvatarStage
        avatar={view({ idleEndsAt: now + 25_000, sessionEndsAt: now + 45_000, speaking: true })}
        active
        onEnd={vi.fn()}
      />,
    );
    expect(screen.getByText("Speaking")).toBeInTheDocument();
    expect(screen.getByText(/session ends in 0:25 unless you keep talking/)).toBeInTheDocument();
    expect(screen.getByText(/time limit in 0:45/)).toBeInTheDocument();
  });

  it("keeps the ticking countdown out of the live region and announces only at thresholds", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-26T12:00:00Z"));
    const now = Date.now();
    render(<LiveAvatarStage avatar={view({ idleEndsAt: now + 25_000 })} active onEnd={vi.fn()} />);
    const timer = screen.getByRole("timer");
    expect(timer).toHaveTextContent("0:25");
    expect(timer).toHaveAttribute("aria-live", "off");
    const status = screen.getByRole("status");
    const warning = status.textContent;
    expect(warning).toMatch(/ends soon unless you keep talking/);
    expect(warning).not.toMatch(/\d/);
    act(() => {
      vi.advanceTimersByTime(1_000);
    });
    // The visible countdown ticks, but the announcement does not change.
    expect(timer).toHaveTextContent("0:24");
    expect(status.textContent).toBe(warning);
    act(() => {
      vi.advanceTimersByTime(14_000);
    });
    // One more announcement once about ten seconds remain.
    expect(timer).toHaveTextContent("0:10");
    expect(status.textContent).toMatch(/About ten seconds until the avatar session ends/);
  });

  it("offers a gesture to start blocked sound and explains a voice-only fallback", async () => {
    const avatar = view({ playbackBlocked: true });
    const { rerender } = render(<LiveAvatarStage avatar={avatar} active onEnd={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Play avatar sound" }));
    expect(avatar.resume).toHaveBeenCalledTimes(1);
    rerender(<LiveAvatarStage avatar={view({ element: null, unsupported: true })} active onEnd={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("voice only");
  });

  it("offers Interrupt only while the avatar talks and says when the microphone is paused", async () => {
    const interrupt = vi.fn();
    const stage = (overrides: Partial<LiveAvatarView>) => (
      <LiveAvatarStage avatar={view({ interrupt, ...overrides })} active onEnd={vi.fn()} />
    );
    const { rerender } = render(stage({ speaking: true }));
    expect(screen.getByText("Speaking")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Interrupt" }));
    expect(interrupt).toHaveBeenCalledTimes(1);

    // The end of the speech is still playing after the server reports idle.
    rerender(stage({ speaking: false, micPaused: true }));
    expect(screen.getByText("Speaking · mic paused")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Interrupt" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Live avatar" })).toHaveAttribute("data-state", "speaking");
    expect(screen.getByText("AI-generated")).toBeInTheDocument();

    rerender(stage({}));
    expect(screen.getByText("Listening")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Interrupt" })).toBeNull();
    rerender(stage({ started: false, speaking: true }));
    expect(screen.queryByRole("button", { name: "Interrupt" })).toBeNull();
  });

  it("offers Interrupt in the mini player as well", async () => {
    const interrupt = vi.fn();
    render(
      <LiveAvatarStage
        variant="mini"
        avatar={view({ speaking: true, interrupt })}
        active
        onEnd={vi.fn()}
        onReturn={vi.fn()}
      />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Interrupt" }));
    expect(interrupt).toHaveBeenCalledTimes(1);
  });

  it("keeps keyboard focus among the stage controls after Interrupt, never on End session", async () => {
    const user = userEvent.setup();
    const interrupt = vi.fn();
    const { rerender } = render(
      <LiveAvatarStage avatar={view({ speaking: true, interrupt })} active onEnd={vi.fn()} onFitChange={vi.fn()} />,
    );
    screen.getByRole("button", { name: "Interrupt" }).focus();
    await user.keyboard("{Enter}");
    expect(interrupt).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Fill frame" })).toHaveFocus();

    // Control: with End session as the only other control, focus never lands on it.
    rerender(<LiveAvatarStage avatar={view({ speaking: true, interrupt })} active onEnd={vi.fn()} />);
    screen.getByRole("button", { name: "Interrupt" }).focus();
    await user.keyboard("{Enter}");
    expect(interrupt).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "End session" })).not.toHaveFocus();
  });

  it("moves no focus for an Interrupt that didn't have it", () => {
    const interrupt = vi.fn();
    render(
      <LiveAvatarStage avatar={view({ speaking: true, interrupt })} active onEnd={vi.fn()} onFitChange={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Interrupt" }));
    expect(interrupt).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Fill frame" })).not.toHaveFocus();
  });
});
