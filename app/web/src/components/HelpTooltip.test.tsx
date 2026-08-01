// @vitest-environment jsdom
import { useEffect } from "react";
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

  it("consumes Escape to close itself without also closing an ancestor dialog, but lets a later Escape reach it once already closed", async () => {
    const user = userEvent.setup();
    const onDialogKeyDown = vi.fn();
    render(
      <div onKeyDown={onDialogKeyDown}>
        <HelpTooltip label="Nested">Some help text.</HelpTooltip>
      </div>,
    );
    const trigger = screen.getByRole("button", { name: "Help: Nested" });
    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
    // The enclosing dialog/drawer must not also see this keypress -- otherwise
    // one Escape would dismiss both the tooltip and its containing modal.
    expect(onDialogKeyDown).not.toHaveBeenCalled();

    // Once the tooltip is already closed, Escape is no longer this trigger's
    // concern, so it must propagate normally to the enclosing dialog.
    await user.keyboard("{Escape}");
    expect(onDialogKeyDown).toHaveBeenCalledTimes(1);
  });

  it("closes on Escape when opened via hover, even though the trigger never received focus", async () => {
    const user = userEvent.setup();
    render(<HelpTooltip label="Hover">Some help text.</HelpTooltip>);
    const trigger = screen.getByRole("button", { name: "Help: Hover" });
    await user.hover(trigger);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    expect(trigger).not.toHaveFocus();
    // Escape is dispatched to document.activeElement (document.body here,
    // since hover never focuses anything) -- the old trigger-only onKeyDown
    // could never see this keypress.
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("closes a click-pinned tooltip on Escape after Tab has moved focus to the next field", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <HelpTooltip label="Pinned">Some help text.</HelpTooltip>
        <button type="button">Next field</button>
      </div>,
    );
    const trigger = screen.getByRole("button", { name: "Help: Pinned" });
    await user.click(trigger);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    await user.tab();
    expect(screen.getByRole("button", { name: "Next field" })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("stops Escape from also closing a real containing dialog's window keydown listener", async () => {
    // Mirrors how MediaPlayer actually closes on Escape
    // (window.addEventListener("keydown", ...), bubble phase) -- distinct
    // from the React onKeyDown ancestor case above, so this proves the
    // document-capture fix wins against that real-world pattern too.
    const user = userEvent.setup();
    const onDialogClose = vi.fn();
    function DialogLike() {
      useEffect(() => {
        const onKey = (event: KeyboardEvent) => {
          if (event.key === "Escape") onDialogClose();
        };
        window.addEventListener("keydown", onKey);
        return () => window.removeEventListener("keydown", onKey);
      }, []);
      return <HelpTooltip label="InDialog">Some help text.</HelpTooltip>;
    }
    render(<DialogLike />);
    const trigger = screen.getByRole("button", { name: "Help: InDialog" });
    await user.hover(trigger);
    expect(screen.getByRole("tooltip")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("tooltip")).toBeNull();
    expect(onDialogClose).not.toHaveBeenCalled();

    // Once closed, Escape must reach the real dialog listener again.
    await user.keyboard("{Escape}");
    expect(onDialogClose).toHaveBeenCalledTimes(1);
  });

  it("applies the compact trigger class when size is sm, and the default class otherwise", () => {
    const { rerender } = render(
      <HelpTooltip label="Risk" size="sm">
        External tools can reach third-party services.
      </HelpTooltip>,
    );
    expect(screen.getByRole("button", { name: "Help: Risk" })).toHaveClass(
      "help-trigger",
      "help-trigger-sm",
    );
    rerender(
      <HelpTooltip label="Risk">
        External tools can reach third-party services.
      </HelpTooltip>,
    );
    const defaultTrigger = screen.getByRole("button", { name: "Help: Risk" });
    expect(defaultTrigger).toHaveClass("help-trigger");
    expect(defaultTrigger).not.toHaveClass("help-trigger-sm");
  });

  it("does not toggle an ancestor checkbox when nested inside its <label>", async () => {
    const user = userEvent.setup();
    render(
      <label>
        <input type="checkbox" />
        Enable calculator
        <HelpTooltip label="Calculator">Evaluates basic arithmetic.</HelpTooltip>
      </label>,
    );
    const checkbox = screen.getByRole("checkbox");
    expect(checkbox).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Help: Calculator" }));
    expect(screen.getByRole("tooltip")).toBeInTheDocument();
    expect(checkbox).not.toBeChecked();
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
        {"X".repeat(500)}
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
        overflowX: "hidden",
        overflowWrap: "anywhere",
      });
    });
    rect.mockRestore();
  });
});
