import { describe, expect, it } from "vitest";

import { ATTACHABLE_TOOLS } from "./studio";
import { BUILT_IN_TOOL_HELP, TOOL_LABELS, toolRiskSummary } from "./toolHelp";

describe("BUILT_IN_TOOL_HELP", () => {
  it("has help copy for every attachable built-in tool, with non-empty what/when/tradeoffs", () => {
    for (const name of ATTACHABLE_TOOLS) {
      const help = BUILT_IN_TOOL_HELP[name];
      expect(help, `missing help copy for ${name}`).toBeDefined();
      expect(help.what.length).toBeGreaterThan(0);
      expect(help.when.length).toBeGreaterThan(0);
      expect(help.tradeoffs.length).toBeGreaterThan(0);
      expect(["safe", "external", "destructive"]).toContain(help.risk);
    }
  });

  it("does not define help for tools outside the attachable set (keeps the map in sync)", () => {
    const attachable = new Set<string>(ATTACHABLE_TOOLS);
    for (const name of Object.keys(BUILT_IN_TOOL_HELP)) {
      expect(attachable.has(name), `${name} is not in ATTACHABLE_TOOLS`).toBe(true);
    }
  });
});

describe("TOOL_LABELS", () => {
  it("labels every attachable tool, so no checkbox falls back to a raw tool name", () => {
    // Shared by the agent builder and the workflow step tool picker. A tool with
    // no entry silently renders its snake_case name in both.
    for (const name of ATTACHABLE_TOOLS) {
      expect(TOOL_LABELS[name], `missing label for ${name}`).toBeTruthy();
    }
  });
});

describe("toolRiskSummary", () => {
  it("describes each risk level in plain language matching backend semantics", () => {
    expect(toolRiskSummary("safe")).toMatch(/no third-party network access/i);
    expect(toolRiskSummary("external")).toMatch(/third-party services/i);
    expect(toolRiskSummary("destructive")).toMatch(/change or delete data/i);
  });
});
