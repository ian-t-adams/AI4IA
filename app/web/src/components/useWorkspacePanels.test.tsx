// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { installFakeLocalStorage } from "@/test/fakeStorage";
import { useWorkspacePanels } from "./useWorkspacePanels";

function Harness() {
  const panels = useWorkspacePanels();
  return (
    <>
      <output aria-label="Panel state">
        {[
          panels.leftIsCollapsed ? "left-collapsed" : "left-open",
          panels.rightIsCollapsed ? "right-collapsed" : "right-open",
          panels.mobileSidebarOpen ? "mobile-sidebar-open" : "",
          panels.mobileInspectorOpen ? "mobile-inspector-open" : "",
        ]
          .filter(Boolean)
          .join(" ")}
      </output>
      <button type="button" onClick={panels.toggleLeftPanel}>
        Toggle left
      </button>
      <button type="button" onClick={panels.toggleRightPanel}>
        Toggle right
      </button>
    </>
  );
}

function installMatchMedia(matches: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  );
}

let restoreLocalStorage: (() => void) | undefined;

beforeEach(() => {
  restoreLocalStorage = installFakeLocalStorage();
  installMatchMedia(false);
});

afterEach(() => {
  cleanup();
  restoreLocalStorage?.();
  restoreLocalStorage = undefined;
  vi.unstubAllGlobals();
});

describe("useWorkspacePanels", () => {
  it("persists desktop collapse choices across remounts", async () => {
    const user = userEvent.setup();
    const view = render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Toggle left" }));
    expect(screen.getByLabelText("Panel state")).toHaveTextContent(
      "left-collapsed",
    );
    expect(localStorage.getItem("ai4ia.leftCollapsed")).toBe("1");

    view.unmount();
    render(<Harness />);
    await waitFor(() =>
      expect(screen.getByLabelText("Panel state")).toHaveTextContent(
        "left-collapsed",
      ),
    );
  });

  it("keeps mobile sidebar and inspector drawers mutually exclusive", async () => {
    installMatchMedia(true);
    const user = userEvent.setup();
    render(<Harness />);

    await user.click(screen.getByRole("button", { name: "Toggle left" }));
    expect(screen.getByLabelText("Panel state")).toHaveTextContent(
      "mobile-sidebar-open",
    );
    expect(screen.getByLabelText("Panel state")).not.toHaveTextContent(
      "mobile-inspector-open",
    );

    await user.click(screen.getByRole("button", { name: "Toggle right" }));
    expect(screen.getByLabelText("Panel state")).toHaveTextContent(
      "mobile-inspector-open",
    );
    expect(screen.getByLabelText("Panel state")).not.toHaveTextContent(
      "mobile-sidebar-open",
    );
  });
});
