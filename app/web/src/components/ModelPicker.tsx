"use client";

import { useId } from "react";
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
 * unselected or uncategorized model instead of a blank/broken block. Accepts
 * an `id` so a sibling <select> can wire `aria-describedby` to it — omit the
 * id (or don't render this at all) rather than pointing at one that isn't
 * there, which would leave a dangling, invalid IDREF.
 */
export function ModelCategoryNote({
  category,
  id,
}: {
  category: string | undefined;
  id?: string;
}) {
  const help = category ? MODEL_CATEGORY_HELP[category] : undefined;
  if (!help) return null;
  return (
    <p id={id} style={{ fontSize: "0.78em", color: "var(--fg-muted)", margin: 0 }}>
      <strong>{help.label}.</strong> {help.what} {help.when} {help.tradeoffs}
    </p>
  );
}

/**
 * Where the selected model will actually process this conversation.
 *
 * Shown at the point of model selection because that is where the decision is
 * made. It reports the residency the SERVER derived from each deployment's SKU,
 * not the endpoint's geography: a GlobalStandard deployment in a Swedish region
 * is reachable from the EU but may be processed anywhere, and calling that "EU"
 * would assert a guarantee Azure is not making.
 *
 * A model can carry more than one eligible deployment (e.g. under the `zonal`
 * policy, one in each zone), in which case both are named rather than picking
 * one — the user needs to know the set they might land in, and the usage ledger
 * records which one actually served.
 *
 * Renders nothing when there is no selection or no deployment metadata, rather
 * than guessing.
 */
export function ModelResidencyNote({
  model,
  id,
}: {
  model: ModelEntry | null;
  id?: string;
}) {
  if (!model || model.options.length === 0) return null;

  const zones = [...new Set(model.options.map((o) => o.residency))].sort();
  if (zones.length === 0) return null;

  const label = (zone: string) =>
    zone === "us" ? "US" : zone === "eu" ? "EU" : zone;

  const text = zones.includes("global")
    ? "May process in any Azure region worldwide."
    : zones.length === 1
      ? `Processing stays in the ${label(zones[0])} data zone.`
      : `Processing stays in the ${zones.map(label).join(" or ")} data zone.`;

  return (
    <p id={id} style={{ fontSize: "0.78em", color: "var(--fg-muted)", margin: 0 }}>
      <strong>Data residency.</strong> {text}
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
  const noteId = useId();
  const residencyId = useId();
  const capabilityId = useId();
  const categoryHelp = selected?.category ? MODEL_CATEGORY_HELP[selected.category] : undefined;
  const hasResidency = (selected?.options.length ?? 0) > 0;
  const hasToolLimitation = selected?.supportsTools === false;
  const describedBy =
    [
      categoryHelp ? noteId : null,
      hasResidency ? residencyId : null,
      hasToolLimitation ? capabilityId : null,
    ]
      .filter(Boolean)
      .join(" ") || undefined;

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
        aria-describedby={describedBy}
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
      <ModelCategoryNote id={noteId} category={selected?.category} />
      <ModelResidencyNote id={residencyId} model={selected} />
      {hasToolLimitation ? (
        <small id={capabilityId} style={{ color: "var(--warn)" }}>
          Plain chat only. This model cannot run agent or workflow tools
          {selected?.inputModalities?.length === 1 &&
          selected.inputModalities[0] === "text"
            ? " and accepts text input only"
            : ""}
          . Parse files through the Library before using them here.
        </small>
      ) : null}
    </label>
  );
}
