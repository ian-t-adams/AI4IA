// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Pill, pillToneColor, type PillTone } from "./Pill";

afterEach(cleanup);

describe("pillToneColor", () => {
  it("maps every tone to a distinct theme token, never a literal color", () => {
    expect(pillToneColor("ok")).toBe("var(--success)");
    expect(pillToneColor("warn")).toBe("var(--warn)");
    expect(pillToneColor("error")).toBe("var(--danger)");
    expect(pillToneColor("neutral")).toBe("var(--fg)");
    expect(pillToneColor("muted")).toBe("var(--fg-muted)");
  });

  // Literal hexes are what this mapping used to contain: #15803d for "ok" and
  // #b45309 for "warn". Both were picked against the light theme and are only
  // 3.45:1 and 3.44:1 on the dark surface, so a pill silently failed AA for
  // anyone not using the default theme. Tokens re-resolve per theme; a literal
  // cannot. Assert the shape so a literal can't creep back in.
  it("returns no literal colors for any tone", () => {
    const tones: PillTone[] = ["ok", "warn", "error", "neutral", "muted"];
    for (const tone of tones) {
      expect(pillToneColor(tone)).toMatch(/^var\(--[a-z-]+\)$/);
    }
  });
});

describe("Pill", () => {
  it("renders the label with no help trigger when detail is omitted", () => {
    render(<Pill label="Healthy" tone="ok" />);
    expect(screen.getByText("Healthy")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("exposes detail text via an accessible, keyboard-reachable help trigger instead of a bare title attribute", async () => {
    const user = userEvent.setup();
    render(
      <Pill
        label="Degraded"
        tone="warn"
        detail="Recent connection failures."
        helpLabel="Health: Degraded"
      />,
    );
    const trigger = screen.getByRole("button", { name: "Help: Health: Degraded" });
    expect(trigger).not.toHaveAttribute("title");
    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toHaveTextContent("Recent connection failures.");
  });
});
