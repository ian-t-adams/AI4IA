// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PhotoAvatarVoiceCard } from "./PhotoAvatarVoiceCard";

type Props = Parameters<typeof PhotoAvatarVoiceCard>[0];
const AVATAR: NonNullable<Props["avatar"]> = {
  id: "a".repeat(32),
  displayName: "Office guide",
  preview: null,
  disclosure: { aiGenerated: true, label: "AI-generated" },
};

function setup(overrides: Partial<Props> = {}) {
  const props: Props = {
    avatar: AVATAR,
    voice: "Fixture voice",
    disabledReason: null,
    locked: false,
    focusRequest: 0,
    onStart: vi.fn(),
    onChoose: vi.fn(),
    onClear: vi.fn(),
    ...overrides,
  };
  const view = render(<PhotoAvatarVoiceCard {...props} />);
  return {
    ...props,
    user: userEvent.setup(),
    rerender: (changes: Partial<Props>) => view.rerender(<PhotoAvatarVoiceCard {...props} {...changes} />),
  };
}

afterEach(cleanup);

describe("PhotoAvatarVoiceCard", () => {
  it("discloses the selected avatar and billing without starting until the user chooses to talk", async () => {
    const { user, onStart, onChoose, onClear } = setup();
    const card = screen.getByRole("region", { name: "Avatar voice" });
    expect(within(card).getByRole("heading", { name: "Talk with Office guide" })).toBeInTheDocument();
    expect(within(card).getByText("AI-generated")).toBeInTheDocument();
    expect(within(card).getByText(/billed while the session is connected/)).toBeInTheDocument();
    expect(onStart).not.toHaveBeenCalled();
    await user.click(within(card).getByRole("button", { name: "Start talking" }));
    expect(onStart).toHaveBeenCalledTimes(1);
    await user.click(within(card).getByRole("button", { name: "Choose avatar" }));
    expect(onChoose).toHaveBeenCalledTimes(1);
    await user.click(within(card).getByRole("button", { name: "Voice only" }));
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("explains an unresolved avatar while permitting an explicit voice-only selection", async () => {
    const { user, rerender, onStart, onClear } = setup();
    expect(screen.getByText("Talk with Office guide")).toBeInTheDocument();
    rerender({ avatar: null, disabledReason: "Checking your avatar before starting..." });
    expect(screen.queryByText("Talk with Office guide")).toBeNull();
    const start = screen.getByRole("button", { name: "Start talking" });
    expect(start).toBeDisabled();
    expect(start).toHaveAccessibleDescription("Checking your avatar before starting...");
    await user.click(start);
    expect(onStart).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Voice only" }));
    expect(onClear).toHaveBeenCalledTimes(1);
  });

  it("keeps both selection controls locked while a transcript is saving", async () => {
    const { user, onChoose, onClear } = setup({
      locked: true, disabledReason: "Finish saving the voice transcript.",
    });
    for (const name of ["Start talking", "Choose avatar", "Voice only"]) {
      const button = screen.getByRole("button", { name });
      expect(button).toBeDisabled();
      await user.click(button);
    }
    expect(onChoose).not.toHaveBeenCalled();
    expect(onClear).not.toHaveBeenCalled();
  });

  it("focuses the selected-avatar heading only when explicitly requested", () => {
    const { rerender } = setup();
    const heading = screen.getByRole("heading", { name: "Talk with Office guide" });
    expect(heading).not.toHaveFocus();
    rerender({ focusRequest: 1 });
    expect(heading).toHaveFocus();
  });
});
