import { afterEach, describe, expect, it, vi } from "vitest";

import { getCustomToolsConfig } from "./customToolsConfig";
import { parseEnabledFlag } from "./envFlags";
import { getLibraryConfig } from "./libraryConfig";
import { getVoiceLiveServerConfig } from "./voiceLiveConfig";

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("parseEnabledFlag", () => {
  it.each(["1", "true", "yes", "on", " true ", "TRUE"])(
    "accepts %j",
    (value) => expect(parseEnabledFlag(value)).toBe(true),
  );

  it.each([undefined, null, "", "0", "off", "false"])("rejects %j", (value) =>
    expect(parseEnabledFlag(value)).toBe(false),
  );
});

describe("server feature configs", () => {
  it("applies the same whitespace-tolerant flag parsing", () => {
    vi.stubEnv("CUSTOM_TOOLS_ENABLED", " true ");
    vi.stubEnv("DOCUMENT_LIBRARY_ENABLED", " TRUE ");
    vi.stubEnv("VOICE_LIVE_ENABLED", " yes ");
    vi.stubEnv("VOICE_LIVE_TOOLS_ENABLED", " on ");
    vi.stubEnv("API_PUBLIC_URL", "https://api.example.test/");

    expect(getCustomToolsConfig().enabled).toBe(true);
    expect(getLibraryConfig().enabled).toBe(true);
    expect(getVoiceLiveServerConfig()).toMatchObject({
      enabled: true,
      toolsAvailable: true,
      wsUrl: "wss://api.example.test/api/voice/live",
    });
  });
});
