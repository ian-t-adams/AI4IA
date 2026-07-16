"use client";

// Compact inline Voice Live settings disclosure. Lives next to the mic
// controls in the composer — a native <details>/<summary> disclosure, not a
// dialog/modal, so it never covers or replaces the chat transcript. Controls
// are disabled (not hidden) while a live session is connecting/live/closing or
// a transcript save is in flight: edits are safe to make any time, but only
// take effect on the *next* connection (see useVoiceLive, which reads voice/
// settings/tools at connect time via refs).
import { useId } from "react";

import type { AgentSummary } from "@/lib/types";
import {
  isSpeechVoiceProvider,
  VAD_TYPES,
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

// The sentinel option value for "no explicit pick — follow the default".
// HTML <select> options can't carry a real null, so "" round-trips to/from it
// at the call boundary.
const DEFAULT_OPTION_VALUE = "";

export interface VoiceSettingsModel {
  id: string;
  displayName: string;
}

export interface VoiceSettingsProvider {
  id: VoiceProviderId;
  displayLabel: string;
  description: string;
}

export interface VoiceSettingsPanelProps {
  agents: AgentSummary[];
  providers: VoiceSettingsProvider[];
  provider: VoiceProviderId;
  onProviderChange: (provider: VoiceProviderId) => void;
  activeProvider: VoiceProvider;
  // Label for the "use the default" agent option, e.g. naming the agent the
  // active chat is currently using (or "the generic assistant" when none).
  defaultAgentLabel: string;
  explicitAgent: string | null;
  onAgentChange: (agent: string | null) => void;
  models: VoiceSettingsModel[];
  defaultModelLabel: string;
  explicitModel: string | null;
  onModelChange: (model: string | null) => void;
  speechModel: string;
  onSpeechModelChange: (model: string) => void;
  voice: string;
  onVoiceChange: (voice: string) => void;
  toolsAvailable: boolean;
  tools: boolean;
  onToolsChange: (enabled: boolean) => void;
  settings: VoiceSessionSettings;
  onSettingsChange: (settings: VoiceSessionSettings) => void;
  speechSettings: SpeechVoiceLiveSettings;
  onSpeechSettingsChange: (settings: SpeechVoiceLiveSettings) => void;
  onReset: () => void;
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
  agents,
  providers,
  provider,
  onProviderChange,
  activeProvider,
  defaultAgentLabel,
  explicitAgent,
  onAgentChange,
  models,
  defaultModelLabel,
  explicitModel,
  onModelChange,
  speechModel,
  onSpeechModelChange,
  voice,
  onVoiceChange,
  toolsAvailable,
  tools,
  onToolsChange,
  settings,
  onSettingsChange,
  speechSettings,
  onSpeechSettingsChange,
  onReset,
  locked,
}: VoiceSettingsPanelProps) {
  const idPrefix = useId();
  const enabledAgents = agents.filter((agent) => agent.enabled);
  const isSpeechProvider = provider === "speech_voice_live";
  const speechProvider = isSpeechVoiceProvider(activeProvider) ? activeProvider : undefined;
  const voiceOptions: readonly string[] = activeProvider.capabilities.voices.options;
  const localeOptions: readonly string[] = speechProvider?.capabilities.locale?.options ?? [];
  const selectedSpeechModel = speechProvider?.managedModels.find(
    (model) => model.id === speechModel,
  );
  const turnDetectionOptions: readonly SpeechVoiceLiveSettings["turnDetection"][] =
    speechProvider?.capabilities.turnDetection.options ?? [];
  const noiseSuppressionOptions: readonly SpeechVoiceLiveSettings["noiseSuppression"][] =
    speechProvider?.capabilities.noiseSuppression?.options ?? [];
  const echoCancellationOptions: readonly SpeechVoiceLiveSettings["echoCancellation"][] =
    speechProvider?.capabilities.echoCancellation?.options ?? [];

  function patchSettings(patch: Partial<VoiceSessionSettings>) {
    onSettingsChange({ ...settings, ...patch });
  }

  function patchSpeechSettings(patch: Partial<SpeechVoiceLiveSettings>) {
    onSpeechSettingsChange({ ...speechSettings, ...patch });
  }

  return (
    <details
      style={{
        border: "1px solid var(--border)",
        borderRadius: 8,
        background: "var(--bg-elevated)",
      }}
    >
      <summary
        style={{
          cursor: "pointer",
          padding: "6px 10px",
          fontSize: "0.8em",
          color: "var(--fg-muted)",
          userSelect: "none",
        }}
      >
        Voice settings
      </summary>
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 10,
          padding: "4px 10px 10px",
        }}
      >
        <label style={FIELD_STYLE} htmlFor={`${idPrefix}-provider`}>
          Provider
          <select
            id={`${idPrefix}-provider`}
            value={provider}
            disabled={locked}
            onChange={(e) => onProviderChange(e.target.value as VoiceProviderId)}
            style={CONTROL_STYLE}
          >
            {providers.map((entry) => (
              <option key={entry.id} value={entry.id} title={entry.description}>
                {entry.displayLabel}
              </option>
            ))}
          </select>
        </label>

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
                  {selectedSpeechModel.inputTranscription.model ===
                  "gpt-4o-transcribe"
                    ? "GPT-4o Transcribe"
                    : "Azure Speech"}
                  {" · "}
                  {selectedSpeechModel.initialRegion}
                </strong>
              </div>
            )}
          </>
        ) : (
          <label style={FIELD_STYLE} htmlFor={`${idPrefix}-model`}>
            Realtime model
            <select
              id={`${idPrefix}-model`}
              value={explicitModel ?? DEFAULT_OPTION_VALUE}
              disabled={locked}
              onChange={(e) =>
                onModelChange(e.target.value === DEFAULT_OPTION_VALUE ? null : e.target.value)
              }
              style={CONTROL_STYLE}
            >
              <option value={DEFAULT_OPTION_VALUE}>{defaultModelLabel}</option>
              {models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.displayName}
                </option>
              ))}
            </select>
          </label>
        )}

        <label style={FIELD_STYLE} htmlFor={`${idPrefix}-voice`}>
          Voice
          <select
            id={`${idPrefix}-voice`}
            value={voice}
            disabled={locked}
            onChange={(e) => onVoiceChange(e.target.value)}
            style={CONTROL_STYLE}
          >
            {voiceOptions.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>

        <label style={FIELD_STYLE} htmlFor={`${idPrefix}-agent`}>
          Agent
          <select
            id={`${idPrefix}-agent`}
            value={explicitAgent ?? DEFAULT_OPTION_VALUE}
            disabled={locked}
            onChange={(e) =>
              onAgentChange(e.target.value === DEFAULT_OPTION_VALUE ? null : e.target.value)
            }
            style={CONTROL_STYLE}
          >
            <option value={DEFAULT_OPTION_VALUE}>{defaultAgentLabel}</option>
            {enabledAgents.map((agent) => (
              <option key={agent.name} value={agent.name}>
                {agent.displayName}
              </option>
            ))}
          </select>
        </label>

        {toolsAvailable && (
          <label
            style={{
              ...FIELD_STYLE,
              flexDirection: "row",
              alignItems: "center",
              gap: 6,
              alignSelf: "flex-end",
            }}
            htmlFor={`${idPrefix}-tools`}
          >
            <input
              id={`${idPrefix}-tools`}
              type="checkbox"
              checked={tools}
              disabled={locked}
              onChange={(e) => onToolsChange(e.target.checked)}
            />
            Allow governed tools in voice
          </label>
        )}

        <details style={{ flexBasis: "100%" }}>
          <summary
            style={{
              cursor: "pointer",
              fontSize: "0.8em",
              color: "var(--fg-muted)",
              userSelect: "none",
            }}
          >
            Advanced
          </summary>
          <div
            style={{
              display: "flex",
              flexWrap: "wrap",
              gap: 10,
              padding: "8px 0 2px",
            }}
          >
            <label
              style={{ ...FIELD_STYLE, flexBasis: "100%" }}
              htmlFor={`${idPrefix}-instructions`}
            >
              Instructions
              <textarea
                id={`${idPrefix}-instructions`}
                value={
                  isSpeechProvider ? speechSettings.instructions : settings.instructions
                }
                disabled={locked}
                onChange={(e) =>
                  isSpeechProvider
                    ? patchSpeechSettings({ instructions: e.target.value })
                    : patchSettings({ instructions: e.target.value })
                }
                rows={2}
                style={{ ...CONTROL_STYLE, resize: "vertical", fontFamily: "inherit" }}
              />
            </label>

            <label style={FIELD_STYLE} htmlFor={`${idPrefix}-temperature`}>
              Temperature
              <input
                id={`${idPrefix}-temperature`}
                type="number"
                min={TEMPERATURE_MIN}
                max={TEMPERATURE_MAX}
                step={0.1}
                value={
                  (isSpeechProvider
                    ? speechSettings.temperature
                    : settings.temperature) ?? ""
                }
                disabled={locked}
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
            </label>

            {isSpeechProvider ? (
              <>
                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-locale`}>
                  Locale
                  <select
                    id={`${idPrefix}-locale`}
                    value={speechSettings.locale}
                    disabled={locked || localeOptions.length === 0}
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

                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-speech-turn`}>
                  Turn detection
                  <select
                    id={`${idPrefix}-speech-turn`}
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
                        {value}
                      </option>
                    ))}
                  </select>
                </label>

                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-noise-suppression`}>
                  Noise suppression
                  <select
                    id={`${idPrefix}-noise-suppression`}
                    value={speechSettings.noiseSuppression}
                    disabled={locked || noiseSuppressionOptions.length === 0}
                    onChange={(e) =>
                      patchSpeechSettings({
                        noiseSuppression:
                          e.target.value as SpeechVoiceLiveSettings["noiseSuppression"],
                      })
                    }
                    style={CONTROL_STYLE}
                  >
                    {noiseSuppressionOptions.map(
                      (value: SpeechVoiceLiveSettings["noiseSuppression"]) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ),
                    )}
                  </select>
                </label>

                <label style={FIELD_STYLE} htmlFor={`${idPrefix}-echo-cancellation`}>
                  Echo cancellation
                  <select
                    id={`${idPrefix}-echo-cancellation`}
                    value={speechSettings.echoCancellation}
                    disabled={locked || echoCancellationOptions.length === 0}
                    onChange={(e) =>
                      patchSpeechSettings({
                        echoCancellation:
                          e.target.value as SpeechVoiceLiveSettings["echoCancellation"],
                      })
                    }
                    style={CONTROL_STYLE}
                  >
                    {echoCancellationOptions.map(
                      (value: SpeechVoiceLiveSettings["echoCancellation"]) => (
                        <option key={value} value={value}>
                          {value}
                        </option>
                      ),
                    )}
                  </select>
                </label>

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
                  Interrupt response on barge-in
                </label>

                <label
                  style={{
                    ...FIELD_STYLE,
                    flexDirection: "row",
                    alignItems: "center",
                    gap: 6,
                    alignSelf: "flex-end",
                  }}
                  htmlFor={`${idPrefix}-speech-truncate`}
                >
                  <input
                    id={`${idPrefix}-speech-truncate`}
                    type="checkbox"
                    checked={speechSettings.autoTruncate}
                    disabled={locked}
                    onChange={(e) => patchSpeechSettings({ autoTruncate: e.target.checked })}
                  />
                  Auto truncate on barge-in
                </label>
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
                  Silence (ms)
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
        </details>
      </div>
    </details>
  );
}
