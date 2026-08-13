import { describe, expect, it } from "vitest";

import type { ToolCatalogItem } from "@/lib/types";
import { stepCapabilities } from "./workflowCapabilities";

function tool(name: string, available = true): ToolCatalogItem {
  return {
    name,
    label: name,
    description: "",
    source: "builtin",
    risk: null,
    requiresApproval: null,
    scopes: null,
    available,
    selectable: true,
    ownership: "server",
    typed: true,
    voice: false,
  };
}

describe("stepCapabilities", () => {
  it("marks only canonical mcp:<server>/<tool> names as MCP chat-only", () => {
    const chips = stepCapabilities({
      attached: ["mcp:calendar/create_event", "custom/tool"],
      extra: [],
      catalog: [tool("process_document"), tool("remember_memory")],
      selectedDocCount: 0,
      agentLabel: "Helper",
    });

    expect(chips.find((chip) => chip.key === "mcp:calendar/create_event")).toMatchObject({
      state: "chat-only",
    });
    expect(chips.some((chip) => chip.key === "custom/tool")).toBe(false);
  });
});
