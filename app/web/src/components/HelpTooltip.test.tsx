// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { HelpTooltip } from "./HelpTooltip";

afterEach(cleanup);

describe("HelpTooltip", () => {
  it("is keyboard focusable and associates visible help with aria-describedby", async () => {
    const user = userEvent.setup();
    render(
      <HelpTooltip label="Temperature">
        Higher values increase variation; the default is 0.7.
      </HelpTooltip>,
    );
    const trigger = screen.getByRole("button", { name: "Help: Temperature" });
    await user.tab();
    expect(trigger).toHaveFocus();
    const tooltip = screen.getByRole("tooltip");
    expect(trigger).toHaveAttribute("aria-describedby", tooltip.id);
    expect(tooltip).toHaveTextContent("default is 0.7");
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("keeps click-pinned help open across mouseleave and closes outside", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <HelpTooltip label="Usage">Known totals may be partial.</HelpTooltip>
        <button type="button">Outside</button>
      </div>,
    );
    const trigger = screen.getByRole("button", { name: "Help: Usage" });
    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    await user.unhover(trigger);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Outside" }));
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
