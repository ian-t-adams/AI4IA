import { describe, expect, it } from "vitest";
import { NextRequest } from "next/server";
import { CAPTURE_WORKLET_PATH } from "./lib/voiceLive";
import { proxy } from "./proxy";

describe("document response CSP", () => {
  it("sets every defensive directive and a fresh nonce", () => {
    const response = proxy(new NextRequest("https://app.example.test/"));
    const csp = response.headers.get("Content-Security-Policy") ?? "";
    expect(csp).toMatch(/script-src 'self' 'nonce-[^']+' 'strict-dynamic'/);
    expect(csp).toContain("style-src 'self' 'unsafe-inline'");
    expect(csp).toContain("img-src 'self' data: blob:");
    expect(csp).toContain("base-uri 'self'");
    expect(csp).toContain("object-src 'none'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).toContain("form-action 'self'");
    expect(csp).not.toMatch(/img-src[^;]*https?:/);
    expect(csp).not.toMatch(/script-src[^;]*blob:/);
    expect(csp).not.toContain("'unsafe-eval'");
  });

  it("uses a distinct nonce for each document response", () => {
    const first = proxy(new NextRequest("https://app.example.test/"));
    const second = proxy(new NextRequest("https://app.example.test/"));
    expect(first.headers.get("Content-Security-Policy")).not.toBe(
      second.headers.get("Content-Security-Policy"),
    );
  });

  it("loads the microphone worklet from the CSP-permitted same origin", () => {
    expect(CAPTURE_WORKLET_PATH).toMatch(/^\/[A-Za-z0-9._/-]+$/);
    expect(CAPTURE_WORKLET_PATH).not.toMatch(/^blob:/);
  });
});
