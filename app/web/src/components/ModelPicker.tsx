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
  const grouped = models.reduce<Record<string, ModelEntry[]>>((acc, m) => {
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
