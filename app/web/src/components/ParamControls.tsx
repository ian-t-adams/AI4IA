"use client";

import type { ChatParams, ModelEntry } from "@/lib/types";
import { HelpTooltip } from "./HelpTooltip";

// Fallback max-output ceiling when the active model declares no metadata
// (e.g. model-router). Mirrors the previous hardcoded input bound so behavior
// is unchanged for models without a published max-output.
const DEFAULT_MAX_OUTPUT = 32000;

// Two of the values the provider accepts read badly as bare title-case in a
// dropdown that already has a "Model default" entry: "None" looks like the same
// thing (it is the opposite -- an explicit instruction not to reason) and
// "Xhigh" is not a word. Everything else title-cases fine, so this is an
// override map rather than a required table: a value the provider adds later
// still renders, just without a hand-written label.
const EFFORT_LABELS: Record<string, string> = {
  none: "None (skip reasoning)",
  xhigh: "Extra high",
};

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
  // Server-computed traits. Default to the permissive shape so a model that
  // predates these fields (or a stubbed catalog in tests) keeps its controls.
  const showSampling = model?.supportsSampling ?? true;
  const effortOptions = model?.reasoningEffortOptions ?? [];
  // Switching models can leave an effort the new model does not offer (e.g.
  // "minimal" carried from GPT-5.4 to a GPT-5.6 model, which 400s on it).
  // Show "Model default" rather than a value the <select> has no option for,
  // which browsers render as a blank or silently-wrong selection. The server
  // drops the stale value too; this keeps the control honest about that.
  const effortValue = effortOptions.includes(params.reasoning_effort ?? "")
    ? (params.reasoning_effort as string)
    : "";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {effortOptions.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <label
              htmlFor="param-reasoning-effort"
              style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}
            >
              Reasoning effort
            </label>
            <HelpTooltip label="Reasoning effort" size="sm">
              How long this model thinks before it starts writing the reply. Higher
              effort spends more hidden reasoning tokens, which usually improves
              hard analytical and coding answers but costs more and takes longer.
              &ldquo;Model default&rdquo; leaves the choice to the model. For
              straightforward questions a lower setting is often just as good and
              noticeably faster.
            </HelpTooltip>
          </div>
          <select
            id="param-reasoning-effort"
            value={effortValue}
            disabled={disabled}
            onChange={(e) => {
              const v = e.target.value;
              const next = { ...params };
              if (v) next.reasoning_effort = v;
              else delete next.reasoning_effort;
              onChange(next);
            }}
            style={{ padding: "6px 8px" }}
          >
            <option value="">Model default</option>
            {effortOptions.map((o) => (
              <option key={o} value={o}>
                {EFFORT_LABELS[o] ?? o.charAt(0).toUpperCase() + o.slice(1)}
              </option>
            ))}
          </select>
        </div>
      )}
      {showSampling && (
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
              Controls how much randomness the model uses when choosing words. Lower
              it (toward 0) for consistent, predictable answers like code or facts;
              raise it (toward 2) for more varied, creative output. Higher values
              also increase the chance of less accurate or coherent answers. Default
              is 0.7.
            </>
          }
        />
      )}
      {showSampling && (
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
              An alternate way to control variety: the model only considers the
              smallest set of next-word options whose combined likelihood reaches
              this value. 1 means &ldquo;consider everything&rdquo;; lowering it
              narrows the model to its most likely words. Most people adjust
              Temperature or Top P, not both, to keep behavior predictable. Default
              is 1.
            </>
          }
        />
      )}
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
            generate.{" "}
            {model?.maxOutputTokens != null ? (
              <>
                This value is capped by the selected model&apos;s published maximum
                output (currently {model.maxOutputTokens.toLocaleString()}). Leaving it
                at the default, 1024, is treated as &ldquo;no preference&rdquo; and
                expands to the model&apos;s full ceiling instead of actually capping the
                reply at 1024 &mdash; pick a different value if you want a genuinely
                short reply.
              </>
            ) : (
              <>
                This model publishes no maximum-output size, so 1024 is sent as a
                literal cap on the reply length instead of expanding to a model
                ceiling &mdash; raise it if replies are getting cut short.
              </>
            )}{" "}
            A few flagship models also enforce a much higher minimum (at least 16,384)
            regardless of a lower value here, since part of that budget goes to hidden
            reasoning before the visible reply is written.
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
