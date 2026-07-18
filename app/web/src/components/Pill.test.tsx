// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { Pill, pillToneColor } from "./Pill";

afterEach(cleanup);

describe("pillToneColor", () => {
  it("maps every tone to a distinct, stable color", () => {
    expect(pillToneColor("ok")).toBe("#15803d");
    expect(pillToneColor("warn")).toBe("#b45309");
    expect(pillToneColor("error")).toBe("var(--danger)");
    expect(pillToneColor("neutral")).toBe("var(--fg)");
    expect(pillToneColor("muted")).toBe("var(--fg-muted)");
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
