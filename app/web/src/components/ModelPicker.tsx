"use client";

import type { ModelEntry } from "@/lib/types";

export function ModelPicker({
  models,
  value,
  onChange,
}: {
  models: ModelEntry[];
  value: string | null;
  onChange: (modelId: string) => void;
}) {
  // Only conversational models belong in the chat picker. Capability models
  // (image, tts, transcription, embedding, …) are reached through their own
  // surfaces/tools, not selected as a raw chat target.
  const grouped = models
    .filter((m) => m.conversational)
    .reduce<Record<string, ModelEntry[]>>((acc, m) => {
      (acc[m.category] ??= []).push(m);
      return acc;
    }, {});

  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>Model</span>
      <select
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value)}
        style={{ padding: "8px 10px", minWidth: 220 }}
      >
        <option value="" disabled>
          Select a model…
        </option>
        {Object.entries(grouped).map(([category, entries]) => (
          <optgroup key={category} label={category}>
            {entries.map((m) => (
              <option key={m.id} value={m.id}>
                {m.displayName}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </label>
  );
}
