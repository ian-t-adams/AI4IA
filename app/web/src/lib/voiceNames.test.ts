import { describe, expect, it } from "vitest";

import { formatVoiceName } from "./voiceNames";

describe("formatVoiceName", () => {
  it("reads Azure Speech HD voice ids as a name with locale and family", () => {
    expect(formatVoiceName("en-US-Ava:DragonHDLatestNeural")).toBe("Ava (en-US, Dragon HD)");
    expect(formatVoiceName("en-US-Brian:DragonHDFlashLatestNeural")).toBe(
      "Brian (en-US, Dragon HD Flash)",
    );
  });

  it("reads classic and multilingual neural voice ids", () => {
    expect(formatVoiceName("en-US-AndrewNeural")).toBe("Andrew (en-US)");
    expect(formatVoiceName("en-US-AndrewMultilingualNeural")).toBe("Andrew (en-US, Multilingual)");
    expect(formatVoiceName("zh-CN-XiaoxiaoNeural")).toBe("Xiaoxiao (zh-CN)");
  });

  it("leaves other voice names untouched", () => {
    for (const voice of ["alloy", "marin", "custom-endpoint:voice", "en-US", ""]) {
      expect(formatVoiceName(voice)).toBe(voice);
    }
  });
});
