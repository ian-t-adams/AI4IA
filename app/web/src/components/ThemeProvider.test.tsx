// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SettingsPanel } from "./SettingsPanel";
import { ThemeProvider } from "./ThemeProvider";

function setSystemDark(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: matches && query === "(prefers-color-scheme: dark)",
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}
function inlineVar(name: string): string {
  return document.documentElement.style.getPropertyValue(name);
}

beforeEach(() => {
  setSystemDark(false);
  localStorage.clear();
  document.documentElement.removeAttribute("style");
});
afterEach(cleanup);

describe("ThemeProvider accent handling", () => {
  it("uses the system dark preference when no theme has been saved", () => {
    setSystemDark(true);
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );

    expect(document.documentElement).toHaveAttribute("data-theme", "dark");
  });

  it.each(["light", "dark", "contrast"] as const)(
    "lets an explicitly stored %s theme override the system preference",
    (savedTheme) => {
      setSystemDark(savedTheme !== "dark");
      localStorage.setItem("ai4ia-theme", JSON.stringify({ theme: savedTheme }));
      render(
        <ThemeProvider>
          <SettingsPanel onClose={() => {}} />
        </ThemeProvider>,
      );

      expect(document.documentElement).toHaveAttribute("data-theme", savedTheme);
    },
  );
  // Non-vacuity floor. Four assertions below expect an EMPTY custom property to
  // mean "no inline override". If jsdom's CSSOM ever stops round-tripping custom
  // properties, those would all pass for entirely the wrong reason. jsdom 29
  // replaced cssstyle with a css-tree implementation and jsdom 30 is already
  // known to silently reject some values it used to store (see the viewport-unit
  // note in Sidebar's tests), so this is a live risk, not a hypothetical one.
  it("jsdom can round-trip an inline custom property (guards the assertions below)", () => {
    document.documentElement.style.setProperty("--probe", "#123456");
    expect(document.documentElement.style.getPropertyValue("--probe")).toBe(
      "#123456",
    );
    document.documentElement.style.removeProperty("--probe");
    expect(document.documentElement.style.getPropertyValue("--probe")).toBe("");
  });

  it("sets no inline accent by default so the stylesheet's brand accent wins", () => {
    // The brand orange differs per theme (deep enough to be legible as link text
    // on white, bright enough to be legible on near-black). A single inline hex
    // cannot satisfy both, so "no choice" must mean "no inline override".
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    expect(inlineVar("--accent")).toBe("");
    expect(inlineVar("--accent-fg")).toBe("");
    expect(inlineVar("--user-bubble")).toBe("");
  });

  it("derives a legible foreground for a chosen accent", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Accent Blue" }));

    expect(inlineVar("--accent")).toBe("#1d4ed8");
    // Regression guard: --accent-fg used to be fixed per theme, so in dark mode
    // every dark swatch got near-black text on a near-black fill. Measured
    // against the shipped swatches that failed WCAG AA on 5 of 6.
    expect(inlineVar("--accent-fg")).toBe("#ffffff");
    expect(inlineVar("--user-bubble-fg")).toBe("#ffffff");
  });

  it("keeps the derived foreground correct after switching to dark", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Accent Blue" }));
    await user.click(screen.getByRole("button", { name: "Dark" }));

    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    // The accent is unchanged by the theme switch, so its foreground must be
    // too -- it is a property of the accent, not of the theme.
    expect(inlineVar("--accent-fg")).toBe("#ffffff");
  });

  it("returns to the brand accent when the default swatch is chosen", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Accent Blue" }));
    expect(inlineVar("--accent")).toBe("#1d4ed8");

    await user.click(
      screen.getByRole("button", { name: "Accent Brand orange (default)" }),
    );
    expect(inlineVar("--accent")).toBe("");
    expect(inlineVar("--accent-fg")).toBe("");
  });

  it("drops a persisted legacy default accent so existing users see the brand", () => {
    // The pre-rebrand default was written to localStorage automatically rather
    // than chosen, so every existing user carries it. Honouring it would pin
    // them to the old indigo forever and the rebrand would never appear.
    localStorage.setItem(
      "ai4ia-theme",
      JSON.stringify({ theme: "light", fontScale: 1, accent: "#4f46e5" }),
    );
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    expect(inlineVar("--accent")).toBe("");
  });

  it("honours a persisted accent the user actually chose", () => {
    localStorage.setItem(
      "ai4ia-theme",
      JSON.stringify({ theme: "light", fontScale: 1, accent: "#15803d" }),
    );
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    expect(inlineVar("--accent")).toBe("#15803d");
    expect(inlineVar("--accent-fg")).toBe("#ffffff");
  });

  it("ignores a persisted accent that is not a plain hex colour", () => {
    // localStorage is untrusted input and the value is written straight into a
    // CSS custom property.
    localStorage.setItem(
      "ai4ia-theme",
      JSON.stringify({ theme: "light", accent: "url(https://example.com/x.png)" }),
    );
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );
    expect(inlineVar("--accent")).toBe("");
  });

  it("removes the inline accent entirely in the high-contrast theme", async () => {
    const user = userEvent.setup();
    render(
      <ThemeProvider>
        <SettingsPanel onClose={() => {}} />
      </ThemeProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Accent Blue" }));
    await user.click(screen.getByRole("button", { name: "High contrast" }));

    // High contrast is an accessibility floor: its tested black/yellow palette
    // must not be overridden by a brand or user colour.
    expect(inlineVar("--accent")).toBe("");
    expect(inlineVar("--accent-fg")).toBe("");
    expect(inlineVar("--user-bubble")).toBe("");
  });
});
