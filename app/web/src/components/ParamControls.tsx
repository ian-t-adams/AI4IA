"use client";

import type { ChatParams, ModelEntry } from "@/lib/types";

// Fallback max-output ceiling when the active model declares no metadata
// (e.g. model-router). Mirrors the previous hardcoded input bound so behavior
// is unchanged for models without a published max-output.
const DEFAULT_MAX_OUTPUT = 32000;

function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
  disabled,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  disabled: boolean;
}) {
  const id = `param-${label.replace(/\s+/g, "-").toLowerCase()}`;
  return (
    <label htmlFor={id} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
        {label}: <strong>{value}</strong>
      </span>
      <input
        id={id}
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

export function ParamControls({
  params,
  onChange,
  model,
  disabled = false,
}: {
  params: ChatParams;
  onChange: (p: ChatParams) => void;
  model?: ModelEntry | null;
  disabled?: boolean;
}) {
  // The active model's published max-output ceiling drives the input bound and
  // is the single source of truth for clamping. The backend caps too (lower
  // only), so this is a UX nicety, not the enforcement point.
  const cap = model?.maxOutputTokens ?? DEFAULT_MAX_OUTPUT;
  const shown = Math.min(params.max_tokens ?? 1024, cap);
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Slider
        label="Temperature"
        value={params.temperature ?? 0.7}
        min={0}
        max={2}
        step={0.1}
        disabled={disabled}
        onChange={(v) => onChange({ ...params, temperature: v })}
      />
      <Slider
        label="Top P"
        value={params.top_p ?? 1}
        min={0}
        max={1}
        step={0.05}
        disabled={disabled}
        onChange={(v) => onChange({ ...params, top_p: v })}
      />
      <label style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
          Max tokens{" "}
          {model?.maxOutputTokens != null && (
            <span style={{ opacity: 0.7 }}>
              (model max: {model.maxOutputTokens.toLocaleString()})
            </span>
          )}
        </span>
        <input
          type="number"
          min={1}
          max={cap}
          value={shown}
          disabled={disabled}
          onChange={(e) => {
            const raw = Number(e.target.value);
            const clamped = Math.max(1, Math.min(raw || 1, cap));
            onChange({ ...params, max_tokens: clamped });
          }}
          style={{ padding: "6px 8px" }}
        />
      </label>
    </div>
  );
}
