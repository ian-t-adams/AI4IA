import { afterEach, describe, expect, it, vi } from "vitest";

import { GET, dynamic } from "./route";

describe("GET /build-id", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("returns the inlined build identifier and is never cached", async () => {
    vi.stubEnv("AI4IA_WEB_BUILD_ID", "3f9c2b7a1d0e4c5b6a7f");
    const response = GET();
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({ buildId: "3f9c2b7a1d0e4c5b6a7f" });
    expect(dynamic).toBe("force-dynamic");
  });

  it("reports no identifier as null, which clients treat as unknown", async () => {
    vi.stubEnv("AI4IA_WEB_BUILD_ID", undefined);
    await expect(GET().json()).resolves.toEqual({ buildId: null });
  });
});
