// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState, type RefObject } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useModalFocus, useModalKeyDown } from "./useModalFocus";

function Panel({
  onClose,
  enabled = true,
  openerRef,
}: {
  onClose: () => void;
  enabled?: boolean;
  openerRef?: RefObject<HTMLElement | null>;
}) {
  const ref = useModalFocus(enabled, openerRef);
  const onKeyDown = useModalKeyDown(onClose, enabled);
  return (
    <div ref={ref} role="dialog" aria-label="Fixture" onKeyDown={onKeyDown}>
      <button type="button" onClick={onClose}>Close</button>
      <h2 tabIndex={-1}>Result</h2>
      <label>Name<input /></label>
      <button type="button">Last action</button>
    </div>
  );
}

function Harness({ enabled = true, replaceOpener = false }) {
  const [open, setOpen] = useState(false);
  const openerRef = useRef<HTMLButtonElement>(null);
  return (
    <>
      {!replaceOpener || !open ? (
        <button ref={openerRef} type="button" onClick={() => setOpen(true)}>Open</button>
      ) : null}
      <button type="button">Outside</button>
      {open ? (
        <Panel
          enabled={enabled}
          openerRef={replaceOpener ? openerRef : undefined}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </>
  );
}

function open() {
  const opener = screen.getByRole("button", { name: "Open" });
  opener.focus();
  fireEvent.click(opener);
  return opener;
}

function frame() {
  act(() => vi.advanceTimersToNextFrame());
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame"] });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("modal initial focus ownership", () => {
  it("focuses the first control when no descendant has taken focus", () => {
    render(<Harness />);
    const opener = open();
    expect(opener).toHaveFocus();
    expect(vi.getTimerCount()).toBe(1);

    frame();

    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
  });

  it.each(["input", "heading"] as const)(
    "keeps deliberate %s focus when the initial frame runs later",
    (target) => {
      render(<Harness />);
      open();
      const chosen = target === "input"
        ? screen.getByLabelText("Name")
        : screen.getByRole("heading", { name: "Result" });
      chosen.focus();
      expect(chosen).toHaveFocus();
      expect(vi.getTimerCount()).toBe(1);

      frame();

      expect(chosen).toHaveFocus();
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it("does not leave focus outside the dialog when its initial frame runs", () => {
    render(<Harness />);
    open();
    const outside = screen.getByRole("button", { name: "Outside" });
    outside.focus();

    frame();

    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
  });

  it("cancels initial focus on close, returns to the opener and focuses on reopen", () => {
    render(<Harness />);
    const opener = open();
    screen.getByLabelText("Name").focus();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(vi.getTimerCount()).toBe(0);
    expect(opener).toHaveFocus();

    frame();
    expect(opener).toHaveFocus();
    open();
    expect(vi.getTimerCount()).toBe(1);
    frame();
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
  });

  it("cancels the pending frame when disabled without taking focus back again", () => {
    const { rerender } = render(<Harness />);
    const opener = open();
    screen.getByLabelText("Name").focus();

    rerender(<Harness enabled={false} />);
    expect(vi.getTimerCount()).toBe(0);
    expect(opener).toHaveFocus();
    const outside = screen.getByRole("button", { name: "Outside" });
    outside.focus();
    frame();
    expect(outside).toHaveFocus();
  });

  it("cancels the queued frame on unmount and restores the external opener", () => {
    const opener = document.createElement("button");
    document.body.append(opener);
    opener.focus();
    try {
      const { unmount } = render(<Panel onClose={() => {}} />);
      screen.getByLabelText("Name").focus();
      expect(vi.getTimerCount()).toBe(1);

      unmount();

      expect(vi.getTimerCount()).toBe(0);
      expect(opener).toHaveFocus();
      frame();
      expect(opener).toHaveFocus();
    } finally {
      opener.remove();
    }
  });

  it("returns to a remounted explicit opener rather than its detached predecessor", () => {
    render(<Harness replaceOpener />);
    const original = open();
    expect(original.isConnected).toBe(false);
    frame();
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });

    const replacement = screen.getByRole("button", { name: "Open" });
    expect(replacement).not.toBe(original);
    expect(replacement).toHaveFocus();
  });

  it("keeps forward and reverse Tab wrapping after deliberate child focus", () => {
    render(<Harness />);
    open();
    screen.getByLabelText("Name").focus();
    frame();
    const first = screen.getByRole("button", { name: "Close" });
    const last = screen.getByRole("button", { name: "Last action" });
    last.focus();
    fireEvent.keyDown(last, { key: "Tab" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();
  });
});
