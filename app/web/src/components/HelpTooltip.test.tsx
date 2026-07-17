// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
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

  it("clamps the portal within a short narrow viewport", async () => {
    Object.defineProperty(window, "innerWidth", { value: 240, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 120, configurable: true });
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        return (
          this.getAttribute("role") === "tooltip"
            ? { top: 0, bottom: 160, left: 0, right: 320, width: 320, height: 160 }
            : { top: 90, bottom: 110, left: 210, right: 230, width: 20, height: 20 }
        ) as DOMRect;
      });
    const user = userEvent.setup();
    render(
      <HelpTooltip label="Bounds">
        {Array.from({ length: 30 }, () => "Long bounded help content. ").join("")}
      </HelpTooltip>,
    );
    const trigger = screen.getByRole("button", { name: "Help: Bounds" });
    await user.click(trigger);
    const tooltip = screen.getByRole("tooltip");
    await waitFor(() => {
      expect(tooltip).toHaveStyle({ top: "16px", left: "16px" });
      expect(tooltip).toHaveStyle({
        maxHeight: "calc(100dvh - 32px)",
        overflowY: "auto",
      });
    });
    rect.mockRestore();
  });
});
