import { describe, expect, it } from "vitest";
import { nameError, suggestedStudioName } from "./studio";

describe("new Studio ID suggestions", () => {
  it.each(["Research assistant", "Résumé", "123 notes", "中文", "a".repeat(80)])(
    "derives a valid ID from %s without constraining the display name", (label) => {
      expect(nameError(suggestedStudioName(label, [], "agent"))).toBeNull();
    },
  );
  it("avoids saved and built-in names within the same length bound", () => {
    expect(suggestedStudioName("Helper", ["helper", "helper-2"], "agent")).toBe("helper-3");
    const base = "a".repeat(32);
    const next = suggestedStudioName(base, [base], "agent");
    expect(next).toHaveLength(32);
    expect(next.endsWith("-2")).toBe(true);
    expect(nameError(next)).toBeNull();
  });
});
