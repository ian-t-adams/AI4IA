import { describe, expect, it } from "vitest";
import {
  DEFAULT_VOICE_PREFERENCES,
  hasStoredVoicePreferences,
  loadVoicePreferences,
  normalizeSpeechVoiceLiveSettings,
  normalizeVoicePreferences,
  normalizeVoiceSessionSettings,
  resolveEffectiveAgent,
  resolveEffectiveModel,
  resolveEffectiveVoiceProvider,
  sanitizeVoicePreferencesForProviders,
  saveVoicePreferences,
  VOICE_PREFERENCES_STORAGE_NAME,
  V2_VOICE_PREFERENCES_STORAGE_NAME,
  type PreferencesStorage,
  type VoicePreferences,
} from "./voicePreferences";
import {
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_VOICE_SETTINGS,
  type VoiceProvider,
} from "./voiceLive";
import { voiceProviderCatalog } from "./data/voice_provider_catalog";

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
      speechAvatarId: "0123456789abcdef0123456789abcdef",
    };
    expect(normalizeVoicePreferences(valid)).toEqual(valid);
  });

  it("keeps only a well-formed photo avatar record id", () => {
    for (const bad of ["ai4ia-0123456789abcdef0123", "0123", "../x", 42, {}, ""]) {
      expect(normalizeVoicePreferences({ speechAvatarId: bad }).speechAvatarId).toBeNull();
    }
    expect(
      normalizeVoicePreferences({ speechAvatarId: "fedcba9876543210fedcba9876543210" })
        .speechAvatarId,
    ).toBe("fedcba9876543210fedcba9876543210");
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
      speechAvatarId: null,
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

describe("Speech transcription and preview voice preferences", () => {
  const providers = [...voiceProviderCatalog.providers] as VoiceProvider[];
  const sanitize = (speech: Record<string, unknown>, available = providers) =>
    sanitizeVoicePreferencesForProviders(
      { ...DEFAULT_VOICE_PREFERENCES, provider: "speech_voice_live", speech: speech as never },
      available,
      new Set(["gpt-realtime"]),
      "gpt-realtime",
      false,
      "azure_openai",
      true,
    ).speech;

  it("defaults to the managed model's own transcription", () => {
    expect(DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.transcriptionModel).toBeNull();
    expect(normalizeSpeechVoiceLiveSettings({}).transcriptionModel).toBeNull();
    for (const bad of ["", "   ", 7, null, {}, ["mai-transcribe-2"]]) {
      expect(normalizeSpeechVoiceLiveSettings({ transcriptionModel: bad }).transcriptionModel)
        .toBeNull();
    }
    expect(
      normalizeSpeechVoiceLiveSettings({ transcriptionModel: " mai-transcribe-2 " })
        .transcriptionModel,
    ).toBe("mai-transcribe-2");
  });

  it("keeps only a transcription option the server's catalog offers", () => {
    const speech = { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS };
    expect(sanitize({ ...speech, transcriptionModel: "mai-transcribe-2" }).transcriptionModel)
      .toBe("mai-transcribe-2");
    for (const unoffered of ["mai-transcribe", "whisper-1", "azure-speech", "gpt-4o-transcribe"]) {
      expect(sanitize({ ...speech, transcriptionModel: unoffered }).transcriptionModel).toBeNull();
    }
    // An older API that predates the capability offers no alternatives.
    const [openai, speechProvider] = providers;
    const olderSpeech = {
      ...speechProvider,
      capabilities: { ...speechProvider.capabilities, inputTranscription: undefined },
    } as unknown as VoiceProvider;
    expect(
      sanitize({ ...speech, transcriptionModel: "mai-transcribe-2" }, [openai, olderSpeech])
        .transcriptionModel,
    ).toBeNull();
    // While the server offers no Speech provider at all, the stored pick is kept
    // (like the voice), so it resumes when Speech returns.
    const withoutSpeech = sanitize(
      { ...speech, voice: "en-US-Harper:MAI-Voice-2.1", transcriptionModel: "mai-transcribe-2" },
      [openai],
    );
    expect(withoutSpeech.transcriptionModel).toBe("mai-transcribe-2");
    expect(withoutSpeech.voice).toBe("en-US-Harper:MAI-Voice-2.1");
  });

  it("keeps a catalog MAI voice and replaces an unreviewed one with the default", () => {
    const speech = { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS };
    expect(sanitize({ ...speech, voice: "en-US-Harper:MAI-Voice-2.1-Flash" }).voice).toBe(
      "en-US-Harper:MAI-Voice-2.1-Flash",
    );
    expect(sanitize({ ...speech, voice: "en-US-Harper:MAI-Voice-2-Flash" }).voice).toBe(
      DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.voice,
    );
  });
});

describe("avatar listening preference", () => {
  it("defaults to pausing the microphone and keeps only a known mode", () => {
    expect(DEFAULT_VOICE_PREFERENCES.speech.avatarListening).toBe("pause");
    expect(normalizeSpeechVoiceLiveSettings({}).avatarListening).toBe("pause");
    for (const bad of ["LISTEN", "mute", " listen", "", 1, null, true, {}, ["listen"]]) {
      expect(normalizeSpeechVoiceLiveSettings({ avatarListening: bad }).avatarListening).toBe("pause");
    }
    expect(normalizeSpeechVoiceLiveSettings({ avatarListening: "listen" }).avatarListening).toBe(
      "listen",
    );
  });

  it("persists the choice, survives provider sanitizing, and defaults for older records", () => {
    const providers = [...voiceProviderCatalog.providers] as VoiceProvider[];
    const prefs: VoicePreferences = {
      ...DEFAULT_VOICE_PREFERENCES,
      provider: "speech_voice_live",
      speech: { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS, avatarListening: "listen" },
    };
    const storage = fakeStorage();
    saveVoicePreferences(prefs, storage);
    expect(loadVoicePreferences(storage).speech.avatarListening).toBe("listen");
    expect(
      sanitizeVoicePreferencesForProviders(
        prefs, providers, new Set(["gpt-realtime"]), "gpt-realtime", false, "azure_openai", true,
      ).speech.avatarListening,
    ).toBe("listen");

    // A record saved before the setting existed.
    const older: Record<string, unknown> = { ...prefs.speech };
    delete older.avatarListening;
    storage.data[VOICE_PREFERENCES_STORAGE_NAME] = JSON.stringify({ ...prefs, speech: older });
    expect(loadVoicePreferences(storage).speech.avatarListening).toBe("pause");
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
  const models = new Set(["gpt-realtime", "gpt-realtime-mini", "gpt-realtime-2"]);

  it("prefers a valid explicit selection over the fallback", () => {
    expect(resolveEffectiveModel("gpt-realtime-mini", models, "gpt-realtime")).toBe(
      "gpt-realtime-mini",
    );
    expect(resolveEffectiveModel("gpt-realtime-2", models, "gpt-realtime")).toBe(
      "gpt-realtime-2",
    );
  });

  it("defaults only an unset pick and retains unavailable explicit models", () => {
    expect(resolveEffectiveModel(null, models, "gpt-realtime")).toBe("gpt-realtime");
    expect(resolveEffectiveModel("retired-model", models, "gpt-realtime")).toBe(
      "retired-model",
    );
    expect(resolveEffectiveModel("gpt-realtime-1.5", models, "gpt-realtime")).toBe(
      "gpt-realtime-1.5",
    );
    expect(resolveEffectiveModel(null, models, "unavailable-default")).toBeNull();
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
