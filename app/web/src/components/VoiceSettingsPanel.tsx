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
  REALTIME_VOICES,
  VAD_TYPES,
  type RealtimeVoice,
  type VadType,
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

export interface VoiceSettingsPanelProps {
  agents: AgentSummary[];
  // Label for the "use the default" agent option, e.g. naming the agent the
  // active chat is currently using (or "the generic assistant" when none).
  defaultAgentLabel: string;
  explicitAgent: string | null;
  onAgentChange: (agent: string | null) => void;
  models: VoiceSettingsModel[];
  defaultModelLabel: string;
  explicitModel: string | null;
  onModelChange: (model: string | null) => void;
  voice: RealtimeVoice;
  onVoiceChange: (voice: RealtimeVoice) => void;
  toolsAvailable: boolean;
  tools: boolean;
  onToolsChange: (enabled: boolean) => void;
  settings: VoiceSessionSettings;
  onSettingsChange: (settings: VoiceSessionSettings) => void;
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
  defaultAgentLabel,
  explicitAgent,
  onAgentChange,
  models,
  defaultModelLabel,
  explicitModel,
  onModelChange,
  voice,
  onVoiceChange,
  toolsAvailable,
  tools,
  onToolsChange,
  settings,
  onSettingsChange,
  onReset,
  locked,
}: VoiceSettingsPanelProps) {
  const idPrefix = useId();
  const enabledAgents = agents.filter((agent) => agent.enabled);

  function patchSettings(patch: Partial<VoiceSessionSettings>) {
    onSettingsChange({ ...settings, ...patch });
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

        <label style={FIELD_STYLE} htmlFor={`${idPrefix}-voice`}>
          Voice
          <select
            id={`${idPrefix}-voice`}
            value={voice}
            disabled={locked}
            onChange={(e) => onVoiceChange(e.target.value as RealtimeVoice)}
            style={CONTROL_STYLE}
          >
            {REALTIME_VOICES.map((v) => (
              <option key={v} value={v}>
                {v}
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
                value={settings.instructions}
                disabled={locked}
                onChange={(e) => patchSettings({ instructions: e.target.value })}
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
                value={settings.temperature ?? ""}
                disabled={locked}
                placeholder="Model default"
                onChange={(e) =>
                  patchSettings({
                    temperature: e.target.value === "" ? null : Number(e.target.value),
                  })
                }
                style={CONTROL_STYLE}
              />
            </label>

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
                    {v === "server_vad" ? "Energy threshold (server_vad)" : "Semantic (semantic_vad)"}
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
