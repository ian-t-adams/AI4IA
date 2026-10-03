import { describe, expect, it } from "vitest";
import {
  AVATAR_LISTENING_MODES,
  avatarPauseTailBoundMs,
  DEFAULT_VOICE,
  DEFAULT_VOICE_SETTINGS,
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_SPEECH_ECHO_CANCELLATION,
  DEFAULT_SPEECH_NOISE_SUPPRESSION,
  DEFAULT_PLAYBACK_PROFILE,
  effectiveAvatarListening,
  MAX_MICROPHONE_BUFFERED_BYTES,
  microphoneBufferLimitBytes,
  microphoneConstraints,
  PLAYBACK_BUFFER_MS,
  avatarErrorMessage,
  buildInitialVoiceFrames,
  buildVoiceLiveWebSocketUrl,
  isVadType,
  realtimeModels,
  resolveAuthorizedVoiceProviders,
  resolveSpeechTranscriptionOption,
  responseDoneNotice,
  sessionUpdate,
  speechEchoReference,
  speechModelSupportsSampling,
  speechSessionUpdate,
  speechTranscriptionOptions,
  transcriptionFailureNotice,
  transcriptionOptionLabel,
  type SpeechManagedModel,
  type VoiceProvider,
  type VoiceSessionSettings,
} from "./voiceLive";
import { voiceProviderCatalog } from "./data/voice_provider_catalog";
import protocolFixtures from "../../test-fixtures/realtime_protocol.json";

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

  it("asks the browser to cancel all playback echo only for a Speech avatar session", () => {
    expect(microphoneConstraints("speech_voice_live", true)).toEqual({
      channelCount: 1,
      echoCancellation: "all",
      noiseSuppression: false,
      autoGainControl: false,
    });
    // Azure OpenAI keeps its browser DSP whatever the flag says.
    expect(microphoneConstraints("azure_openai", true)).toEqual({
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
    });
  });

  it("bounds the microphone pause after the avatar's speech by its lag, live edge and echo tail", () => {
    expect(avatarPauseTailBoundMs(PLAYBACK_BUFFER_MS.balanced / 1000)).toBe(2520);
    expect(avatarPauseTailBoundMs(PLAYBACK_BUFFER_MS.smooth / 1000)).toBe(2580);
  });

  it("turns browser echo cancellation off for the client echo reference, as Microsoft's sample does", () => {
    // Azure cancels against what the page plays, so it must hear the raw microphone.
    expect(microphoneConstraints("speech_voice_live", true, "reference")).toEqual({
      channelCount: 1,
      echoCancellation: false,
      noiseSuppression: false,
      autoGainControl: false,
    });
    // Controls: the other listening modes still ask the browser to cancel playback,
    // and Azure OpenAI keeps its browser DSP.
    for (const mode of ["pause", "listen"] as const) {
      expect(microphoneConstraints("speech_voice_live", true, mode).echoCancellation).toBe("all");
    }
    expect(microphoneConstraints("speech_voice_live", false, "reference").echoCancellation).toBe(false);
    expect(microphoneConstraints("azure_openai", true, "reference").echoCancellation).toBe(true);
  });

  it("bounds the stereo microphone queue to the same two seconds of audio", () => {
    expect(microphoneBufferLimitBytes(1)).toBe(MAX_MICROPHONE_BUFFERED_BYTES);
    expect(microphoneBufferLimitBytes(2)).toBe(2 * MAX_MICROPHONE_BUFFERED_BYTES);
    expect(microphoneBufferLimitBytes(0)).toBe(MAX_MICROPHONE_BUFFERED_BYTES);
  });

  it("keeps every playback profile within a conversational latency budget", () => {
    expect(DEFAULT_PLAYBACK_PROFILE).toBe("balanced");
    expect(PLAYBACK_BUFFER_MS).toEqual({
      fast: 80,
      balanced: 120,
      smooth: 180,
    });
    expect(Math.max(...Object.values(PLAYBACK_BUFFER_MS))).toBeLessThanOrEqual(200);
  });
});

describe("realtimeModels", () => {
  it("offers GA-only models only with the server-selected GA protocol", () => {
    const models = [
      { id: "gpt-realtime-2", category: "realtime" },
      { id: "gpt-realtime-1.5", category: "realtime", requiredRealtimeProtocol: "ga" as const },
      { id: "gpt-realtime-2.1", category: "realtime", requiredRealtimeProtocol: "ga" as const },
      { id: "gpt-realtime-2.1-mini", category: "realtime", requiredRealtimeProtocol: "ga" as const },
      { id: "disabled", category: "realtime", runtimeEnabled: false },
    ];
    expect(realtimeModels(models).map((m) => m.id)).toEqual(["gpt-realtime-2"]);
    expect(realtimeModels(models, "preview").map((m) => m.id)).toEqual(["gpt-realtime-2"]);
    const gaModels = ["gpt-realtime-2", "gpt-realtime-1.5", "gpt-realtime-2.1", "gpt-realtime-2.1-mini"];
    expect(realtimeModels(models, "ga").map((m) => m.id)).toEqual(gaModels);
    models[4].runtimeEnabled = true;
    expect(realtimeModels(models, "ga").map((m) => m.id)).toEqual([...gaModels, "disabled"]);
    for (const model of models.slice(1, 4)) {
      model.runtimeEnabled = false;
      expect(realtimeModels(models, "ga").map((m) => m.id)).not.toContain(model.id);
      model.runtimeEnabled = true;
      expect(realtimeModels(models, "ga").map((m) => m.id)).toContain(model.id);
    }
  });

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
  it("matches the application frames exercised by the API's GA adapter", () => {
    const frames = buildInitialVoiceFrames({
      providerId: "azure_openai",
      voice: "alloy",
      history: [
        { role: "user", text: "Hello" },
        { role: "assistant", text: "Hello" },
      ],
    }).map((frame) => JSON.parse(frame));
    expect(frames).toEqual(
      ["browser-default-session", "user-seed", "assistant-seed"].map((name) =>
        protocolFixtures.client.find((fixture) => fixture.name === name)?.application,
      ),
    );
    expect(frames).toHaveLength(3);
  });

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
          type: DEFAULT_SPEECH_NOISE_SUPPRESSION,
        },
        input_audio_echo_cancellation: {
          type: DEFAULT_SPEECH_ECHO_CANCELLATION,
        },
      },
    });
  });

  it("never sends the browser-only avatar listening mode", () => {
    expect(DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.avatarListening).toBe("pause");
    const reference = speechSessionUpdate("gpt-realtime", DEFAULT_SPEECH_VOICE_LIVE_SETTINGS);
    for (const avatarListening of AVATAR_LISTENING_MODES) {
      const frame = speechSessionUpdate("gpt-realtime", {
        ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
        avatarListening,
      });
      expect(frame).toBe(reference);
      expect(frame).not.toContain("avatarListening");
    }
  });

  it("keeps the default frame byte for byte and adds the client reference only when asked", () => {
    // Captured from the Speech frame before the client echo reference existed.
    const before =
      '{"type":"session.update","session":{"voice":{"type":"azure-standard","name":"en-US-Ava:DragonHDLatestNeural","locale":"en-US"},"input_audio_transcription":{"model":"gpt-4o-transcribe","language":"en-US"},"turn_detection":{"type":"azure_semantic_vad","interrupt_response":true,"auto_truncate":false},"input_audio_noise_reduction":{"type":"azure_deep_noise_suppression"},"input_audio_echo_cancellation":{"type":"server_echo_cancellation"}}}';
    expect(speechSessionUpdate("gpt-realtime")).toBe(before);
    expect(speechSessionUpdate("gpt-realtime", DEFAULT_SPEECH_VOICE_LIVE_SETTINGS, null)).toBe(before);
    const reference = speechEchoReference();
    expect(reference).not.toBeNull();
    const opted = JSON.parse(
      speechSessionUpdate("gpt-realtime", DEFAULT_SPEECH_VOICE_LIVE_SETTINGS, reference),
    );
    expect(opted.session.input_audio_echo_cancellation).toEqual({
      type: "server_echo_cancellation",
      reference_source: "client",
      channels: 2,
    });
    // Nothing else differs from the default frame.
    opted.session.input_audio_echo_cancellation = { type: "server_echo_cancellation" };
    expect(JSON.stringify(opted)).toBe(before);
    expect(
      buildInitialVoiceFrames({
        providerId: "speech_voice_live",
        model: "gpt-realtime",
        voice: "ignored",
        echoReference: reference,
      })[0],
    ).toContain('"reference_source":"client","channels":2');
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
      DEFAULT_SPEECH_NOISE_SUPPRESSION,
    );
    expect(parsed.session.input_audio_echo_cancellation.type).toBe(
      DEFAULT_SPEECH_ECHO_CANCELLATION,
    );
  });

  it.each(voiceProviderCatalog.providers[1].managedModels)(
    "sends a temperature for $id only if the model has sampling",
    (model) => {
      const session = JSON.parse(
        speechSessionUpdate(model.id, { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS, temperature: 0.5 }),
      ).session;
      expect(speechModelSupportsSampling(model)).toBe(model.samplingSupported);
      if (model.samplingSupported) {
        expect(session.temperature).toBe(0.5);
      } else {
        expect(session).not.toHaveProperty("temperature");
      }
      // The browser never chooses a reasoning effort; only the relay sets one.
      expect(session).not.toHaveProperty("reasoning_effort");
    },
  );

  it("marks exactly the GPT-5.x Speech models as without sampling", () => {
    const models = voiceProviderCatalog.providers[1].managedModels;
    expect(
      models.filter((model) => !speechModelSupportsSampling(model)).map((model) => model.id),
    ).toEqual(["gpt-5-mini", "gpt-5.1", "gpt-5.2", "gpt-5.4", "gpt-5.6-terra", "gpt-5.6-luna"]);
    // A provider served by an older API has no flag, which keeps the control.
    const terra = models.find((model) => model.id === "gpt-5.6-terra");
    expect(terra && speechModelSupportsSampling(terra)).toBe(false);
    const legacy = Object.fromEntries(
      Object.entries(terra ?? {}).filter(([key]) => key !== "samplingSupported"),
    ) as unknown as SpeechManagedModel;
    expect(speechModelSupportsSampling(legacy)).toBe(true);
    expect(speechModelSupportsSampling(undefined)).toBe(true);
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

  it.each(voiceProviderCatalog.providers[1].managedModels)(
    "sends the selected MAI transcription and voice for $id",
    (model) => {
      const session = JSON.parse(
        speechSessionUpdate(model.id, {
          ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
          voice: "en-US-Harper:MAI-Voice-2.1-Flash",
          transcriptionModel: "mai-transcribe-2",
        }),
      ).session;
      expect(session.input_audio_transcription).toEqual({
        model: "mai-transcribe-2",
        language: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.locale,
      });
      expect(session.voice).toEqual({
        type: "azure-standard",
        name: "en-US-Harper:MAI-Voice-2.1-Flash",
        locale: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.locale,
      });
    },
  );

  it.each(["mai-transcribe", "MAI-Transcribe-2", "whisper-1", "azure-speech", "gpt-4o-transcribe"])(
    "keeps the model default instead of the unoffered transcription %s",
    (requested) => {
      for (const model of voiceProviderCatalog.providers[1].managedModels) {
        const transcription = JSON.parse(
          speechSessionUpdate(model.id, {
            ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
            transcriptionModel: requested,
          }),
        ).session.input_audio_transcription.model;
        expect(transcription).toBe(model.inputTranscription.model);
      }
    },
  );
});

describe("Speech transcription options", () => {
  const speech = voiceProviderCatalog.providers[1];
  const native = speech.managedModels.find((model) => model.id === "gpt-realtime");
  const chain = speech.managedModels.find((model) => model.id === "gpt-4.1");

  it("offers the catalog option to every profile it lists, with a preview label", () => {
    for (const model of speech.managedModels) {
      expect(speechTranscriptionOptions(model).map((option) => option.model)).toEqual([
        "mai-transcribe-2",
      ]);
    }
    const option = resolveSpeechTranscriptionOption(native, "mai-transcribe-2");
    expect(option && transcriptionOptionLabel(option)).toBe("MAI Transcribe 2 (preview)");
    expect(resolveSpeechTranscriptionOption(native, null)).toBeUndefined();
    expect(resolveSpeechTranscriptionOption(native, "mai-transcribe")).toBeUndefined();
  });

  it("limits an option to the profiles it lists", () => {
    const chainOnly = {
      ...speech,
      capabilities: {
        ...speech.capabilities,
        inputTranscription: {
          options: [
            { ...speech.capabilities.inputTranscription.options[0], profiles: ["azure_speech_chain"] },
          ],
        },
      },
    } as unknown as typeof speech;
    expect(speechTranscriptionOptions(native, chainOnly)).toEqual([]);
    expect(resolveSpeechTranscriptionOption(native, "mai-transcribe-2", chainOnly)).toBeUndefined();
    expect(resolveSpeechTranscriptionOption(chain, "mai-transcribe-2", chainOnly)?.model).toBe(
      "mai-transcribe-2",
    );
    const olderApi = {
      ...speech,
      capabilities: { ...speech.capabilities, inputTranscription: undefined },
    } as unknown as typeof speech;
    expect(speechTranscriptionOptions(chain, olderApi)).toEqual([]);
  });
});

describe("transcriptionFailureNotice", () => {
  const error = {
    type: "server_error",
    code: "transcription_failed",
    message: "Transcription unavailable token=supersecret",
  };

  it("names the chosen preview option and how to go back to the default", () => {
    expect(
      transcriptionFailureNotice(error, { displayName: "MAI Transcribe 2", preview: true }),
    ).toBe(
      "MAI Transcribe 2 (preview) couldn't transcribe your last turn: Transcription unavailable " +
        "token=[REDACTED] (type: server_error; code: transcription_failed). If this continues, " +
        "choose Model default transcription in Voice settings.",
    );
  });

  it("explains a default model's failure without suggesting a switch", () => {
    expect(transcriptionFailureNotice(error)).toBe(
      "Your last turn couldn't be transcribed: Transcription unavailable token=[REDACTED] " +
        "(type: server_error; code: transcription_failed).",
    );
    expect(transcriptionFailureNotice(undefined)).toBe(
      "Your last turn couldn't be transcribed: Live voice reported an error.",
    );
  });

  it("stays within the safe error bound without cutting the guidance", () => {
    const long = transcriptionFailureNotice(
      { message: "x".repeat(2_000), code: "c".repeat(200) },
      { displayName: "MAI Transcribe 2", preview: true },
    );
    expect(long.length).toBeLessThanOrEqual(512);
    expect(long.endsWith("choose Model default transcription in Voice settings.")).toBe(true);
  });
});

describe("responseDoneNotice", () => {
  const failed = (error: unknown) => ({
    id: "resp_1",
    status: "failed",
    status_details: { type: "failed", error },
  });
  const ADVICE = " Try another speech model or voice in Setup > Voice.";

  it("explains a failed reply with Azure's type, code, param and message", () => {
    expect(
      responseDoneNotice(
        failed({
          type: "invalid_request_error",
          code: "unsupported_value",
          param: "session.temperature",
          message: "Unsupported value api_key=supersecret",
        }),
      ),
    ).toBe(
      "Azure couldn't complete the reply: Unsupported value api_key=[REDACTED] " +
        "(type: invalid_request_error; code: unsupported_value; param: session.temperature)." +
        ADVICE,
    );
  });

  it("explains a failed reply without an error, or without a message", () => {
    expect(responseDoneNotice({ status: "failed" })).toBe(
      `Azure couldn't complete the reply.${ADVICE}`,
    );
    expect(responseDoneNotice(failed({ code: "server_error" }))).toBe(
      `Azure couldn't complete the reply (code: server_error).${ADVICE}`,
    );
  });

  it("keeps the message plain, bounded text without cutting the codes or guidance", () => {
    const notice = responseDoneNotice(
      failed({
        type: "t".repeat(200),
        code: "c".repeat(200),
        message: `<img src=x onerror=alert(1)>\u0000${"x".repeat(2_000)}`,
      }),
    );
    expect(notice).not.toBeNull();
    expect(notice!.length).toBeLessThanOrEqual(512);
    expect(notice!.endsWith(`(type: ${"t".repeat(96)}; code: ${"c".repeat(96)}).${ADVICE}`)).toBe(
      true,
    );
    // Markup stays literal text (the notice is rendered as text), and control
    // characters are gone.
    expect(notice).toContain("<img src=x onerror=alert(1)>");
    expect(notice).not.toContain("\u0000");
  });

  it("explains a reply cut short by Azure but not one the user ended", () => {
    const incomplete = (reason: unknown) => ({
      status: "incomplete",
      status_details: { type: "incomplete", reason },
    });
    expect(responseDoneNotice(incomplete("content_filter"))).toBe(
      "Azure's content filter stopped the reply.",
    );
    expect(responseDoneNotice(incomplete("max_output_tokens"))).toBe(
      "The reply reached its length limit and was cut short.",
    );
    expect(responseDoneNotice(incomplete("new_reason"))).toBe(
      "Azure ended the reply early (reason: new_reason).",
    );
    expect(responseDoneNotice(incomplete(undefined))).toBe("Azure ended the reply early.");
    expect(responseDoneNotice(incomplete("turn_detected"))).toBeNull();
    expect(responseDoneNotice(incomplete("client_cancelled"))).toBeNull();
  });

  it.each([
    { status: "completed" },
    { status: "cancelled", status_details: { type: "cancelled", reason: "turn_detected" } },
    { status: "cancelled", status_details: { type: "cancelled", reason: "client_cancelled" } },
    { status: "in_progress" },
    {},
    null,
    "failed",
  ])("needs no notice for %j", (response) => {
    expect(responseDoneNotice(response)).toBeNull();
  });
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

  it("names a photo avatar only for Speech and only as a well-formed record id", () => {
    const base = "wss://api.example.test/api/voice/live";
    const id = "0123456789abcdef0123456789abcdef";
    expect(buildVoiceLiveWebSocketUrl(base, { providerId: "speech_voice_live", avatar: id })).toBe(
      `${base}?provider=speech_voice_live&avatar=${id}`,
    );
    for (const input of [
      { providerId: "azure_openai" as const, avatar: id },
      { providerId: "speech_voice_live" as const, avatar: "ai4ia-0123456789abcdef0123" },
      { providerId: "speech_voice_live" as const, avatar: "../other" },
      { providerId: "speech_voice_live" as const, avatar: null },
    ]) {
      expect(buildVoiceLiveWebSocketUrl(base, input)).not.toContain("avatar=");
    }
  });

  it("asks the relay for the client echo reference only with a Speech photo avatar", () => {
    const base = "wss://api.example.test/api/voice/live";
    const id = "0123456789abcdef0123456789abcdef";
    expect(
      buildVoiceLiveWebSocketUrl(base, {
        providerId: "speech_voice_live", avatar: id, echoReference: true,
      }),
    ).toBe(`${base}?provider=speech_voice_live&avatar=${id}&echoRef=client`);
    for (const input of [
      { providerId: "speech_voice_live" as const, avatar: id, echoReference: false },
      { providerId: "speech_voice_live" as const, avatar: id },
      { providerId: "speech_voice_live" as const, avatar: null, echoReference: true },
      { providerId: "speech_voice_live" as const, avatar: "../other", echoReference: true },
      { providerId: "azure_openai" as const, avatar: id, echoReference: true },
    ]) {
      expect(buildVoiceLiveWebSocketUrl(base, input)).not.toContain("echoRef");
    }
  });
});

describe("speechEchoReference", () => {
  const speech = voiceProviderCatalog.providers[1];
  const withEcho = (echoCancellation: object) =>
    ({
      ...speech,
      capabilities: { ...speech.capabilities, echoCancellation },
    }) as unknown as VoiceProvider;
  const without = withEcho({
    default: speech.capabilities.echoCancellation.default,
    options: speech.capabilities.echoCancellation.options,
  });

  it("reads the catalog's opt-in Live-Reference AEC contract, for Speech only", () => {
    expect(speechEchoReference()).toEqual({
      preview: true,
      apiVersion: "2026-07-15",
      features: "client_ec_reference:true",
      channels: 2,
    });
    expect(speechEchoReference(speech)).toEqual(speechEchoReference());
    expect(speechEchoReference(voiceProviderCatalog.providers[0])).toBeNull();
    expect(speechEchoReference(null)).toBeNull();
    expect(speechEchoReference(without)).toBeNull();
    // The capture worklet interleaves exactly two channels.
    expect(
      speechEchoReference(
        withEcho({
          ...speech.capabilities.echoCancellation,
          clientReference: { ...speech.capabilities.echoCancellation.clientReference, channels: 4 },
        }),
      ),
    ).toBeNull();
  });

  it("falls back to the default listening mode where the reference isn't offered", () => {
    expect(effectiveAvatarListening("reference")).toBe("reference");
    expect(effectiveAvatarListening("reference", without)).toBe("pause");
    expect(effectiveAvatarListening("listen", without)).toBe("listen");
    expect(effectiveAvatarListening("pause", speech)).toBe("pause");
  });
});

describe("avatarErrorMessage", () => {
  it("explains bounded relay avatar errors and ignores every other error", () => {
    expect(
      avatarErrorMessage({
        type: "avatar_error", code: "avatar_unavailable", reason: "needs_reverification",
        retry_after_seconds: 300,
      }),
    ).toBe("The avatar service couldn't verify this avatar. Try again in 5 minutes.");
    expect(avatarErrorMessage({ type: "avatar_error", code: "avatar_unavailable", reason: "not_found" }))
      .toMatch(/no longer exists/);
    expect(avatarErrorMessage({ type: "avatar_error", code: "cost_unknown_under_cap" }))
      .toMatch(/spending cap/);
    expect(avatarErrorMessage({ type: "avatar_error", code: "avatar_stream_refused" }))
      .toBe("The avatar video stream failed, so the session ended.");
    expect(avatarErrorMessage({ type: "avatar_error", code: "avatar_unavailable", reason: "policy_denied" }))
      .toBe("Live avatars aren't permitted for your account.");
    expect(avatarErrorMessage({ type: "invalid_request_error", code: "avatar_unavailable" })).toBeNull();
    expect(avatarErrorMessage(null)).toBeNull();
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
