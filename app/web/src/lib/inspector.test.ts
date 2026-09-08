import { afterEach, describe, expect, it, vi } from "vitest";
import { getMemoryPreference, updateMemoryPreference } from "./inspector";

const fetch = vi.hoisted(() => vi.fn());
vi.mock("./auth", () => ({ apiFetch: fetch }));

afterEach(() => vi.resetAllMocks());

describe("memory preference API", () => {
  it("reads through apiFetch without caching or accepting an owner id", async () => {
    fetch.mockResolvedValue(Response.json({ automaticMemoryEnabled: true, etag: '"pref-0"' }));
    expect(await getMemoryPreference()).toEqual({ automaticMemoryEnabled: true, etag: '"pref-0"' });
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/memories/preference", { cache: "no-store" });
  });

  it("sends a typed boolean and conditional preference ETag", async () => {
    fetch.mockResolvedValue(Response.json({ automaticMemoryEnabled: false, etag: '"pref-1"' }));
    expect((await updateMemoryPreference(false, '"pref-0"')).automaticMemoryEnabled).toBe(false);
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/memories/preference", {
      method: "PATCH", cache: "no-store",
      headers: { "Content-Type": "application/json", "If-Match": '"pref-0"' },
      body: JSON.stringify({ automaticMemoryEnabled: false }),
    });
  });

  it.each([null, {}, { automaticMemoryEnabled: "false", etag: "e" }])("rejects malformed preference %j", async (value) => {
    fetch.mockResolvedValue(Response.json(value));
    await expect(getMemoryPreference()).rejects.toThrow("invalid memory preference");
  });

  it("propagates storage failures instead of manufacturing an enabled default", async () => {
    fetch.mockResolvedValue(Response.json({ detail: "Unavailable" }, { status: 503 }));
    await expect(getMemoryPreference()).rejects.toThrow("503: Unavailable");
  });
});
