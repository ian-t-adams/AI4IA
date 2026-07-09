import { describe, it, expect } from "vitest";

import { chunkForSpeech, TTS_CHUNK_LIMIT } from "./voice";

describe("chunkForSpeech", () => {
  it("returns a single chunk for short text", () => {
    expect(chunkForSpeech("Hello there.")).toEqual(["Hello there."]);
  });

  it("returns nothing for empty or whitespace text", () => {
    expect(chunkForSpeech("")).toEqual([]);
    expect(chunkForSpeech("   \n\n  ")).toEqual([]);
  });

  it("splits long text into sub-limit chunks at sentence boundaries", () => {
    const text = "One sentence here. Two sentence here. Three sentence here.";
    const chunks = chunkForSpeech(text, 20);
    expect(chunks.length).toBeGreaterThan(1);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(20);
    // Boundaries fall after terminators, so no chunk starts mid-sentence.
    expect(chunks.join(" ")).toContain("One sentence here.");
    expect(chunks.join(" ")).toContain("Three sentence here.");
  });

  it("hard-splits a single token longer than the limit", () => {
    const chunks = chunkForSpeech("x".repeat(45), 20);
    expect(chunks.length).toBe(3);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(20);
    expect(chunks.join("")).toBe("x".repeat(45));
  });

  it("keeps every chunk within the default limit for a very long answer", () => {
    const para = "This is a fairly normal sentence that a model might write. ";
    const chunks = chunkForSpeech(para.repeat(400));
    expect(chunks.length).toBeGreaterThan(1);
    for (const c of chunks) expect(c.length).toBeLessThanOrEqual(TTS_CHUNK_LIMIT);
  });

  it("preserves all non-whitespace content across chunks", () => {
    const text = "Alpha beta gamma. Delta epsilon zeta. Eta theta iota.";
    const chunks = chunkForSpeech(text, 15);
    const strip = (s: string) => s.replace(/\s+/g, "");
    expect(strip(chunks.join(""))).toBe(strip(text));
  });
});
