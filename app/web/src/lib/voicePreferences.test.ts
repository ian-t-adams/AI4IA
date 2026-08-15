import { describe, expect, it } from "vitest";
import {
  DEFAULT_VOICE_PREFERENCES,
  hasStoredVoicePreferences,
  loadVoicePreferences,
  normalizeVoicePreferences,
  normalizeVoiceSessionSettings,
  resolveEffectiveAgent,
  resolveEffectiveModel,
  resolveEffectiveVoiceProvider,
  saveVoicePreferences,
  VOICE_PREFERENCES_STORAGE_NAME,
  V2_VOICE_PREFERENCES_STORAGE_NAME,
  type PreferencesStorage,
  type VoicePreferences,
} from "./voicePreferences";
import {
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_VOICE_SETTINGS,
} from "./voiceLive";

function fakeStorage(initial: Record<string, string> = {}): PreferencesStorage & {
  data: Record<string, string>;
} {
  const data = { ...initial };
  return {
    data,
    getItem: (key: string) => (key in data ? data[key] : null),
    setItem: (key: string, value: string) => {
      data[key] = value;
    },
  };
}

describe("normalizeVoicePreferences", () => {
  it("returns defaults for non-object/null input", () => {
    expect(normalizeVoicePreferences(null)).toEqual(DEFAULT_VOICE_PREFERENCES);
    expect(normalizeVoicePreferences(undefined)).toEqual(DEFAULT_VOICE_PREFERENCES);
    expect(normalizeVoicePreferences("nonsense")).toEqual(DEFAULT_VOICE_PREFERENCES);
    expect(normalizeVoicePreferences(42)).toEqual(DEFAULT_VOICE_PREFERENCES);
  });

  it("round-trips a fully valid preferences object", () => {
    const valid: VoicePreferences = {
      provider: "azure_openai",
      explicitAgent: "analyst",
      model: "gpt-realtime",
      speechModel: "gpt-4.1",
      voice: "marin",
      tools: true,
      settings: {
        ...DEFAULT_VOICE_SETTINGS,
        playbackProfile: "smooth",
        temperature: 0.8,
        vadThreshold: 0.5,
        vadSilenceMs: 400,
        language: "en-US",
      },
      speech: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
    };
    expect(normalizeVoicePreferences(valid)).toEqual(valid);
  });

  it("migrates legacy v1 data into the v4 shape and drops instructions", () => {
    const storage = fakeStorage({
      "ai4ia.voiceLive.prefs.v1": JSON.stringify({
        explicitAgent: "analyst",
        model: "gpt-realtime-mini",
        voice: "cedar",
        tools: true,
        settings: {
          temperature: 0.5,
          vadType: "semantic_vad",
          transcriptionModel: "whisper-1",
          language: "en",
        },
      }),
    });
    expect(loadVoicePreferences(storage)).toEqual({
      provider: "azure_openai",
      explicitAgent: "analyst",
      model: "gpt-realtime-mini",
      speechModel: "gpt-realtime",
      voice: "cedar",
      tools: true,
      settings: {
        playbackProfile: "balanced",
        temperature: 0.5,
        vadType: "semantic_vad",
        vadThreshold: null,
        vadSilenceMs: null,
        transcriptionModel: "whisper-1",
        language: "en",
      },
      speech: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
    });
  });

  it("migrates v2 while ignoring legacy Speech transcription", () => {
    const storage = fakeStorage({
      [V2_VOICE_PREFERENCES_STORAGE_NAME]: JSON.stringify({
        provider: "speech_voice_live",
        explicitAgent: "analyst",
        model: "gpt-realtime-mini",
        voice: "cedar",
        speech: {
          ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
          voice: "en-US-AndrewNeural",
          transcription: "user-controlled-transcriber",
        },
      }),
    });

    const migrated = loadVoicePreferences(storage);
    expect(migrated.provider).toBe("speech_voice_live");
    expect(migrated.model).toBe("gpt-realtime-mini");
    expect(migrated.speechModel).toBe("gpt-realtime");
    expect(migrated.speech.voice).toBe("en-US-AndrewNeural");
    expect(migrated.speech).not.toHaveProperty("transcription");
    expect(JSON.parse(storage.data[VOICE_PREFERENCES_STORAGE_NAME])).toEqual(migrated);
  });

  it("drops an unknown voice, non-boolean tools, and blank agent/model to defaults", () => {
    const result = normalizeVoicePreferences({
      explicitAgent: "",
      model: 42,
      voice: "not-a-voice",
      tools: "yes",
    });
    expect(result.explicitAgent).toBeNull();
    expect(result.model).toBeNull();
    expect(result.voice).toBe(DEFAULT_VOICE_PREFERENCES.voice);
    expect(result.tools).toBe(false);
  });

  it("ignores malformed JSON top-level shape without throwing", () => {
    expect(() => normalizeVoicePreferences([1, 2, 3])).not.toThrow();
  });
});

describe("normalizeVoiceSessionSettings", () => {
  it("returns defaults for a non-object", () => {
    expect(normalizeVoiceSessionSettings(null)).toEqual(DEFAULT_VOICE_SETTINGS);
    expect(normalizeVoiceSessionSettings("bad")).toEqual(DEFAULT_VOICE_SETTINGS);
  });

  it("clamps out-of-range numeric fields into safe bounds", () => {
    const result = normalizeVoiceSessionSettings({
      temperature: 99,
      vadThreshold: -5,
      vadSilenceMs: 999_999,
    });
    expect(result.temperature).toBe(2);
    expect(result.vadThreshold).toBe(0);
    expect(result.vadSilenceMs).toBe(60_000);
  });

  it("rejects a non-finite/NaN number in favor of null", () => {
    const result = normalizeVoiceSessionSettings({
      temperature: Number.NaN,
      vadThreshold: Infinity,
    });
    expect(result.temperature).toBeNull();
    expect(result.vadThreshold).toBeNull();
  });

  it("falls back to the default VAD type for an unknown value", () => {
    expect(normalizeVoiceSessionSettings({ vadType: "invalid_vad" }).vadType).toBe(
      DEFAULT_VOICE_SETTINGS.vadType,
    );
    expect(normalizeVoiceSessionSettings({ vadType: "semantic_vad" }).vadType).toBe(
      "semantic_vad",
    );
  });

  it("accepts only bounded playback profiles", () => {
    expect(normalizeVoiceSessionSettings({ playbackProfile: "smooth" }).playbackProfile).toBe(
      "smooth",
    );
    expect(normalizeVoiceSessionSettings({ playbackProfile: 500 }).playbackProfile).toBe(
      DEFAULT_VOICE_SETTINGS.playbackProfile,
    );
  });

  it("accepts a plain or regioned language tag and rejects garbage", () => {
    expect(normalizeVoiceSessionSettings({ language: "en" }).language).toBe("en");
    expect(normalizeVoiceSessionSettings({ language: "en-US" }).language).toBe("en-US");
    expect(normalizeVoiceSessionSettings({ language: "" }).language).toBe("");
    expect(normalizeVoiceSessionSettings({ language: "not a tag!!" }).language).toBe("");
    expect(normalizeVoiceSessionSettings({ language: 7 }).language).toBe("");
  });

  it("drops legacy instructions and falls back to the transcription model", () => {
    const result = normalizeVoiceSessionSettings({
      instructions: "   ",
      transcriptionModel: "",
    });
    expect(result).not.toHaveProperty("instructions");
    expect(result.transcriptionModel).toBe(DEFAULT_VOICE_SETTINGS.transcriptionModel);
  });
});

describe("resolveEffectiveAgent", () => {
  const enabled = new Set(["analyst", "writer"]);

  it("prefers a valid explicit selection over the fallback", () => {
    expect(resolveEffectiveAgent("analyst", enabled, "writer")).toBe("analyst");
  });

  it("falls back when the explicit selection is null", () => {
    expect(resolveEffectiveAgent(null, enabled, "writer")).toBe("writer");
  });

  it("falls back when the explicit selection is stale/disabled", () => {
    expect(resolveEffectiveAgent("retired-agent", enabled, "writer")).toBe("writer");
  });

  it("falls back to null when there is no fallback and the pick is stale", () => {
    expect(resolveEffectiveAgent("retired-agent", enabled, null)).toBeNull();
  });
});

describe("resolveEffectiveModel", () => {
  const models = new Set(["gpt-realtime", "gpt-realtime-mini"]);

  it("prefers a valid explicit selection over the fallback", () => {
    expect(resolveEffectiveModel("gpt-realtime-mini", models, "gpt-realtime")).toBe(
      "gpt-realtime-mini",
    );
  });

  it("falls back when null or stale", () => {
    expect(resolveEffectiveModel(null, models, "gpt-realtime")).toBe("gpt-realtime");
    expect(resolveEffectiveModel("retired-model", models, "gpt-realtime")).toBe(
      "gpt-realtime",
    );
  });
});

describe("resolveEffectiveVoiceProvider", () => {
  const enabled = ["azure_openai", "speech_voice_live"] as const;

  it("honors the server default only when no usable preference is stored", () => {
    expect(
      resolveEffectiveVoiceProvider("azure_openai", enabled, "speech_voice_live", false),
    ).toBe("speech_voice_live");
    expect(
      resolveEffectiveVoiceProvider("azure_openai", enabled, "speech_voice_live", true),
    ).toBe("azure_openai");
  });

  it("selects Speech when it is the only server-authorized provider", () => {
    expect(
      resolveEffectiveVoiceProvider(
        "azure_openai",
        ["speech_voice_live"],
        "speech_voice_live",
        false,
      ),
    ).toBe("speech_voice_live");
  });

  it("falls back safely when a persisted provider or server default is disabled", () => {
    expect(
      resolveEffectiveVoiceProvider(
        "speech_voice_live",
        ["azure_openai"],
        "azure_openai",
        true,
      ),
    ).toBe("azure_openai");
    expect(
      resolveEffectiveVoiceProvider(
        "speech_voice_live",
        ["azure_openai"],
        "speech_voice_live",
        true,
      ),
    ).toBe("azure_openai");
  });
});

describe("loadVoicePreferences / saveVoicePreferences", () => {
  it("reports whether current or legacy preferences exist", () => {
    expect(hasStoredVoicePreferences(fakeStorage())).toBe(false);
    expect(
      hasStoredVoicePreferences(
        fakeStorage({ "ai4ia.voiceLive.prefs.v1": JSON.stringify({ voice: "alloy" }) }),
      ),
    ).toBe(true);
    expect(
      hasStoredVoicePreferences(
        fakeStorage({ [V2_VOICE_PREFERENCES_STORAGE_NAME]: JSON.stringify({ voice: "alloy" }) }),
      ),
    ).toBe(true);
    expect(
      hasStoredVoicePreferences(
        fakeStorage({
          [VOICE_PREFERENCES_STORAGE_NAME]: JSON.stringify(DEFAULT_VOICE_PREFERENCES),
        }),
      ),
    ).toBe(true);
  });

  it("does not treat a stale or malformed v2 provider as a usable preference", () => {
    expect(
      hasStoredVoicePreferences(
        fakeStorage({
          [VOICE_PREFERENCES_STORAGE_NAME]: JSON.stringify({ provider: "retired-provider" }),
        }),
      ),
    ).toBe(false);
    expect(
      hasStoredVoicePreferences(
        fakeStorage({ [VOICE_PREFERENCES_STORAGE_NAME]: "{not json" }),
      ),
    ).toBe(false);
  });

  it("returns defaults when nothing is stored", () => {
    expect(loadVoicePreferences(fakeStorage())).toEqual(DEFAULT_VOICE_PREFERENCES);
  });

  it("returns defaults for malformed JSON instead of throwing", () => {
    const storage = fakeStorage({ [VOICE_PREFERENCES_STORAGE_NAME]: "{not json" });
    expect(() => loadVoicePreferences(storage)).not.toThrow();
    expect(loadVoicePreferences(storage)).toEqual(DEFAULT_VOICE_PREFERENCES);
  });

  it("round-trips a saved preference through storage", () => {
    const storage = fakeStorage();
    const prefs: VoicePreferences = {
      ...DEFAULT_VOICE_PREFERENCES,
      explicitAgent: "analyst",
      voice: "cedar",
      tools: true,
    };
    saveVoicePreferences(prefs, storage);
    expect(loadVoicePreferences(storage)).toEqual(prefs);
  });

  it("normalizes a stale/invalid stored value on load", () => {
    const storage = fakeStorage({
      [VOICE_PREFERENCES_STORAGE_NAME]: JSON.stringify({
        explicitAgent: 123,
        voice: "not-a-voice",
        tools: "true",
        settings: { vadThreshold: 5 },
      }),
    });
    const loaded = loadVoicePreferences(storage);
    expect(loaded.explicitAgent).toBeNull();
    expect(loaded.voice).toBe(DEFAULT_VOICE_PREFERENCES.voice);
    expect(loaded.tools).toBe(false);
    expect(loaded.settings.vadThreshold).toBe(1);
  });

  it("tolerates a storage backend that throws on read", () => {
    const storage: PreferencesStorage = {
      getItem: () => {
        throw new Error("storage disabled");
      },
      setItem: () => {},
    };
    expect(() => loadVoicePreferences(storage)).not.toThrow();
    expect(loadVoicePreferences(storage)).toEqual(DEFAULT_VOICE_PREFERENCES);
  });

  it("tolerates a storage backend that throws on write", () => {
    const storage: PreferencesStorage = {
      getItem: () => null,
      setItem: () => {
        throw new Error("quota exceeded");
      },
    };
    expect(() => saveVoicePreferences(DEFAULT_VOICE_PREFERENCES, storage)).not.toThrow();
  });

  it("falls back to defaults when no storage backend is available", () => {
    expect(loadVoicePreferences(undefined)).toEqual(DEFAULT_VOICE_PREFERENCES);
    expect(() => saveVoicePreferences(DEFAULT_VOICE_PREFERENCES, undefined)).not.toThrow();
  });
});
