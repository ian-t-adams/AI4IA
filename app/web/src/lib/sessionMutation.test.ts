import { describe, expect, it, vi } from "vitest";

import { commitLatestSessionMutation } from "./sessionMutation";

describe("session mutation guards", () => {
  it("discards a late model save after switching from A to B", async () => {
    let currentSession: string | null = "A";
    let currentGeneration = 1;
    let resolve!: (value: string) => void;
    const operation = new Promise<string>((done) => {
      resolve = done;
    });
    const commit = vi.fn();
    const pending = commitLatestSessionMutation({
      capturedSession: "A",
      capturedGeneration: 1,
      currentSession: () => currentSession,
      currentGeneration: () => currentGeneration,
      operation: () => operation,
      commit,
    });
    currentSession = "B";
    currentGeneration = 2;
    resolve("saved A");
    await expect(pending).resolves.toBe(false);
    expect(commit).not.toHaveBeenCalled();
  });
});
