"use client";

// Voice Live preferences: the user's persisted picks for the inline settings
// disclosure (agent, realtime model, voice, governed-tools opt-in, and the
// advanced session settings). Persisted to localStorage under a versioned key
// so a future shape change can migrate/ignore old data instead of crashing.
//
// This module is intentionally split from voiceLive.ts: normalization here is
// pure and self-contained (no catalog/agent list required), which keeps it
// trivially unit-testable. Catalog-dependent validation (does this agent/model
// still exist?) happens where the live catalog is available (ChatApp), via the
// resolveEffective* helpers below.
import {
  DEFAULT_VOICE,
  DEFAULT_VOICE_SETTINGS,
  DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
  DEFAULT_SPEECH_MODEL_ID,
  DEFAULT_VOICE_PROVIDER,
  isRealtimeVoice,
  isSpeechVoiceProvider,
  isVadType,
  type RealtimeVoice,
  type SpeechVoiceLiveSettings,
  type VoiceSessionSettings,
  type VoiceProvider,
} from "./voiceLive";

export const VOICE_PREFERENCES_STORAGE_NAME = "ai4ia.voiceLive.prefs.v4";
const V3_VOICE_PREFERENCES_STORAGE_NAME = "ai4ia.voiceLive.prefs.v3";
export const V2_VOICE_PREFERENCES_STORAGE_NAME = "ai4ia.voiceLive.prefs.v2";
const V1_VOICE_PREFERENCES_STORAGE_NAME = "ai4ia.voiceLive.prefs.v1";

export interface VoicePreferences {
  provider: "azure_openai" | "speech_voice_live";
  // The user's explicit agent pick, or null to always follow the active chat
  // agent (the default). Kept even when temporarily invalid (e.g. the agent is
  // disabled) so it resumes automatically if the agent becomes valid again.
  explicitAgent: string | null;
  // The user's explicit realtime model pick, or null to use the catalog default
  // (the first realtime model).
  model: string | null;
  // Azure Speech managed-model selection is isolated from the Azure OpenAI
  // deployment selection above.
  speechModel: string;
  voice: RealtimeVoice;
  // Per-session governed-tools opt-in. Only ever honored when the server
  // advertises tools as available; the raw preference is kept regardless.
  tools: boolean;
  settings: VoiceSessionSettings;
  speech: SpeechVoiceLiveSettings;
}

export const DEFAULT_VOICE_PREFERENCES: VoicePreferences = {
  provider: DEFAULT_VOICE_PROVIDER,
  explicitAgent: null,
  model: null,
  speechModel: DEFAULT_SPEECH_MODEL_ID,
  voice: DEFAULT_VOICE,
  tools: false,
  settings: DEFAULT_VOICE_SETTINGS,
  speech: DEFAULT_SPEECH_VOICE_LIVE_SETTINGS,
};

// Safe bounds for the advanced numeric settings — mirrors what the realtime
// model actually accepts, so a corrupted/out-of-range stored value can never
// reach the upstream session.update.
export const TEMPERATURE_MIN = 0;
export const TEMPERATURE_MAX = 2;
export const VAD_THRESHOLD_MIN = 0;
export const VAD_THRESHOLD_MAX = 1;
export const VAD_SILENCE_MIN_MS = 0;
export const VAD_SILENCE_MAX_MS = 60_000;

// A permissive ISO-639-1/639-2 language tag with an optional region/script
// subtag (e.g. "en", "en-US", "yue-Hant"). Anything else is dropped rather than
// forwarded to the upstream transcription config.
const LANGUAGE_RE = /^[a-zA-Z]{2,3}(-[a-zA-Z]{2,8})?$/;

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

// A stored string field that must be non-empty, or null (meaning "unset" /
// "use the default"). Any other shape (number, object, empty string) is
// treated as unset rather than guessed at.
function normalizeNullableString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

export function normalizeVoiceSessionSettings(raw: unknown): VoiceSessionSettings {
  if (!raw || typeof raw !== "object") return { ...DEFAULT_VOICE_SETTINGS };
  const r = raw as Record<string, unknown>;

  const temperature = isFiniteNumber(r.temperature)
    ? clamp(r.temperature, TEMPERATURE_MIN, TEMPERATURE_MAX)
    : null;

  const vadType =
    typeof r.vadType === "string" && isVadType(r.vadType)
      ? r.vadType
      : DEFAULT_VOICE_SETTINGS.vadType;

  const vadThreshold = isFiniteNumber(r.vadThreshold)
    ? clamp(r.vadThreshold, VAD_THRESHOLD_MIN, VAD_THRESHOLD_MAX)
    : null;

  const vadSilenceMs = isFiniteNumber(r.vadSilenceMs)
    ? Math.round(clamp(r.vadSilenceMs, VAD_SILENCE_MIN_MS, VAD_SILENCE_MAX_MS))
    : null;

  const transcriptionModel =
    typeof r.transcriptionModel === "string" && r.transcriptionModel.trim().length > 0
      ? r.transcriptionModel
      : DEFAULT_VOICE_SETTINGS.transcriptionModel;

  const language =
    typeof r.language === "string" && (r.language === "" || LANGUAGE_RE.test(r.language))
      ? r.language
      : "";

  return {
    temperature,
    vadType,
    vadThreshold,
    vadSilenceMs,
    transcriptionModel,
    language,
  };
}

// Pure structural + bounds validation of a (possibly stale/malformed) stored
// preferences blob. Never throws; anything unrecognized falls back to the
// matching default field. Catalog membership (does this agent/model still
// exist?) is intentionally NOT checked here — see resolveEffectiveAgent /
// resolveEffectiveModel, which need the live catalog and so live in the
// caller (ChatApp).
export function normalizeVoicePreferences(raw: unknown): VoicePreferences {
  if (!raw || typeof raw !== "object") return { ...DEFAULT_VOICE_PREFERENCES };
  const r = raw as Record<string, unknown>;
  const provider =
    r.provider === "speech_voice_live" ? "speech_voice_live" : DEFAULT_VOICE_PROVIDER;
  return {
    provider,
    explicitAgent: normalizeNullableString(r.explicitAgent),
    model: normalizeNullableString(r.model),
    speechModel: normalizeNullableString(r.speechModel) ?? DEFAULT_SPEECH_MODEL_ID,
    voice:
      typeof r.voice === "string" && isRealtimeVoice(r.voice) ? r.voice : DEFAULT_VOICE,
    tools: typeof r.tools === "boolean" ? r.tools : false,
    settings: normalizeVoiceSessionSettings(r.settings),
    speech: normalizeSpeechVoiceLiveSettings(r.speech),
  };
}

// Resolves the agent to actually use: the explicit pick when it is still a
// known, enabled agent; otherwise the caller's fallback (typically the active
// chat's current agent, or null for the generic assistant). Switching the
// active chat agent never overwrites a stored explicit pick — this function is
// re-evaluated on every render from the unchanged stored value, so a later
// re-enable of a stale agent resumes it automatically.
export function resolveEffectiveAgent(
  explicitAgent: string | null,
  enabledAgentNames: ReadonlySet<string>,
  fallback: string | null,
): string | null {
  return explicitAgent !== null && enabledAgentNames.has(explicitAgent)
    ? explicitAgent
    : fallback;
}

// Resolves the realtime model to actually use: the explicit pick when it is
// still present in the realtime-model catalog; otherwise the caller's fallback
// (typically the catalog default).
export function resolveEffectiveModel(
  explicitModel: string | null,
  realtimeModelIds: ReadonlySet<string>,
  fallback: string | null,
): string | null {
  return explicitModel !== null && realtimeModelIds.has(explicitModel)
    ? explicitModel
    : fallback;
}

export function resolveEffectiveVoiceProvider(
  persistedProvider: VoicePreferences["provider"],
  enabledProviderIds: readonly VoicePreferences["provider"][],
  defaultProvider: VoicePreferences["provider"],
  hasStoredProviderPreference: boolean,
): VoicePreferences["provider"] {
  const enabled = new Set(enabledProviderIds);
  const requested = hasStoredProviderPreference ? persistedProvider : defaultProvider;
  if (enabled.has(requested)) return requested;
  if (enabled.has(defaultProvider)) return defaultProvider;
  return enabledProviderIds[0] ?? DEFAULT_VOICE_PROVIDER;
}

export function normalizeSpeechVoiceLiveSettings(
  raw: unknown,
): SpeechVoiceLiveSettings {
  if (!raw || typeof raw !== "object") return { ...DEFAULT_SPEECH_VOICE_LIVE_SETTINGS };
  const r = raw as Record<string, unknown>;
  const temperature =
    typeof r.temperature === "number" && Number.isFinite(r.temperature)
      ? Math.min(2, Math.max(0, r.temperature))
      : null;
  const voice =
    typeof r.voice === "string" && r.voice.trim().length > 0
      ? r.voice.trim()
      : DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.voice;
  const locale =
    typeof r.locale === "string" && r.locale.trim().length > 0
      ? r.locale.trim()
      : DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.locale;
  const turnDetection =
    typeof r.turnDetection === "string" && r.turnDetection.trim().length > 0
      ? (r.turnDetection.trim() as SpeechVoiceLiveSettings["turnDetection"])
      : DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.turnDetection;
  const noiseSuppression =
    typeof r.noiseSuppression === "string" && r.noiseSuppression.trim().length > 0
      ? (r.noiseSuppression.trim() as SpeechVoiceLiveSettings["noiseSuppression"])
      : DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.noiseSuppression;
  const echoCancellation =
    typeof r.echoCancellation === "string" && r.echoCancellation.trim().length > 0
      ? (r.echoCancellation.trim() as SpeechVoiceLiveSettings["echoCancellation"])
      : DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.echoCancellation;
  const interruptResponse =
    typeof r.interruptResponse === "boolean"
      ? r.interruptResponse
      : DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.interruptResponse;
  const autoTruncate =
    typeof r.autoTruncate === "boolean"
      ? r.autoTruncate
      : DEFAULT_SPEECH_VOICE_LIVE_SETTINGS.autoTruncate;
  return {
    temperature,
    voice,
    locale,
    turnDetection,
    noiseSuppression,
    echoCancellation,
    interruptResponse,
    autoTruncate,
  };
}

function providerDefaultVoice(provider: VoiceProvider): string {
  const voices = provider.capabilities.voices.options as readonly string[];
  return provider.capabilities.voices.default ?? voices[0] ?? "";
}

function sanitizeSpeechPreferences(
  speech: VoicePreferences["speech"],
  provider: VoiceProvider | undefined,
): VoicePreferences["speech"] {
  const normalized = normalizeSpeechVoiceLiveSettings(speech);
  const speechProvider = isSpeechVoiceProvider(provider) ? provider : undefined;
  const voices: readonly string[] =
    speechProvider?.capabilities.voices.options ?? [normalized.voice];
  const locales: readonly string[] =
    speechProvider?.capabilities.locale?.options ?? [normalized.locale];
  const turnDetections: readonly SpeechVoiceLiveSettings["turnDetection"][] =
    speechProvider?.capabilities.turnDetection.options ?? [normalized.turnDetection];
  const noiseSuppression: readonly SpeechVoiceLiveSettings["noiseSuppression"][] =
    speechProvider?.capabilities.noiseSuppression?.options ?? [normalized.noiseSuppression];
  const echoCancellation: readonly SpeechVoiceLiveSettings["echoCancellation"][] =
    speechProvider?.capabilities.echoCancellation?.options ?? [normalized.echoCancellation];
  return {
    ...normalized,
    voice:
      voices.includes(normalized.voice) && speechProvider
        ? normalized.voice
        : speechProvider
          ? providerDefaultVoice(speechProvider)
          : normalized.voice,
    locale:
      locales.includes(normalized.locale) && speechProvider
        ? normalized.locale
        : speechProvider?.capabilities.locale?.default ?? normalized.locale,
    turnDetection:
      turnDetections.includes(normalized.turnDetection) && speechProvider
        ? normalized.turnDetection
        : speechProvider?.capabilities.turnDetection.default ?? normalized.turnDetection,
    noiseSuppression:
      noiseSuppression.includes(normalized.noiseSuppression) && speechProvider
        ? normalized.noiseSuppression
        : speechProvider?.capabilities.noiseSuppression?.default ??
          normalized.noiseSuppression,
    echoCancellation:
      echoCancellation.includes(normalized.echoCancellation) && speechProvider
        ? normalized.echoCancellation
        : speechProvider?.capabilities.echoCancellation?.default ??
          normalized.echoCancellation,
  };
}

export function sanitizeVoicePreferencesForProviders(
  prefs: VoicePreferences,
  providers: VoiceProvider[],
  realtimeModelIds: ReadonlySet<string>,
  defaultRealtimeModelId: string | null,
  voiceToolsAvailable: boolean,
  defaultProviderId: VoicePreferences["provider"],
  hasStoredPreferences: boolean,
): VoicePreferences {
  let provider = resolveEffectiveVoiceProvider(
    prefs.provider,
    providers.map((entry) => entry.id),
    defaultProviderId,
    hasStoredPreferences,
  );
  if (provider === "azure_openai" && realtimeModelIds.size === 0) {
    provider =
      (providers.find((entry) => entry.id === "speech_voice_live")?.id ??
        provider) as VoicePreferences["provider"];
  }
  const speechProvider = providers.find(
    (entry) => entry.id === "speech_voice_live",
  );
  const speechModelIds = isSpeechVoiceProvider(speechProvider)
    ? new Set(speechProvider.managedModels.map((model) => model.id))
    : new Set<string>();
  const defaultSpeechModel = isSpeechVoiceProvider(speechProvider)
    ? speechProvider.defaultManagedModelId
    : DEFAULT_SPEECH_MODEL_ID;
  return {
    ...normalizeVoicePreferences(prefs),
    provider,
    model: resolveEffectiveModel(
      prefs.model,
      realtimeModelIds,
      defaultRealtimeModelId,
    ),
    speechModel: speechModelIds.has(prefs.speechModel)
      ? prefs.speechModel
      : speechModelIds.has(defaultSpeechModel)
        ? defaultSpeechModel
        : [...speechModelIds][0] ?? DEFAULT_SPEECH_MODEL_ID,
    voice: isRealtimeVoice(prefs.voice)
      ? prefs.voice
      : DEFAULT_VOICE_PREFERENCES.voice,
    tools: voiceToolsAvailable && prefs.tools,
    settings: normalizeVoiceSessionSettings(prefs.settings),
    speech: sanitizeSpeechPreferences(prefs.speech, speechProvider),
  };
}

// A narrow Storage-like surface so tests can inject a fake/throwing store
// without touching jsdom's real localStorage.
export type PreferencesStorage = Pick<Storage, "getItem" | "setItem">;

function safeLocalStorage(): PreferencesStorage | undefined {
  try {
    return typeof window !== "undefined" ? window.localStorage : undefined;
  } catch {
    // Some browsers throw merely accessing localStorage in certain privacy
    // modes; treat that the same as "no storage available".
    return undefined;
  }
}

// Loads and normalizes stored preferences. Malformed JSON, a missing/blocked
// storage backend, or a storage access error are all tolerated by design —
// this returns safe defaults rather than throwing, so a corrupted value can
// never break Voice Live startup.
export function loadVoicePreferences(
  storage: PreferencesStorage | undefined = safeLocalStorage(),
): VoicePreferences {
  if (!storage) return { ...DEFAULT_VOICE_PREFERENCES };
  try {
    const raw = storage.getItem(VOICE_PREFERENCES_STORAGE_NAME);
    if (raw) return normalizeVoicePreferences(JSON.parse(raw));
    for (const legacyName of [
      V3_VOICE_PREFERENCES_STORAGE_NAME,
      V2_VOICE_PREFERENCES_STORAGE_NAME,
      V1_VOICE_PREFERENCES_STORAGE_NAME,
    ]) {
      const legacy = storage.getItem(legacyName);
      if (!legacy) continue;
      const migrated = normalizeVoicePreferences(JSON.parse(legacy));
      saveVoicePreferences(migrated, storage);
      return migrated;
    }
    return { ...DEFAULT_VOICE_PREFERENCES };
  } catch {
    return { ...DEFAULT_VOICE_PREFERENCES };
  }
}

export function hasStoredVoicePreferences(
  storage: PreferencesStorage | undefined = safeLocalStorage(),
): boolean {
  if (!storage) return false;
  try {
    const current = storage.getItem(VOICE_PREFERENCES_STORAGE_NAME);
    if (current !== null) {
      const parsed = JSON.parse(current) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return false;
      const provider = (parsed as Record<string, unknown>).provider;
      return provider === "azure_openai" || provider === "speech_voice_live";
    }
    for (const legacyName of [
      V3_VOICE_PREFERENCES_STORAGE_NAME,
      V2_VOICE_PREFERENCES_STORAGE_NAME,
      V1_VOICE_PREFERENCES_STORAGE_NAME,
    ]) {
      const legacy = storage.getItem(legacyName);
      if (legacy === null) continue;
      const parsed = JSON.parse(legacy) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return true;
    }
    return false;
  } catch {
    return false;
  }
}

// Persists preferences. Storage failures (quota exceeded, disabled storage)
// are swallowed by design: losing the ability to remember a preference is not
// worth surfacing as a user-facing error.
export function saveVoicePreferences(
  prefs: VoicePreferences,
  storage: PreferencesStorage | undefined = safeLocalStorage(),
): void {
  if (!storage) return;
  try {
    storage.setItem(VOICE_PREFERENCES_STORAGE_NAME, JSON.stringify(prefs));
  } catch {
    /* best-effort persistence only */
  }
}
