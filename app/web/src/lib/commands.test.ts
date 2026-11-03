import { describe, expect, it } from "vitest";

import { SLASH_COMMANDS } from "./commands";
import { CHAT_ONLY_TOOLS, WORKFLOW_STEP_TOOLS, WORKFLOW_TEMPLATES } from "./workflowTemplates";
import { ATTACHABLE_TOOLS } from "./studio";
import { BUILT_IN_TOOL_HELP, TOOL_LABELS } from "./toolHelp";

describe("SLASH_COMMANDS", () => {
  it("includes the research command for slash autocomplete", () => {
    expect(SLASH_COMMANDS).toContainEqual({
      name: "research",
      label: "Research",
      hint: "Search the live web and cite sources",
    });
  });

  it("removes video generation affordances without removing image tools or workflow templates", () => {
    const commandNames = SLASH_COMMANDS.map(({ name }) => name);
    expect(commandNames).not.toContain("generate_video");
    expect(commandNames).toContain("generate_image");

    expect(ATTACHABLE_TOOLS).not.toContain("generate_video");
    expect(TOOL_LABELS).not.toHaveProperty("generate_video");
    expect(BUILT_IN_TOOL_HELP).not.toHaveProperty("generate_video");
    expect(CHAT_ONLY_TOOLS).not.toContain("generate_video");
    expect(WORKFLOW_STEP_TOOLS).not.toContain("generate_video");
    expect(WORKFLOW_TEMPLATES.length).toBeGreaterThan(0);
  });
});
