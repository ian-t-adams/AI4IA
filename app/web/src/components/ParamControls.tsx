"use client";

import type { ChatParams, ModelEntry } from "@/lib/types";
import { HelpTooltip } from "./HelpTooltip";

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
  help,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
  disabled: boolean;
  help: React.ReactNode;
}) {
  const id = `param-${label.replace(/\s+/g, "-").toLowerCase()}`;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <label htmlFor={id} style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
          {label}: <strong>{value}</strong>
        </label>
        <HelpTooltip label={label} size="sm">
          {help}
        </HelpTooltip>
      </div>
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
    </div>
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
        help={
          <>
            Controls how much randomness the model uses when choosing words. Lower it
            (toward 0) for consistent, predictable answers like code or facts; raise it
            (toward 2) for more varied, creative output. Higher values also increase the
            chance of less accurate or coherent answers. Default is 0.7.
          </>
        }
      />
      <Slider
        label="Top P"
        value={params.top_p ?? 1}
        min={0}
        max={1}
        step={0.05}
        disabled={disabled}
        onChange={(v) => onChange({ ...params, top_p: v })}
        help={
          <>
            An alternate way to control variety: the model only considers the smallest
            set of next-word options whose combined likelihood reaches this value. 1
            means &ldquo;consider everything&rdquo;; lowering it narrows the model to
            its most likely words. Most people adjust Temperature or Top P, not both, to
            keep behavior predictable. Default is 1.
          </>
        }
      />
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <label htmlFor="param-max-tokens" style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
            Max tokens{" "}
            {model?.maxOutputTokens != null && (
              <span style={{ opacity: 0.7 }}>
                (model max: {model.maxOutputTokens.toLocaleString()})
              </span>
            )}
          </label>
          <HelpTooltip label="Max tokens" size="sm">
            The maximum length of the model&apos;s reply, in tokens (roughly 3&ndash;4
            characters each). Raise it for long-form answers like essays or code files;
            lower it to keep replies short. Longer replies cost more and take longer to
            generate, and this value is always capped by the selected model&apos;s own
            maximum output{" "}
            {model?.maxOutputTokens != null
              ? `(currently ${model.maxOutputTokens.toLocaleString()}).`
              : "."}
          </HelpTooltip>
        </div>
        <input
          id="param-max-tokens"
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
      </div>
    </div>
  );
}
