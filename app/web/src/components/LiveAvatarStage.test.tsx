// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen } from "@testing-library/react";
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
});
