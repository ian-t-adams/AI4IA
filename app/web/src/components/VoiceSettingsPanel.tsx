"use client";

// Compact inline Voice Live settings. Controls are disabled (not hidden) while
// a live session is connecting/live/closing or
// a transcript save is in flight: edits are safe to make any time, but only
// take effect on the *next* connection (see useVoiceLive, which reads voice/
// settings/tools at connect time via refs).
import { useId } from "react";

import {
  AVATAR_LISTENING_MODES,
  effectiveAvatarListening,
  isAvatarListeningMode,
  PLAYBACK_BUFFER_MS,
  PLAYBACK_PROFILES,
  isSpeechVoiceProvider,
  resolveSpeechTranscriptionOption,
  speechEchoReference,
  speechTranscriptionOptions,
  transcriptionOptionLabel,
  VAD_TYPES,
  type AvatarListeningMode,
  type PlaybackProfile,
  type SpeechVoiceLiveSettings,
  type VadType,
  type VoiceProvider,
  type VoiceProviderId,
  type VoiceSessionSettings,
} from "@/lib/voiceLive";
import {
  TEMPERATURE_MAX,
  TEMPERATURE_MIN,
  VAD_SILENCE_MAX_MS,
  VAD_SILENCE_MIN_MS,
  VAD_THRESHOLD_MAX,
  VAD_THRESHOLD_MIN,
} from "@/lib/voicePreferences";
import { formatVoiceName, isPreviewVoice } from "@/lib/voiceNames";

// The sentinel option value for "no explicit pick — follow the default".
// HTML <select> options can't carry a real null, so "" round-trips to/from it
// at the call boundary.
const DEFAULT_OPTION_VALUE = "";
const PLAYBACK_PROFILE_LABELS: Record<PlaybackProfile, string> = {
  fast: "Fast",
  balanced: "Balanced",
  smooth: "Smooth",
};
// Names for the managed models' own transcription defaults.
const MANAGED_TRANSCRIPTION_LABELS: Record<string, string> = {
  "gpt-4o-transcribe": "GPT-4o Transcribe",
  "azure-speech": "Azure Speech",
};
// Plain names for Azure's turn detection modes; the catalog value is what is sent.
const SPEECH_TURN_DETECTION_LABELS: Record<string, string> = {
  azure_semantic_vad: "Semantic (English)",
  azure_semantic_vad_multilingual: "Semantic (multilingual)",
};
const AVATAR_LISTENING_LABELS: Record<AvatarListeningMode, string> = {
  pause: "Pause my microphone (speakers)",
  listen: "Keep listening (headphones)",
  reference: "Keep listening with precise echo cancellation (preview)",
};
const AVATAR_LISTENING_DESCRIPTIONS: Record<AvatarListeningMode, string> = {
  pause:
    "Your microphone sends silence while the avatar speaks, so it can't hear itself. Use Interrupt to cut in.",
  listen: "Talk over the avatar to interrupt it. Without headphones it may hear itself.",
  reference:
    "Talk over the avatar on speakers. This page also sends Azure what it plays, so Azure can remove the avatar's voice from your microphone.",
};

function managedTranscriptionLabel(model: string): string {
  return MANAGED_TRANSCRIPTION_LABELS[model] ?? model;
}

export interface VoiceSettingsModel {
  id: string;
  displayName: string;
}

export interface VoiceSettingsProvider {
  id: VoiceProviderId;
  displayLabel: string;
  description: string;
}

export interface VoiceSettingsAvatarChoice {
  id: string;
  displayName: string;
}

export interface VoiceSettingsPanelProps {
  providers: VoiceSettingsProvider[];
  provider: VoiceProviderId;
  onProviderChange: (provider: VoiceProviderId) => void;
  activeProvider: VoiceProvider;
  openaiRealtimeProtocol?: "preview" | "ga";
  models: VoiceSettingsModel[];
  defaultModelLabel: string;
  explicitModel: string | null;
  onModelChange: (model: string | null) => void;
  speechModel: string;
  onSpeechModelChange: (model: string) => void;
  voice: string;
  onVoiceChange: (voice: string) => void;
  settings: VoiceSessionSettings;
  onSettingsChange: (settings: VoiceSessionSettings) => void;
  speechSettings: SpeechVoiceLiveSettings;
  onSpeechSettingsChange: (settings: SpeechVoiceLiveSettings) => void;
  onReset: () => void;
  // Owned photo avatars usable with Speech Voice Live right now. The picker
  // grants nothing (the server re-checks every connection).
  avatarChoices?: VoiceSettingsAvatarChoice[];
  avatarId?: string | null;
  onAvatarChange?: (id: string | null) => void;
  // False when this browser can't play the avatar stream, so sessions stay voice only.
  avatarVideoSupported?: boolean;
  onOpenPhotoAvatars?: () => void;
  // True while a live session is connecting/live/closing or a transcript save
  // is in flight — controls disable but stay visible; edits apply next
  // connection.
  locked: boolean;
}

const FIELD_STYLE: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 4,
  fontSize: "0.8em",
  color: "var(--fg-muted)",
};

const CONTROL_STYLE: React.CSSProperties = {
  padding: "6px 8px",
  borderRadius: 6,
  border: "1px solid var(--border)",
  background: "var(--bg)",
  color: "var(--fg)",
  fontSize: "0.95em",
};

export function VoiceSettingsPanel({
  providers,
  provider,
  onProviderChange,
  activeProvider,
  openaiRealtimeProtocol = "preview",
  models,
  defaultModelLabel,
  explicitModel,
  onModelChange,
  speechModel,
  onSpeechModelChange,
  voice,
  onVoiceChange,
  settings,
  onSettingsChange,
  speechSettings,
  onSpeechSettingsChange,
  onReset,
  avatarChoices,
  avatarId = null,
  onAvatarChange,
  avatarVideoSupported = true,
  onOpenPhotoAvatars,
  locked,
}: VoiceSettingsPanelProps) {
  const idPrefix = useId();
  const isSpeechProvider = provider === "speech_voice_live";
  const isGaRealtime = !isSpeechProvider && openaiRealtimeProtocol === "ga";
  const unavailableModel =
    explicitModel !== null && !models.some((model) => model.id === explicitModel);
  const selectedProvider = providers.find((entry) => entry.id === provider);
  const speechProvider = isSpeechVoiceProvider(activeProvider) ? activeProvider : undefined;
  const voiceOptions: readonly string[] = activeProvider.capabilities.voices.options;
  const localeOptions: readonly string[] = speechProvider?.capabilities.locale?.options ?? [];
  const selectedSpeechModel = speechProvider?.managedModels.find(
    (model) => model.id === speechModel,
  );
  const transcriptionOptions = speechTranscriptionOptions(selectedSpeechModel, speechProvider);
  const selectedTranscription = resolveSpeechTranscriptionOption(
    selectedSpeechModel,
    speechSettings.transcriptionModel,
    speechProvider,
  );
  const defaultTranscriptionLabel = selectedSpeechModel
    ? managedTranscriptionLabel(selectedSpeechModel.inputTranscription.model)
    : "";
  const previewVoiceSelected = isSpeechProvider && isPreviewVoice(voice);
  const turnDetectionOptions: readonly SpeechVoiceLiveSettings["turnDetection"][] =
    speechProvider?.capabilities.turnDetection.options ?? [];
  const showAvatarPicker = isSpeechProvider && (
    (avatarChoices?.length ?? 0) > 0 || avatarId !== null || Boolean(onOpenPhotoAvatars)
  );
  const unavailableAvatar = avatarId !== null && !avatarChoices?.some((choice) => choice.id === avatarId);
  // The client echo reference is offered only where the server's catalog has it;
  // a saved choice elsewhere shows (and uses) the default.
  const echoReferenceOffered = speechEchoReference(speechProvider ?? null) !== null;
  const listeningModes = AVATAR_LISTENING_MODES.filter(
    (mode) => mode !== "reference" || echoReferenceOffered,
  );
  const avatarListening = effectiveAvatarListening(
    speechSettings.avatarListening,
    speechProvider ?? null,
  );

  function patchSettings(patch: Partial<VoiceSessionSettings>) {
    onSettingsChange({ ...settings, ...patch });
  }

  function patchSpeechSettings(patch: Partial<SpeechVoiceLiveSettings>) {
    onSpeechSettingsChange({ ...speechSettings, ...patch });
  }

  return (
    <div className="voice-settings-panel">
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 10,
          padding: 10,
        }}
      >
        <div style={FIELD_STYLE}>
          <label htmlFor={`${idPrefix}-provider`}>Provider</label>
          <select
            id={`${idPrefix}-provider`}
            aria-describedby={`${idPrefix}-provider-description`}
            value={provider}
            disabled={locked}
            onChange={(e) => onProviderChange(e.target.value as VoiceProviderId)}
            style={CONTROL_STYLE}
          >
            {providers.map((entry) => (
              <option key={entry.id} value={entry.id}>
                {entry.displayLabel}
              </option>
            ))}
          </select>
          <span id={`${idPrefix}-provider-description`} style={{ maxWidth: 260 }}>
            {selectedProvider?.description}
          </span>
        </div>

        {isSpeechProvider ? (
          <>
            <label style={FIELD_STYLE} htmlFor={`${idPrefix}-speech-model`}>
              Speech model
              <select
                id={`${idPrefix}-speech-model`}
                value={speechModel}
                disabled={locked || !speechProvider?.managedModels.length}
                onChange={(event) => onSpeechModelChange(event.target.value)}
                style={CONTROL_STYLE}
              >
                {speechProvider?.managedModels.map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.displayName}
                  </option>
                ))}
              </select>
            </label>
            {selectedSpeechModel && transcriptionOptions.length > 0 && (
              <div style={FIELD_STYLE}>
                <label htmlFor={`${idPrefix}-speech-transcription`}>Transcription</label>
                <select
                  id={`${idPrefix}-speech-transcription`}
                  aria-describedby={
                    selectedTranscription?.preview
                      ? `${idPrefix}-speech-transcription-description`
                      : undefined
                  }
                  value={selectedTranscription?.model ?? DEFAULT_OPTION_VALUE}
                  disabled={locked}
                  onChange={(event) =>
                    patchSpeechSettings({
                      transcriptionModel:
                        event.target.value === DEFAULT_OPTION_VALUE ? null : event.target.value,
                    })
                  }
                  style={CONTROL_STYLE}
                >
                  <option value={DEFAULT_OPTION_VALUE}>
                    Model default ({defaultTranscriptionLabel})
                  </option>
                  {transcriptionOptions.map((option) => (
                    <option key={option.model} value={option.model}>
                      {transcriptionOptionLabel(option)}
                    </option>
                  ))}
                </select>
                {selectedTranscription?.preview && (
                  <span
                    id={`${idPrefix}-speech-transcription-description`}
                    style={{ maxWidth: 260 }}
                  >
                    Preview, no SLA, and not yet confirmed in this region. If Azure refuses it,
                    Voice Live shows the error instead of switching models.
                  </span>
                )}
              </div>
            )}
            {selectedSpeechModel && (
              <div
                style={{
                  ...FIELD_STYLE,
                  flexBasis: "100%",
                  padding: "6px 8px",
                  borderRadius: 6,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                }}
              >
                <span>{selectedSpeechModel.description}</span>
                <strong>
                  {selectedSpeechModel.profile === "native_audio"
                    ? "Native audio"
                    : "Azure Speech chain"}
                  {" · "}
                  {selectedTranscription
                    ? transcriptionOptionLabel(selectedTranscription)
                    : defaultTranscriptionLabel}
                  {" · "}
                  {selectedSpeechModel.initialRegion}
                </strong>
              </div>
            )}
          </>
        ) : (
          <div style={FIELD_STYLE}>
            <label htmlFor={`${idPrefix}-model`}>Realtime model</label>
            <select
              id={`${idPrefix}-model`}
              value={explicitModel ?? DEFAULT_OPTION_VALUE}
              aria-invalid={unavailableModel || undefined}
              aria-describedby={unavailableModel ? `${idPrefix}-model-error` : undefined}
              disabled={locked}
              onChange={(e) =>
                onModelChange(e.target.value === DEFAULT_OPTION_VALUE ? null : e.target.value)
              }
              style={CONTROL_STYLE}
            >
              <option value={DEFAULT_OPTION_VALUE}>{defaultModelLabel}</option>
              {unavailableModel && (
                <option value={explicitModel ?? ""} disabled>
                  Saved model unavailable ({explicitModel})
                </option>
              )}
              {models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.displayName}
                </option>
              ))}
            </select>
            {unavailableModel && (
              <span id={`${idPrefix}-model-error`} role="alert" style={{ maxWidth: 300 }}>
                The saved realtime model is unavailable under the current server
                configuration. Choose an available model or Default; it will not
                be replaced automatically.
              </span>
            )}
          </div>
        )}

        <div style={FIELD_STYLE}>
          <label htmlFor={`${idPrefix}-voice`}>Voice</label>
          <select
            id={`${idPrefix}-voice`}
            aria-describedby={previewVoiceSelected ? `${idPrefix}-voice-description` : undefined}
            value={voice}
            disabled={locked}
            onChange={(e) => onVoiceChange(e.target.value)}
            style={CONTROL_STYLE}
          >
            {voiceOptions.map((v) => (
              <option key={v} value={v}>
                {formatVoiceName(v)}
              </option>
            ))}
          </select>
          {previewVoiceSelected && (
            <span id={`${idPrefix}-voice-description`} style={{ maxWidth: 260 }}>
              Preview voice, no SLA, and not yet tested with photo avatars.
            </span>
          )}
        </div>

        {(showAvatarPicker || onOpenPhotoAvatars) && (
          <div style={FIELD_STYLE}>
            {showAvatarPicker ? (
              <>
                <label htmlFor={`${idPrefix}-avatar`}>Avatar</label>
                <select
                  id={`${idPrefix}-avatar`}
                  aria-describedby={`${idPrefix}-avatar-description`}
                  aria-invalid={avatarVideoSupported && unavailableAvatar || undefined}
                  value={avatarVideoSupported ? avatarId ?? "" : ""}
                  disabled={locked || !avatarVideoSupported || !onAvatarChange}
                  onChange={(event) =>
                    onAvatarChange?.(event.target.value === "" ? null : event.target.value)
                  }
                  style={CONTROL_STYLE}
                >
                  <option value="">None (voice only)</option>
                  {unavailableAvatar ? <option value={avatarId ?? ""} disabled>Selected avatar unavailable</option> : null}
                  {avatarChoices?.map((choice) => (
                    <option key={choice.id} value={choice.id}>
                      {choice.displayName}
                    </option>
                  ))}
                </select>
              </>
            ) : <span>Photo avatar</span>}
            <span id={`${idPrefix}-avatar-description`} style={{ maxWidth: 260 }}>
              {!isSpeechProvider
                ? "Photo avatars use Azure Speech. Choose one from your gallery to switch providers."
                : !avatarVideoSupported
                  ? "This browser can't play avatar video. Choose Voice only in chat to continue."
                  : unavailableAvatar
                    ? "The selected avatar is unavailable. Choose another avatar or None (voice only) before starting."
                    : "Your AI-generated avatar speaks the replies on video. It streams, and is billed, while the session is connected."}
            </span>
            {onOpenPhotoAvatars ? (
              <button type="button" style={CONTROL_STYLE} disabled={locked} onClick={onOpenPhotoAvatars}>
                Choose avatar
              </button>
            ) : null}
          </div>
        )}

        {showAvatarPicker && avatarVideoSupported && (
          <div style={FIELD_STYLE}>
            <label htmlFor={`${idPrefix}-avatar-listening`}>While the avatar talks</label>
            <select
              id={`${idPrefix}-avatar-listening`}
              aria-describedby={`${idPrefix}-avatar-listening-description`}
              value={avatarListening}
              disabled={locked}
              onChange={(event) => {
                const mode = event.target.value;
                if (isAvatarListeningMode(mode)) patchSpeechSettings({ avatarListening: mode });
              }}
              style={CONTROL_STYLE}
            >
              {listeningModes.map((mode) => (
                <option key={mode} value={mode}>
                  {AVATAR_LISTENING_LABELS[mode]}
                </option>
              ))}
            </select>
            <span id={`${idPrefix}-avatar-listening-description`} style={{ maxWidth: 260 }}>
              {AVATAR_LISTENING_DESCRIPTIONS[avatarListening]}
            </span>
          </div>
        )}

        <div style={{ flexBasis: "100%" }}>
          <div
            style={{
              fontSize: "0.8em",
              color: "var(--fg-muted)",
              fontWeight: 600,
            }}
          >
            Audio controls
          </div>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 10,
              padding: "8px 0 2px",
            }}
          >
            <div style={FIELD_STYLE}>
              <label htmlFor={`${idPrefix}-temperature`}>Temperature</label>
              <input
                id={`${idPrefix}-temperature`}
                aria-describedby={isGaRealtime ? `${idPrefix}-temperature-description` : undefined}
                type="number"
                min={TEMPERATURE_MIN}
                max={TEMPERATURE_MAX}
                step={0.1}
                value={
                  isGaRealtime ? "" : (isSpeechProvider
                    ? speechSettings.temperature
                    : settings.temperature) ?? ""
                }
                disabled={locked || isGaRealtime}
                placeholder="Model default"
                onChange={(e) =>
                  isSpeechProvider
                    ? patchSpeechSettings({
                        temperature:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    : patchSettings({
                        temperature:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                }
                style={CONTROL_STYLE}
              />
              {isGaRealtime && (
                <span id={`${idPrefix}-temperature-description`} style={{ maxWidth: 240 }}>
                  Temperature is not configurable with GA Realtime.
                </span>
              )}
            </div>

            <div style={FIELD_STYLE}>
              <label htmlFor={`${idPrefix}-playback-profile`}>Playback stability</label>
              <select
                id={`${idPrefix}-playback-profile`}
                aria-describedby={`${idPrefix}-playback-profile-description`}
                value={settings.playbackProfile}
                disabled={locked}
                onChange={(event) =>
                  patchSettings({
                    playbackProfile: event.target.value as PlaybackProfile,
                  })
                }
                style={CONTROL_STYLE}
              >
                {PLAYBACK_PROFILES.map((profile) => (
                  <option key={profile} value={profile}>
                    {PLAYBACK_PROFILE_LABELS[profile]} ({PLAYBACK_BUFFER_MS[profile]} ms)
                  </option>
                ))}
              </select>
              <span
                id={`${idPrefix}-playback-profile-description`}
                style={{ maxWidth: 240 }}
              >
                Higher stability adds a little delay to smooth network jitter.
              </span>
            </div>

            {isSpeechProvider ? (
              <>
                {localeOptions.length > 1 && (
                  <label style={FIELD_STYLE} htmlFor={`${idPrefix}-locale`}>
                    Locale
                    <select
                      id={`${idPrefix}-locale`}
                      value={speechSettings.locale}
                      disabled={locked}
                      onChange={(e) => patchSpeechSettings({ locale: e.target.value })}
                      style={CONTROL_STYLE}
                    >
                      {localeOptions.map((value) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ))}
                    </select>
                  </label>
                )}

                <div style={FIELD_STYLE}>
                  <label htmlFor={`${idPrefix}-speech-turn`}>Turn detection</label>
                  <select
                    id={`${idPrefix}-speech-turn`}
                    aria-describedby={`${idPrefix}-speech-turn-description`}
                    value={speechSettings.turnDetection}
                    disabled={locked || turnDetectionOptions.length === 0}
                    onChange={(e) =>
                      patchSpeechSettings({
                        turnDetection: e.target.value as SpeechVoiceLiveSettings["turnDetection"],
                      })
                    }
                    style={CONTROL_STYLE}
                  >
                    {turnDetectionOptions.map((value: SpeechVoiceLiveSettings["turnDetection"]) => (
                      <option key={value} value={value}>
                        {SPEECH_TURN_DETECTION_LABELS[value] ?? value}
                      </option>
                    ))}
                  </select>
                  <span id={`${idPrefix}-speech-turn-description`} style={{ maxWidth: 240 }}>
                    How Azure tells that you have finished speaking.
                  </span>
                </div>

                <div style={{ ...FIELD_STYLE, maxWidth: 260 }}>
                  <span>Input processing</span>
                  <strong style={{ color: "var(--fg)" }}>Managed by Azure Speech</strong>
                  <span>
                    {speechSettings.locale}; deep noise suppression and echo cancellation.
                    With a photo avatar, your browser is also asked to cancel the
                    avatar&apos;s voice, unless precise echo cancellation gives Azure
                    what this page plays instead.
                  </span>
                </div>

                <label
                  style={{
                    ...FIELD_STYLE,
                    flexDirection: "row",
                    alignItems: "center",
                    gap: 6,
                    alignSelf: "flex-end",
                  }}
                  htmlFor={`${idPrefix}-speech-interrupt`}
                >
                  <input
                    id={`${idPrefix}-speech-interrupt`}
                    type="checkbox"
                    checked={speechSettings.interruptResponse}
                    disabled={locked}
                    onChange={(e) =>
                      patchSpeechSettings({ interruptResponse: e.target.checked })
                    }
                  />
                  Stop the reply when I start talking
                </label>

                <div style={{ ...FIELD_STYLE, maxWidth: 260, alignSelf: "flex-end" }}>
                  <label
                    style={{ display: "flex", alignItems: "center", gap: 6 }}
                    htmlFor={`${idPrefix}-speech-truncate`}
                  >
                    <input
                      id={`${idPrefix}-speech-truncate`}
                      type="checkbox"
                      aria-describedby={`${idPrefix}-speech-truncate-description`}
                      checked={speechSettings.autoTruncate}
                      disabled={locked}
                      onChange={(e) => patchSpeechSettings({ autoTruncate: e.target.checked })}
                    />
                    Let Azure trim interrupted replies
                  </label>
                  <span id={`${idPrefix}-speech-truncate-description`}>
                    Keeps only the part you heard in the conversation. When off, the browser
                    trims voice-only replies itself.
                  </span>
                </div>
              </>
            ) : (
              <>
                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-vad-type`}>
                  Turn detection
                  <select
                    id={`${idPrefix}-vad-type`}
                    value={settings.vadType}
                    disabled={locked}
                    onChange={(e) => patchSettings({ vadType: e.target.value as VadType })}
                    style={CONTROL_STYLE}
                  >
                    {VAD_TYPES.map((v) => (
                      <option key={v} value={v}>
                        {v === "server_vad"
                          ? "Energy threshold (server_vad)"
                          : "Semantic (semantic_vad)"}
                      </option>
                    ))}
                  </select>
                </label>

                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-vad-threshold`}>
                  VAD threshold
                  <input
                    id={`${idPrefix}-vad-threshold`}
                    type="number"
                    min={VAD_THRESHOLD_MIN}
                    max={VAD_THRESHOLD_MAX}
                    step={0.05}
                    value={settings.vadThreshold ?? ""}
                    disabled={locked || settings.vadType !== "server_vad"}
                    placeholder="Model default"
                    onChange={(e) =>
                      patchSettings({
                        vadThreshold: e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    style={CONTROL_STYLE}
                  />
                </label>

                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-vad-silence`}>
                  Reply after silence (ms)
                  <input
                    id={`${idPrefix}-vad-silence`}
                    type="number"
                    min={VAD_SILENCE_MIN_MS}
                    max={VAD_SILENCE_MAX_MS}
                    step={50}
                    value={settings.vadSilenceMs ?? ""}
                    disabled={locked || settings.vadType !== "server_vad"}
                    placeholder="Model default"
                    onChange={(e) =>
                      patchSettings({
                        vadSilenceMs: e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                    style={CONTROL_STYLE}
                  />
                </label>

                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-transcription-model`}>
                  Transcription model
                  <input
                    id={`${idPrefix}-transcription-model`}
                    type="text"
                    value={settings.transcriptionModel}
                    disabled={locked}
                    onChange={(e) => patchSettings({ transcriptionModel: e.target.value })}
                    style={CONTROL_STYLE}
                  />
                </label>

                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-language`}>
                  Language hint
                  <input
                    id={`${idPrefix}-language`}
                    type="text"
                    value={settings.language}
                    disabled={locked}
                    placeholder="Auto"
                    onChange={(e) => patchSettings({ language: e.target.value })}
                    style={{ ...CONTROL_STYLE, width: 90 }}
                  />
                </label>
              </>
            )}

            <button
              type="button"
              onClick={onReset}
              disabled={locked}
              style={{
                alignSelf: "flex-end",
                padding: "6px 10px",
                borderRadius: 6,
                border: "1px solid var(--border)",
                background: "var(--bg)",
                color: "var(--fg)",
                fontSize: "0.8em",
                cursor: locked ? "not-allowed" : "pointer",
              }}
            >
              Reset defaults
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
