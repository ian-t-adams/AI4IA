import { describe, expect, it } from "vitest";

import { SLASH_COMMANDS } from "./commands";

describe("SLASH_COMMANDS", () => {
  it("includes the research command for slash autocomplete", () => {
    expect(SLASH_COMMANDS).toContainEqual({
      name: "research",
      label: "Research",
      hint: "Search the live web and cite sources",
    });
  });
});
