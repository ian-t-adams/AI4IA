import { afterEach, describe, expect, it, vi } from "vitest";

import { getChatPilotUiConfig } from "./chatPilotUiConfig";

afterEach(() => vi.unstubAllEnvs());

describe("chat-pilot UI configuration", () => {
  it("is disabled when unset", () => {
    vi.stubEnv("CHAT_PILOT_UI_ENABLED", "");
    expect(getChatPilotUiConfig()).toEqual({ enabled: false });
  });

  it("accepts the existing server-flag truthy values", () => {
    vi.stubEnv("CHAT_PILOT_UI_ENABLED", "true");
    expect(getChatPilotUiConfig()).toEqual({ enabled: true });
  });
});
