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


describe("server-reported WebIQ capabilities", () => {
  it("renders structured and newly discovered capabilities beyond the original five", () => {
    const names = ["web_search", "news_search", "video_search", "image_search", "browse_url",
      "classic_search", "finance_search", "places_search", "sports_search", "sonic_search", "web_autosuggest", "future_webiq_tool"];
    const catalog = names.map((name) => ({ ...tool(name), source: "webiq", label: name.replaceAll("_", " "), description: `Description for ${name}` }));
    catalog[10].available = false;
    catalog[10].detail = "Requires upstream entitlement";
    const chips = stepCapabilities({ attached: [], extra: [], catalog, selectedDocCount: 0, agentLabel: "Helper" });
    for (const name of names) expect(chips.find((chip) => chip.key === name)).toBeDefined();
    expect(chips.find((chip) => chip.key === "finance_search")).toMatchObject({ state: "ambient" });
    expect(chips.find((chip) => chip.key === "web_autosuggest")).toMatchObject({ state: "off" });
    expect(chips.find((chip) => chip.key === "web_autosuggest")?.help).toContain("Requires upstream entitlement");
    expect(chips.find((chip) => chip.key === "classic_search")?.help).toContain("not necessarily slash commands");
  });

  it("does not fabricate availability or a five-tool count when the server does not list WebIQ", () => {
    const chips = stepCapabilities({ attached: [], extra: [], catalog: [], selectedDocCount: 0, agentLabel: "Helper" });
    expect(chips.find((chip) => chip.key === "web_search")).toMatchObject({ state: "conditional" });
    expect(chips.find((chip) => chip.key === "web_search")?.help).toContain("no enabled state or tool count");
  });
});
