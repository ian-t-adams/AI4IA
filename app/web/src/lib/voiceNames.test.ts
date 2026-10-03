import { describe, expect, it } from "vitest";

import { voiceProviderCatalog } from "./data/voice_provider_catalog";
import { formatVoiceName, isPreviewVoice } from "./voiceNames";

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

  it("reads persona names with a digit or joined words", () => {
    expect(formatVoiceName("en-US-Andrew2:DragonHDLatestNeural")).toBe("Andrew2 (en-US, Dragon HD)");
    expect(formatVoiceName("en-US-Emma2:DragonHDLatestNeural")).toBe("Emma2 (en-US, Dragon HD)");
    expect(formatVoiceName("en-US-AlloyTurboMultilingualNeural")).toBe(
      "Alloy Turbo (en-US, Multilingual)",
    );
  });

  it("gives every Speech catalog voice a readable name", () => {
    for (const voice of voiceProviderCatalog.providers[1].capabilities.voices.options) {
      expect(formatVoiceName(voice)).not.toBe(voice);
      expect(formatVoiceName(voice)).toMatch(/^[A-Z][A-Za-z0-9 ]+ \(en-US(, [^)]+)?\)$/);
    }
  });

  it("leaves other voice names untouched", () => {
    for (const voice of ["alloy", "marin", "custom-endpoint:voice", "en-US", ""]) {
      expect(formatVoiceName(voice)).toBe(voice);
    }
  });

  it("reads MAI voices and marks the catalog's preview voices", () => {
    expect(formatVoiceName("en-US-Harper:MAI-Voice-2.1-Flash")).toBe(
      "Harper (en-US, MAI Voice 2.1 Flash, preview)",
    );
    expect(formatVoiceName("en-US-Sage:MAI-Voice-2.1")).toBe("Sage (en-US, MAI Voice 2.1, preview)");
    const speech = voiceProviderCatalog.providers[1].capabilities.voices;
    for (const voice of speech.options) {
      expect(isPreviewVoice(voice)).toBe(
        (speech.previewOptions as readonly string[]).includes(voice),
      );
      expect(formatVoiceName(voice).endsWith(", preview)")).toBe(isPreviewVoice(voice));
    }
    expect(speech.options.filter(isPreviewVoice)).toHaveLength(14);
  });

  it("takes preview status from the catalog, not from the shape of an id", () => {
    // A MAI-shaped id the catalog does not list is formatted, never called preview.
    expect(isPreviewVoice("en-GB-Emily:MAI-Voice-2.1-Flash")).toBe(false);
    expect(formatVoiceName("en-GB-Emily:MAI-Voice-2.1-Flash")).toBe(
      "Emily (en-GB, MAI Voice 2.1 Flash)",
    );
    expect(formatVoiceName("en-US-Ava:DragonHDLatestNeural")).toBe("Ava (en-US, Dragon HD)");
    expect(isPreviewVoice("alloy")).toBe(false);
  });
});
