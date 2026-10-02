// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { AppearanceSettings, SettingsPage } from "./SettingsPanel";
import { ThemeProvider } from "./ThemeProvider";

afterEach(cleanup);

describe("AppearanceSettings", () => {
  it("keeps the high-contrast accent explanation at full contrast, outside the dimmed disabled fieldset", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <AppearanceSettings />
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
        <AppearanceSettings />
      </ThemeProvider>,
    );
    expect(
      screen.queryByText(/Disabled while High contrast is active/),
    ).toBeNull();
    expect(screen.getByRole("group", { name: "Accent color" })).toBeEnabled();
  });
});

describe("SettingsPage", () => {
  function renderPage(onOpenDeletionStatus = vi.fn()) {
    render(
      <ThemeProvider>
        <SettingsPage onOpenDeletionStatus={onOpenDeletionStatus} />
      </ThemeProvider>,
    );
    return onOpenDeletionStatus;
  }

  it("groups appearance, data and help into labelled sections", () => {
    renderPage();
    for (const name of ["Appearance & accessibility", "Data & privacy", "Help & resources"]) {
      expect(screen.getByRole("region", { name })).toBeInTheDocument();
    }
    const appearance = screen.getByRole("region", { name: "Appearance & accessibility" });
    expect(within(appearance).getByRole("group", { name: "Theme" })).toBeInTheDocument();
    expect(within(appearance).getByRole("slider", { name: /Text size/i })).toBeInTheDocument();
    expect(within(appearance).getByRole("group", { name: "Accent color" })).toBeInTheDocument();
  });

  it("contains no conversation settings or background generator", () => {
    renderPage();
    expect(screen.queryByRole("group", { name: /Background/i })).toBeNull();
    expect(screen.queryByText(/Generate a background/i)).toBeNull();
    expect(screen.queryByRole("combobox", { name: /Model/i })).toBeNull();
  });

  it("opens deletion status without implying that removal erases data", async () => {
    const user = userEvent.setup();
    const onOpen = renderPage();
    const data = screen.getByRole("region", { name: "Data & privacy" });
    expect(data).toHaveTextContent("That alone doesn't mean its stored data has been erased.");
    expect(data).toHaveTextContent("It is not proof of erasure, there is no automatic cleanup");
    expect(data).not.toHaveTextContent(/permanently erased|automatically deleted/i);

    await user.click(within(data).getByRole("button", { name: "Deletion status" }));
    expect(onOpen).toHaveBeenCalledTimes(1);
  });

  it("opens help resources in a new tab without an opener", () => {
    renderPage();
    const help = screen.getByRole("region", { name: "Help & resources" });
    const links = within(help).getAllByRole("link");
    expect(links.map((link) => link.textContent?.replace(" (opens in a new tab)", ""))).toEqual([
      "User guide",
      "Documentation",
      "Deployment status",
    ]);
    for (const link of links) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link.getAttribute("rel")).toContain("noopener");
      expect(link).toHaveAccessibleName(/opens in a new tab/);
    }
  });
});
