// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { StaleBuildBanner } from "./StaleBuildBanner";

afterEach(() => {
  cleanup();
});

describe("StaleBuildBanner", () => {
  it("announces the new version and reloads only when asked", async () => {
    const onReload = vi.fn();
    render(<StaleBuildBanner blockedReason={null} onReload={onReload} />);
    expect(screen.getByRole("status")).toHaveTextContent("A new version of AI4IA is available.");
    const reload = screen.getByRole("button", { name: "Reload" });
    expect(reload).toBeEnabled();
    expect(reload).toHaveAccessibleDescription(/New voice sessions start after you reload/);
    expect(onReload).not.toHaveBeenCalled();
    await userEvent.setup().click(reload);
    expect(onReload).toHaveBeenCalledTimes(1);
  });

  it("explains why it waits while reloading would lose work", async () => {
    const onReload = vi.fn();
    render(
      <StaleBuildBanner blockedReason="Reload after your voice session ends." onReload={onReload} />,
    );
    const reload = screen.getByRole("button", { name: "Reload" });
    expect(reload).toBeDisabled();
    expect(reload).toHaveAccessibleDescription("Reload after your voice session ends.");
    await userEvent.setup().click(reload);
    expect(onReload).not.toHaveBeenCalled();
  });
});
