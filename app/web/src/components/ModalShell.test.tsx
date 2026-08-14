// @vitest-environment jsdom
import { useState } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ModalShell } from "./ModalShell";

afterEach(cleanup);

function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open dialog
      </button>
      {open ? (
        <ModalShell
          ariaLabel="Test dialog"
          title="Dialog title"
          closeLabel="Close dialog"
          onClose={() => setOpen(false)}
        >
          <button type="button">First action</button>
          <button type="button">Last action</button>
        </ModalShell>
      ) : null}
    </>
  );
}

describe("ModalShell", () => {
  it("traps focus, closes on Escape, and restores the opener", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open dialog" });

    await user.click(opener);
    const first = screen.getByRole("button", { name: "Close dialog" });
    const last = screen.getByRole("button", { name: "Last action" });
    await waitFor(() => expect(first).toHaveFocus());

    await user.tab({ shift: true });
    expect(last).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("dialog", { name: "Test dialog" })).toBeNull();
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("closes from the backdrop but not from its content surface", async () => {
    const onClose = vi.fn();
    render(
      <ModalShell
        ariaLabel="Test dialog"
        title="Dialog title"
        closeLabel="Close dialog"
        onClose={onClose}
      >
        <button type="button">Inside action</button>
      </ModalShell>,
    );
    const dialog = screen.getByRole("dialog", { name: "Test dialog" });

    fireEvent.click(screen.getByRole("button", { name: "Inside action" }));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Close dialog" })).toHaveStyle({
      minWidth: "44px",
      minHeight: "44px",
    });
  });
});
