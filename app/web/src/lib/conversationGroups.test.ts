import { describe, expect, it } from "vitest";

import { groupConversations, matchesConversationQuery } from "./conversationGroups";

const NOW = new Date(2026, 9, 2, 9, 30);
const at = (year: number, month: number, day: number, hour = 12) =>
  new Date(year, month, day, hour).toISOString();

describe("groupConversations", () => {
  it("buckets by local calendar day while keeping the server's order inside each group", () => {
    const items = [
      { id: "a", updatedAt: at(2026, 9, 2, 8) },
      { id: "b", updatedAt: at(2026, 9, 1, 23) },
      { id: "c", updatedAt: at(2026, 9, 2, 0) },
      { id: "d", updatedAt: at(2026, 8, 28) },
      { id: "e", updatedAt: at(2026, 8, 10) },
      { id: "f", updatedAt: at(2025, 0, 1) },
    ];
    const groups = groupConversations(items, NOW);
    expect(groups.map((group) => [group.title, group.items.map((item) => item.id)])).toEqual([
      ["Today", ["a", "c"]],
      ["Yesterday", ["b"]],
      ["Previous 7 days", ["d"]],
      ["Previous 30 days", ["e"]],
      ["Older", ["f"]],
    ]);
  });

  it("falls back to createdAt and files undated items with the oldest", () => {
    const groups = groupConversations(
      [
        { id: "new", createdAt: at(2026, 9, 2, 7), updatedAt: "" },
        { id: "unknown", createdAt: "not a date", updatedAt: null },
      ],
      NOW,
    );
    expect(groups.map((group) => [group.title, group.items.map((item) => item.id)])).toEqual([
      ["Today", ["new"]],
      ["Older", ["unknown"]],
    ]);
  });

  it("never invents dates: an undated list is one untitled group, an empty list none", () => {
    const items = [{ id: "x", createdAt: "", updatedAt: "" }, { id: "y" }];
    expect(groupConversations(items, NOW)).toEqual([{ title: null, items }]);
    expect(groupConversations([], NOW)).toEqual([]);
  });
});

describe("matchesConversationQuery", () => {
  it("matches titles case-insensitively and treats a blank query as everything", () => {
    expect(matchesConversationQuery("Quarterly Planning", "planning")).toBe(true);
    expect(matchesConversationQuery("Quarterly Planning", "  ")).toBe(true);
    expect(matchesConversationQuery("Quarterly Planning", "budget")).toBe(false);
  });

  it("searches an untitled conversation by the name it is shown with", () => {
    expect(matchesConversationQuery("", "untitled")).toBe(true);
    expect(matchesConversationQuery(null, "untit")).toBe(true);
  });
});
