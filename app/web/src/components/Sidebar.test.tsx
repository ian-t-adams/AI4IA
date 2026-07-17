// @vitest-environment jsdom
import { useRef, useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Sidebar } from "./Sidebar";

vi.mock("./AdminLink", () => ({ AdminLink: () => null }));
vi.mock("./UserMenu", () => ({ UserMenu: () => null }));

beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({
      matches: true,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("responsive sidebar", () => {
  it("makes the background inert and restores explicit opener focus on every close path", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      const openerRef = useRef<HTMLElement | null>(null);
      return (
        <div style={{ width: 320, fontSize: "200%" }}>
          {!open ? (
            <button
              ref={(element) => {
                if (element) openerRef.current = element;
              }}
              type="button"
              onClick={() => setOpen(true)}
            >
              Open conversations
            </button>
          ) : (
            <>
              <button
                type="button"
                aria-label="Close conversations backdrop"
                onClick={() => setOpen(false)}
              />
              <Sidebar
                sessions={[
                  {
                    id: "s1",
                    userId: "u1",
                    title: "A very long conversation title that must not overlap controls",
                    model: null,
                    systemPrompt: null,
                    agentName: null,
                    toolOverrides: { added: [], removed: [] },
                    libraryDocumentIds: [],
                    createdAt: "",
                    updatedAt: "",
                  },
                ]}
                activeId="s1"
                onSelect={vi.fn()}
                onNewChat={vi.fn()}
                onDelete={vi.fn()}
                onOpenSettings={vi.fn()}
                onOpenStudio={vi.fn()}
                onCollapse={() => setOpen(false)}
                openerRef={openerRef}
              />
            </>
          )}
          <main inert={open ? true : undefined} aria-hidden={open ? true : undefined}>
            Conversation
          </main>
        </div>
      );
    }
    const user = userEvent.setup();
    render(<Harness />);
    const open = async () => {
      await user.click(
        screen.getByRole("button", { name: "Open conversations" }),
      );
      expect(document.querySelector("main")).toHaveAttribute("inert");
      return screen.getByRole("dialog", { name: "Chat sessions" });
    };

    let dialog = await open();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(
      await screen.findByRole("button", { name: "Open conversations" }),
    ).toHaveFocus();

    await open();
    await user.click(
      screen.getByRole("button", { name: "Close conversations backdrop" }),
    );
    expect(
      await screen.findByRole("button", { name: "Open conversations" }),
    ).toHaveFocus();

    dialog = await open();
    expect(screen.getByTestId("sidebar-scroll")).toHaveStyle({
      minHeight: "0",
      overflowY: "auto",
      overflowX: "hidden",
    });
    expect(dialog).toHaveStyle({
      maxWidth: "100vw",
      maxHeight: "100dvh",
      overflow: "hidden",
    });
    expect(
      screen.getByRole("button", {
        name: "A very long conversation title that must not overlap controls",
      }),
    ).toHaveStyle({ overflow: "hidden", textOverflow: "ellipsis" });
    const status = screen.getByRole("link", {
      name: "Status (opens in new tab)",
    });
    status.focus();
    expect(status).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(
      await screen.findByRole("button", { name: "Open conversations" }),
    ).toHaveFocus();
    expect(dialog).not.toBeInTheDocument();
  });
});
