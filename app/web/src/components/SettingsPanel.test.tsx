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
        <SettingsPanel models={[]} onClose={() => {}} />
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
        <SettingsPanel models={[]} onClose={() => {}} />
      </ThemeProvider>,
    );
    expect(
      screen.queryByText(/Disabled while High contrast is active/),
    ).toBeNull();
    expect(screen.getByRole("group", { name: "Accent color" })).toBeEnabled();
  });
});
