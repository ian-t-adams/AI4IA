// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { SkipLink } from "./SkipLink";

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
});

describe("SkipLink", () => {
  it("retains native hash navigation and explicitly focuses an unfocusable main", async () => {
    const user = userEvent.setup();
    render(
      <>
        <SkipLink />
        <main id="main">Main surface</main>
      </>,
    );

    await user.click(screen.getByRole("link", { name: "Skip to main content" }));

    const main = screen.getByRole("main");
    expect(window.location.hash).toBe("#main");
    expect(main).toHaveFocus();
    expect(main).toHaveAttribute("tabindex", "-1");
  });

  it("keeps native hash navigation when a route has no target yet", async () => {
    const user = userEvent.setup();
    render(<SkipLink />);

    await user.click(screen.getByRole("link", { name: "Skip to main content" }));

    expect(window.location.hash).toBe("#main");
    expect(document.activeElement).not.toBe(document.body.querySelector("#main"));
  });
});
