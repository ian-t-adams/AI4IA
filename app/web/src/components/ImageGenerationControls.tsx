"use client";

import { useEffect, useMemo, useState } from "react";

import * as api from "@/lib/api";
import type {
  ImageGenerationPreferences,
  ImageModelOption,
  ImageOptionsResponse,
} from "@/lib/types";

function intersection(values: string[][]): string[] {
  if (values.length === 0) return [];
  return values[0].filter((value) =>
    values.slice(1).every((candidate) => candidate.includes(value)),
  );
}

function samePreferences(
  left: ImageGenerationPreferences | null,
  right: ImageGenerationPreferences,
): boolean {
  return (
    left !== null &&
    left.size === right.size &&
    left.quality === right.quality &&
    left.models.length === right.models.length &&
    left.models.every((model, index) => model === right.models[index])
  );
}

function formatCost(value: number): string {
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(3)}`;
}

export function ImageGenerationControls({
  preferences,
  disabled,
  onSave,
  onReset,
  onStart,
}: {
  preferences: ImageGenerationPreferences | null;
  disabled: boolean;
  onSave: (value: ImageGenerationPreferences) => void;
  onReset: () => void;
  onStart: () => void;
}) {
  const [options, setOptions] = useState<ImageOptionsResponse | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draftOverride, setDraftOverride] =
    useState<ImageGenerationPreferences | null>(null);
  const current = draftOverride ??
    preferences ?? { models: [], size: null, quality: null };
  const selected = current.models;
  const size = current.size;
  const quality = current.quality;

  useEffect(() => {
    let active = true;
    void api.getImageOptions().then(
      (value) => {
        if (active) setOptions(value);
      },
      (reason: unknown) => {
        if (active) setLoadError(api.apiErrorDetail(reason));
      },
    );
    return () => {
      active = false;
    };
  }, []);

  const selectedOptions = useMemo(
    () =>
      selected
        .map((id) => options?.models.find((model) => model.id === id))
        .filter((model): model is ImageModelOption => model !== undefined),
    [options, selected],
  );
  const commonSizes = useMemo(
    () => intersection(selectedOptions.map((model) => model.sizes)),
    [selectedOptions],
  );
  const commonQualities = useMemo(
    () => intersection(selectedOptions.map((model) => model.qualities)),
    [selectedOptions],
  );
  const resolvedSize = size && commonSizes.includes(size) ? size : commonSizes[0] ?? null;
  const resolvedQuality =
    quality && commonQualities.includes(quality)
      ? quality
      : commonQualities[0] ?? null;
  const draft: ImageGenerationPreferences = {
    models: selected,
    size: resolvedSize,
    quality: resolvedQuality,
  };
  const saved = samePreferences(preferences, draft);
  const selectedPrices = selectedOptions.map((model) =>
    model.prices.find(
      (candidate) =>
        candidate.size === resolvedSize &&
        candidate.quality === resolvedQuality,
    ),
  );
  const knownTotal = selectedPrices.reduce(
    (total, price) =>
      total +
      (price?.costKnown && price.estimatedCostUsd !== null
        ? price.estimatedCostUsd
        : 0),
    0,
  );
  const unknownPrices = selectedPrices.filter(
    (price) => !price?.costKnown || price.estimatedCostUsd === null,
  ).length;

  const toggleModel = (id: string, checked: boolean) => {
    setDraftOverride((override) => {
      const source = override ?? current;
      if (!checked) {
        return {
          ...source,
          models: source.models.filter((model) => model !== id),
        };
      }
      if (
        source.models.includes(id) ||
        source.models.length >= (options?.maxSelectedModels ?? 3)
      ) {
        return source;
      }
      return { ...source, models: [...source.models, id] };
    });
  };

  if (loadError) {
    return <p className="inspector-error" role="alert">{loadError}</p>;
  }
  if (!options) {
    return <p className="inspector-empty">Loading image models…</p>;
  }

  return (
    <div className="image-preferences">
      <div className="tool-list-header">
        <span>Image generation</span>
        <small>Choose 1–{options.maxSelectedModels} models</small>
      </div>
      <p className="inspector-note">
        One prompt can generate a single image or a side-by-side comparison.
        You can change this selection at any point in the conversation.
      </p>
      <fieldset className="image-model-options" disabled={disabled}>
        <legend className="sr-only">Image models</legend>
        {options.models.map((model) => {
          const checked = selected.includes(model.id);
          const atLimit =
            !checked && selected.length >= options.maxSelectedModels;
          const price = model.prices.find(
            (candidate) =>
              candidate.size === resolvedSize &&
              candidate.quality === resolvedQuality,
          );
          return (
            <label key={model.id} className="image-model-option">
              <input
                type="checkbox"
                checked={checked}
                disabled={disabled || atLimit}
                onChange={(event) => toggleModel(model.id, event.target.checked)}
              />
              <span>
                <strong>{model.displayName}</strong>
                <small>
                  {price?.costKnown && price.estimatedCostUsd !== null
                    ? `Estimated ${formatCost(price.estimatedCostUsd)} per image`
                    : "Cost estimate unavailable"}
                </small>
              </span>
            </label>
          );
        })}
      </fieldset>
      {selected.length > 0 ? (
        <div className="image-output-options">
          <label>
            Output size
            <select
              value={resolvedSize ?? ""}
              disabled={disabled || commonSizes.length === 0}
              onChange={(event) =>
                setDraftOverride({
                  ...draft,
                  size: event.target.value || null,
                })
              }
            >
              {commonSizes.map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </label>
          <label>
            Quality
            <select
              value={resolvedQuality ?? ""}
              disabled={disabled || commonQualities.length === 0}
              onChange={(event) =>
                setDraftOverride({
                  ...draft,
                  quality: event.target.value || null,
                })
              }
            >
              {commonQualities.map((value) => (
                <option key={value} value={value}>{value}</option>
              ))}
            </select>
          </label>
        </div>
      ) : null}
      {selected.length > 0 &&
      (commonSizes.length === 0 || commonQualities.length === 0) ? (
        <p className="inspector-error" role="alert">
          Those models do not share a compatible size and quality.
        </p>
      ) : null}
      {selected.length > 0 && resolvedSize && resolvedQuality ? (
        <p className="inspector-note">
          {unknownPrices === 0
            ? `Estimated run total: ${formatCost(knownTotal)}`
            : knownTotal > 0
              ? `Known subtotal: ${formatCost(knownTotal)}; ${unknownPrices} model cost estimate unavailable`
              : `Cost estimate unavailable for ${unknownPrices === 1 ? "this model" : `all ${unknownPrices} models`}`}
        </p>
      ) : null}
      <p className="inspector-note">
        Estimates are directional, not bills. “Unavailable” is intentional when
        Azure does not publish an unambiguous retail meter.
      </p>
      <div className="inspector-actions">
        <button
          type="button"
          disabled={
            disabled ||
            selected.length === 0 ||
            !resolvedSize ||
            !resolvedQuality ||
            saved
          }
          onClick={() => onSave(draft)}
        >
          Save image setup
        </button>
        <button
          type="button"
          disabled={disabled || !saved || selected.length === 0}
          onClick={onStart}
        >
          {selected.length > 1 ? "Start comparison in chat" : "Start image in chat"}
        </button>
        {preferences && preferences.models.length > 0 ? (
          <button
            type="button"
            disabled={disabled}
            onClick={() => {
              setDraftOverride(null);
              onReset();
            }}
          >
            Use automatic
          </button>
        ) : null}
      </div>
      {!saved && selected.length > 0 ? (
        <small className="inspector-note">Save this setup before starting.</small>
      ) : null}
    </div>
  );
}
