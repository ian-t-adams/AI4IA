import { describe, expect, it } from "vitest";
import {
  DEFAULT_VOICE,
  DEFAULT_VOICE_SETTINGS,
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  microphoneConstraints,
  PLAYBACK_REBUFFER_SECONDS,
  buildInitialVoiceFrames,
  buildVoiceLiveWebSocketUrl,
  isVadType,
  realtimeModels,
  resolveAuthorizedVoiceProviders,
  sessionUpdate,
  speechSessionUpdate,
  type VoiceSessionSettings,
} from "./voiceLive";
import { voiceProviderCatalog } from "./data/voice_provider_catalog";

// The exact session.update the relay has always received. Locked byte-for-byte so a
// regression in the default payload (key order, extra fields) fails loudly.
const DEFAULT_SESSION_UPDATE =
  '{"type":"session.update","session":{"voice":"alloy","input_audio_format":"pcm16","output_audio_format":"pcm16","turn_detection":{"type":"server_vad"},"input_audio_transcription":{"model":"whisper-1"}}}';

describe("voice audio transport", () => {
  it("uses browser DSP only when the provider does not already process audio", () => {
    expect(microphoneConstraints("azure_openai")).toEqual({
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    });
    expect(microphoneConstraints("speech_voice_live")).toEqual({
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    });
  });

  it("keeps the playback rebuffer small enough for conversational latency", () => {
    expect(PLAYBACK_REBUFFER_SECONDS).toBeGreaterThanOrEqual(0.08);
    expect(PLAYBACK_REBUFFER_SECONDS).toBeLessThanOrEqual(0.15);
  });
});

describe("realtimeModels", () => {
  it("keeps only realtime-category models", () => {
    const models = [
      { id: "gpt-realtime", category: "realtime" },
      { id: "gpt-5", category: "chat" },
      { id: "gpt-realtime-mini", category: "realtime" },
      { id: "whisper", category: "transcription" },
    ];
    expect(realtimeModels(models).map((m) => m.id)).toEqual([
      "gpt-realtime",
      "gpt-realtime-mini",
    ]);
  });

  describe("resolveAuthorizedVoiceProviders", () => {
    it("stays fail-closed while server provider config is loading or unavailable", () => {
      expect(resolveAuthorizedVoiceProviders(null)).toEqual({
        defaultProviderId: null,
        providers: [],
      });
    });

    it("uses a Speech-only server allowlist and default without adding Azure OpenAI", () => {
      const speech = voiceProviderCatalog.providers[1];
      expect(
        resolveAuthorizedVoiceProviders({
          defaultProviderId: "speech_voice_live",
          enabledProviderIds: ["speech_voice_live"],
          providers: [...voiceProviderCatalog.providers],
        }),
      ).toEqual({
        defaultProviderId: "speech_voice_live",
        providers: [speech],
      });
    });

    it("rejects a server default that is not in the enabled provider set", () => {
      expect(
        resolveAuthorizedVoiceProviders({
          defaultProviderId: "azure_openai",
          enabledProviderIds: ["speech_voice_live"],
          providers: [...voiceProviderCatalog.providers],
        }).defaultProviderId,
      ).toBeNull();
    });
  });

  it("returns an empty list when nothing is realtime", () => {
    expect(realtimeModels([{ id: "gpt-5", category: "chat" }])).toEqual([]);
  });
});

describe("sessionUpdate defaults (byte-for-byte unchanged)", () => {
  it("matches the original payload with no settings argument", () => {
    expect(sessionUpdate("alloy")).toBe(DEFAULT_SESSION_UPDATE);
  });

  it("matches the original payload when given the default settings", () => {
    expect(sessionUpdate("alloy", DEFAULT_VOICE_SETTINGS)).toBe(DEFAULT_SESSION_UPDATE);
  });

  it("falls back to the default voice for an unknown voice", () => {
    const parsed = JSON.parse(sessionUpdate("not-a-voice"));
    expect(parsed.session.voice).toBe(DEFAULT_VOICE);
  });
});

describe("sessionUpdate settings round-trip", () => {
  it("adds temperature only when set", () => {
    const parsed = JSON.parse(
      sessionUpdate("alloy", { ...DEFAULT_VOICE_SETTINGS, temperature: 0.6 }),
    );
    expect(parsed.session.temperature).toBe(0.6);
  });

  it("omits temperature when null", () => {
    const parsed = JSON.parse(sessionUpdate("alloy", DEFAULT_VOICE_SETTINGS));
    expect("temperature" in parsed.session).toBe(false);
  });

  it("threads server_vad threshold and silence", () => {
    const settings: VoiceSessionSettings = {
      ...DEFAULT_VOICE_SETTINGS,
      vadType: "server_vad",
      vadThreshold: 0.4,
      vadSilenceMs: 250,
    };
    const td = JSON.parse(sessionUpdate("alloy", settings)).session.turn_detection;
    expect(td).toEqual({ type: "server_vad", threshold: 0.4, silence_duration_ms: 250 });
  });

  it("drops threshold/silence knobs for semantic_vad", () => {
    const settings: VoiceSessionSettings = {
      ...DEFAULT_VOICE_SETTINGS,
      vadType: "semantic_vad",
      vadThreshold: 0.4,
      vadSilenceMs: 250,
    };
    const td = JSON.parse(sessionUpdate("alloy", settings)).session.turn_detection;
    expect(td).toEqual({ type: "semantic_vad" });
  });

  it("carries a custom transcription model and language hint", () => {
    const settings: VoiceSessionSettings = {
      ...DEFAULT_VOICE_SETTINGS,
      transcriptionModel: "gpt-4o-transcribe",
      language: "en",
    };
    const t = JSON.parse(sessionUpdate("alloy", settings)).session.input_audio_transcription;
    expect(t).toEqual({ model: "gpt-4o-transcribe", language: "en" });
  });

  it("never sends browser-owned instructions", () => {
    const parsed = JSON.parse(sessionUpdate("alloy", DEFAULT_VOICE_SETTINGS));
    expect(parsed.session).not.toHaveProperty("instructions");
  });
});

describe("speechSessionUpdate", () => {
  it("builds the managed speech payload with only catalog-safe settings", () => {
    const parsed = JSON.parse(
      speechSessionUpdate("gpt-realtime", DEFAULT_SPEECH_VOICE_LIVE_SETTINGS),
    );
    expect(parsed).toEqual({
      type: "session.update",
      session: {
        voice: {
          type: "azure-standard",
          name: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.voice,
          locale: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.locale,
        },
        input_audio_transcription: {
          model: "gpt-4o-transcribe",
          language: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.locale,
        },
        turn_detection: {
          type: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.turnDetection,
          interrupt_response: true,
          auto_truncate: false,
        },
        input_audio_noise_reduction: {
          type: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.noiseSuppression,
        },
        input_audio_echo_cancellation: {
          type: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.echoCancellation,
        },
      },
    });
  });

  it("reconstructs stale settings from catalog defaults and clamps temperature", () => {
    const parsed = JSON.parse(
      speechSessionUpdate("not-a-managed-model", {
          ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
          temperature: 99,
          voice: "custom-voice",
          locale: "xx-XX",
          transcription: "custom-transcriber",
          turnDetection: "custom-vad" as never,
          noiseSuppression: "custom-noise" as never,
          echoCancellation: "custom-echo" as never,
        } as typeof DEFAULT_SPEECH_VOICE_LIVE_SETTINGS),
    );

    expect(parsed.session.temperature).toBe(2);
    expect(parsed.session.voice.name).toBe(DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.voice);
    expect(parsed.session.voice.locale).toBe(DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.locale);
    expect(parsed.session.input_audio_transcription.model).toBe(
      "gpt-4o-transcribe",
    );
    expect(parsed.session.turn_detection.type).toBe(
      DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.turnDetection,
    );
    expect(parsed.session.input_audio_noise_reduction.type).toBe(
      DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.noiseSuppression,
    );
    expect(parsed.session.input_audio_echo_cancellation.type).toBe(
      DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.echoCancellation,
    );
  });

  it.each(voiceProviderCatalog.providers[1].managedModels)(
    "uses the catalog transcription for $id ($profile)",
    (model) => {
      const transcription = JSON.parse(
        speechSessionUpdate(model.id, DEFAULT_SPEECH_VOICE_LIVE_SETTINGS),
      ).session.input_audio_transcription.model;
      expect(transcription).toBe(model.inputTranscription.model);
      expect(transcription).toBe(
        model.profile === "native_audio" ? "gpt-4o-transcribe" : "azure-speech",
      );
    },
  );
});

describe("buildVoiceLiveWebSocketUrl", () => {
  it("keeps model and region only for Azure OpenAI", () => {
    expect(
      buildVoiceLiveWebSocketUrl("wss://api.example.test/api/voice/live", {
        providerId: "azure_openai",
        model: "gpt-realtime",
        region: "eastus2",
        agent: "analyst",
        tools: true,
      }),
    ).toBe("wss://api.example.test/api/voice/live?provider=azure_openai&model=gpt-realtime&region=eastus2&agent=analyst&tools=1");
  });

  it("keeps model but omits region for managed speech", () => {
    expect(
      buildVoiceLiveWebSocketUrl("wss://api.example.test/api/voice/live", {
        providerId: "speech_voice_live",
        model: "gpt-realtime",
        region: "eastus2",
        agent: "analyst",
        tools: true,
      }),
    ).toBe("wss://api.example.test/api/voice/live?provider=speech_voice_live&model=gpt-realtime&agent=analyst&tools=1");
  });
});

describe("buildInitialVoiceFrames", () => {
  it("preserves exact Azure OpenAI session bytes before oldest-to-newest seeds", () => {
    const frames = buildInitialVoiceFrames({
      providerId: "azure_openai",
      model: "gpt-realtime",
      voice: "alloy",
      history: [
        { role: "user", text: "first" },
        { role: "assistant", text: "second" },
      ],
    });
    expect(frames[0]).toBe(DEFAULT_SESSION_UPDATE);
    expect(frames.slice(1).map((frame) => JSON.parse(frame))).toEqual([
      {
        type: "conversation.item.create",
        item: {
          type: "message",
          role: "user",
          content: [{ type: "input_text", text: "first" }],
        },
      },
      {
        type: "conversation.item.create",
        item: {
          type: "message",
          role: "assistant",
          content: [{ type: "text", text: "second" }],
        },
      },
    ]);
  });
});

describe("isVadType", () => {
  it("accepts the two known VAD types and rejects others", () => {
    expect(isVadType("server_vad")).toBe(true);
    expect(isVadType("semantic_vad")).toBe(true);
    expect(isVadType("nope")).toBe(false);
  });
});
