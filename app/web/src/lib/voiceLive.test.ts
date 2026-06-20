import { describe, expect, it } from "vitest";
import {
  DEFAULT_VOICE,
  DEFAULT_VOICE_SETTINGS,
  isVadType,
  realtimeModels,
  sessionUpdate,
  type VoiceSessionSettings,
} from "./voiceLive";

// The exact session.update the relay has always received. Locked byte-for-byte so a
// regression in the default payload (key order, extra fields) fails loudly.
const DEFAULT_SESSION_UPDATE =
  '{"type":"session.update","session":{"instructions":"You are a helpful, concise voice assistant. Keep spoken replies brief and natural.","voice":"alloy","input_audio_format":"pcm16","output_audio_format":"pcm16","turn_detection":{"type":"server_vad"},"input_audio_transcription":{"model":"whisper-1"}}}';

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

  it("threads an instructions override", () => {
    const parsed = JSON.parse(
      sessionUpdate("alloy", { ...DEFAULT_VOICE_SETTINGS, instructions: "Be terse." }),
    );
    expect(parsed.session.instructions).toBe("Be terse.");
  });
});

describe("isVadType", () => {
  it("accepts the two known VAD types and rejects others", () => {
    expect(isVadType("server_vad")).toBe(true);
    expect(isVadType("semantic_vad")).toBe(true);
    expect(isVadType("nope")).toBe(false);
  });
});
