import { describe, expect, it } from "vitest";
import { NextRequest } from "next/server";
import { proxy } from "./proxy";

describe("document response CSP", () => {
  it("prevents model-authored Markdown from loading arbitrary remote images", () => {
    const response = proxy(new NextRequest("https://app.example.test/"));
    const csp = response.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toContain("img-src 'self' data: blob:");
    expect(csp).not.toMatch(/img-src[^;]*https?:/);
  });
});
