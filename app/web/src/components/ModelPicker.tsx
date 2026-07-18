"use client";

import { MODEL_CATEGORY_HELP } from "@/lib/modelHelp";
import type { ModelEntry } from "@/lib/types";

/** Compact human label for a context window, e.g. 400000 -> "400K", 1047576 -> "1M". */
export function formatContextWindow(tokens: number): string {
  if (tokens >= 1_000_000) {
    const m = tokens / 1_000_000;
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (tokens >= 1_000) return `${Math.round(tokens / 1_000)}K`;
  return `${tokens}`;
}

/**
 * Groups conversational models by category (e.g. chat, reasoning, router) for
 * rendering as <optgroup>s. Shared by every model picker so the grouping and
 * ordering stays identical wherever a model select appears.
 */
export function groupConversationalModels(
  models: ModelEntry[],
): Record<string, ModelEntry[]> {
  return models
    .filter((m) => m.conversational)
    .reduce<Record<string, ModelEntry[]>>((acc, m) => {
      (acc[m.category] ??= []).push(m);
      return acc;
    }, {});
}

/**
 * Plain-language what/when/tradeoffs for a model category, shown as visible
 * text (not just a hover title) so it's available to keyboard and
 * screen-reader users navigating a native <select>. Renders nothing for an
 * unselected or uncategorized model instead of a blank/broken block.
 */
export function ModelCategoryNote({ category }: { category: string | undefined }) {
  const help = category ? MODEL_CATEGORY_HELP[category] : undefined;
  if (!help) return null;
  return (
    <p style={{ fontSize: "0.78em", color: "var(--fg-muted)", margin: 0 }}>
      <strong>{help.label}.</strong> {help.what} {help.when} {help.tradeoffs}
    </p>
  );
}

export function ModelPicker({
  models,
  value,
  onChange,
  disabled = false,
}: {
  models: ModelEntry[];
  value: string | null;
  onChange: (modelId: string) => void;
  disabled?: boolean;
}) {
  // Only conversational models belong in the chat picker. Capability models
  // (image, tts, transcription, embedding, …) are reached through their own
  // surfaces/tools, not selected as a raw chat target.
  const grouped = groupConversationalModels(models);
  const selected = models.find((m) => m.id === value) ?? null;

  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>Model</span>
      <select
        value={value ?? ""}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        style={{ padding: "8px 10px", minWidth: 220 }}
        // Pin the accessible name to "Model" explicitly: the category note
        // below renders inside this same wrapping <label>, and without this
        // the select's name-from-content would absorb that note's text too.
        aria-label="Model"
      >
        <option value="" disabled>
          Select a model…
        </option>
        {Object.entries(grouped).map(([category, entries]) => (
          <optgroup key={category} label={category}>
            {entries.map((m) => (
              <option key={m.id} value={m.id}>
                {m.displayName}
                {m.contextWindow != null
                  ? ` — ${formatContextWindow(m.contextWindow)} ctx`
                  : ""}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
      <ModelCategoryNote category={selected?.category} />
    </label>
  );
}
