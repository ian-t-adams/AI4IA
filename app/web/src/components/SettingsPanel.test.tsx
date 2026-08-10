// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SettingsPanel } from "./SettingsPanel";
import { ThemeProvider } from "./ThemeProvider";

afterEach(cleanup);

describe("SettingsPanel", () => {
  it("keeps the high-contrast accent explanation at full contrast, outside the dimmed disabled fieldset", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    await user.click(screen.getByRole("button", { name: "High contrast" }));

    const explanation = screen.getByText(/Disabled while High contrast is active/);
    // Must NOT be a descendant of the (opacity-reduced) disabled fieldset --
    // otherwise the explanation of *why* accent picking is disabled would
    // itself be dimmed below a readable contrast ratio.
    expect(explanation.closest("fieldset")).toBeNull();

    const accentFieldset = screen.getByRole("group", { name: "Accent color" });
    expect(accentFieldset).toBeDisabled();
  });

  it("does not render the accent explanation outside high contrast", () => {
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    expect(
      screen.queryByText(/Disabled while High contrast is active/),
    ).toBeNull();
    expect(screen.getByRole("group", { name: "Accent color" })).toBeEnabled();
  });

  it("wraps Tab from the last enabled control when a disabled fieldset follows it", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    await user.click(screen.getByRole("button", { name: "High contrast" }));
    const textSize = screen.getByRole("slider", { name: /Text size/i });
    const close = screen.getByRole("button", { name: "Close settings" });
    textSize.focus();

    const event = new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    });
    textSize.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(close).toHaveFocus();
  });

  it("wraps Tab from the last accent control when that fieldset is enabled", () => {
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    const lastAccent = screen.getByRole("button", { name: "Accent Magenta" });
    const close = screen.getByRole("button", { name: "Close settings" });
    lastAccent.focus();

    const event = new KeyboardEvent("keydown", {
      key: "Tab",
      bubbles: true,
      cancelable: true,
    });
    lastAccent.dispatchEvent(event);

    expect(event.defaultPrevented).toBe(true);
    expect(close).toHaveFocus();
  });
  it("contains only appearance and accessibility controls", () => {
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );

    expect(screen.getByRole("dialog")).toHaveAccessibleName(
      "Appearance and accessibility settings",
    );
    expect(screen.queryByRole("group", { name: /Background/i })).toBeNull();
    expect(screen.queryByText(/Generate a background/i)).toBeNull();
  });
});
