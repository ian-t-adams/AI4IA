import { describe, expect, it } from "vitest";

import {
  closeUnavailableMobileDrawer,
  toggleMobileDrawer,
} from "./workspaceLayout";

describe("mobile workspace drawers", () => {
  it("defaults closed, toggles one drawer, and makes drawers mutually exclusive", () => {
    expect(toggleMobileDrawer(null, "sidebar")).toBe("sidebar");
    expect(toggleMobileDrawer("sidebar", "sidebar")).toBeNull();
    expect(toggleMobileDrawer("sidebar", "inspector")).toBe("inspector");
    expect(toggleMobileDrawer("inspector", "sidebar")).toBe("sidebar");
  });

  it("closes a drawer when its responsive breakpoint is no longer active", () => {
    expect(closeUnavailableMobileDrawer("sidebar", false, true)).toBeNull();
    expect(closeUnavailableMobileDrawer("inspector", true, false)).toBeNull();
    expect(closeUnavailableMobileDrawer("sidebar", true, true)).toBe("sidebar");
    expect(closeUnavailableMobileDrawer("inspector", true, true)).toBe("inspector");
  });
});
