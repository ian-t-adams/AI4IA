"use client";

import type { ChatParams } from "@/lib/types";

function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
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
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

export function ParamControls({
  params,
  onChange,
}: {
  params: ChatParams;
  onChange: (p: ChatParams) => void;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <Slider
        label="Temperature"
        value={params.temperature ?? 0.7}
        min={0}
        max={2}
        step={0.1}
        onChange={(v) => onChange({ ...params, temperature: v })}
      />
      <Slider
        label="Top P"
        value={params.top_p ?? 1}
        min={0}
        max={1}
        step={0.05}
        onChange={(v) => onChange({ ...params, top_p: v })}
      />
      <label style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
          Max tokens
        </span>
        <input
          type="number"
          min={1}
          max={32000}
          value={params.max_tokens ?? 1024}
          onChange={(e) =>
            onChange({ ...params, max_tokens: Number(e.target.value) })
          }
          style={{ padding: "6px 8px" }}
        />
      </label>
    </div>
  );
}
