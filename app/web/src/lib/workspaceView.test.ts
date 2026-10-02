import { describe, expect, it } from "vitest";

import { hashForView, viewFromHash, type WorkspaceView } from "./workspaceView";

describe("workspace view routing", () => {
  it("round-trips every destination through the URL hash", () => {
    const views: WorkspaceView[] = ["chat", "library", "avatars", "studio", "settings"];
    for (const view of views) {
      expect(viewFromHash(hashForView(view))).toBe(view);
    }
    expect(hashForView("chat")).toBe("");
    expect(hashForView("avatars")).toBe("#/avatars");
  });

  it("accepts the short and slash forms, case-insensitively, ignoring any tail", () => {
    expect(viewFromHash("#avatars")).toBe("avatars");
    expect(viewFromHash("#/Library")).toBe("library");
    expect(viewFromHash("#/studio/runs/abc")).toBe("studio");
    expect(viewFromHash("#/settings?tab=appearance")).toBe("settings");
  });

  it("falls back to the conversation for anything it does not know", () => {
    for (const hash of ["", "#", "#/", "#/admin", "#chat", "#/avatarsX", "#/../settings"]) {
      expect(viewFromHash(hash)).toBe("chat");
    }
  });
});
