"use client";

import { useMemo, useState } from "react";
import * as api from "@/lib/api";
import type { ModelEntry } from "@/lib/types";
import { BACKGROUND_PRESETS, useTheme } from "./ThemeProvider";

const SIZES = ["1024x1024", "1024x1536", "1536x1024", "auto"];

const swatchCss: Record<string, string> = Object.fromEntries(
  BACKGROUND_PRESETS.map((p) => [p.id, p.css]),
);

export function ImageryPanel({ models }: { models: ModelEntry[] }) {
  const {
    theme,
    background,
    backgroundDim,
    setBackground,
    setBackgroundDim,
  } = useTheme();

  const imageModels = useMemo(
    () => models.filter((m) => m.category === "image"),
    [models],
  );

  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState<string>("");
  const [size, setSize] = useState<string>("1024x1024");
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null); // data URL

  const disabled = theme === "contrast";
  const effectiveModel = model || imageModels[0]?.id || "";

  const generate = async () => {
    if (!prompt.trim() || generating) return;
    setGenerating(true);
    setGenError(null);
    setPreview(null);
    try {
      const resp = await api.generateImage({
        prompt: prompt.trim(),
        model: effectiveModel || undefined,
        size,
      });
      const b64 = resp.images[0]?.b64;
      if (!b64) {
        setGenError("No image was returned.");
        return;
      }
      setPreview(`data:image/png;base64,${b64}`);
    } catch (e) {
      setGenError((e as Error).message);
    } finally {
      setGenerating(false);
    }
  };

  return (
    <fieldset
      style={{
        border: "none",
        margin: 0,
        padding: 0,
        display: "flex",
        flexDirection: "column",
        gap: 12,
      }}
      disabled={disabled}
    >
      <legend style={{ fontSize: "0.85em", color: "var(--fg-muted)", marginBottom: 4 }}>
        Background
        {disabled && " (disabled in high contrast)"}
      </legend>

      {/* Preset gradients + a None option. */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
        <button
          type="button"
          onClick={() => setBackground(null)}
          aria-pressed={background === null}
          aria-label="No background"
          title="None"
          style={{
            width: 44,
            height: 32,
            borderRadius: 8,
            background: "var(--bg)",
            color: "var(--fg-muted)",
            fontSize: "0.7em",
            border:
              background === null ? "3px solid var(--fg)" : "1px solid var(--border)",
          }}
        >
          None
        </button>
        {BACKGROUND_PRESETS.map((p) => {
          const active = background?.kind === "preset" && background.id === p.id;
          return (
            <button
              key={p.id}
              type="button"
              onClick={() => setBackground({ kind: "preset", id: p.id })}
              aria-pressed={active}
              aria-label={p.label}
              title={p.label}
              style={{
                width: 44,
                height: 32,
                borderRadius: 8,
                backgroundImage: swatchCss[p.id],
                backgroundSize: "cover",
                border: active ? "3px solid var(--fg)" : "1px solid var(--border)",
              }}
            />
          );
        })}
      </div>

      {/* Dim overlay for legibility. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
        <label htmlFor="bg-dim" style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
          Background dim: {Math.round(backgroundDim * 100)}%
        </label>
        <input
          id="bg-dim"
          type="range"
          min={0}
          max={0.7}
          step={0.05}
          value={backgroundDim}
          onChange={(e) => setBackgroundDim(Number(e.target.value))}
        />
      </div>

      {/* AI image generation. */}
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
          Generate a background
        </span>
        {imageModels.length === 0 ? (
          <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
            No image models available.
          </span>
        ) : (
          <>
            <textarea
              aria-label="Image prompt"
              placeholder="e.g. an orange neon cyberpunk skyline, dark, cinematic"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              rows={2}
              maxLength={4000}
              style={{ padding: 8, resize: "vertical" }}
            />
            <div style={{ display: "flex", gap: 8 }}>
              <select
                aria-label="Image model"
                value={effectiveModel}
                onChange={(e) => setModel(e.target.value)}
                style={{ flex: 1, padding: 6 }}
              >
                {imageModels.map((m) => (
                  <option key={m.id} value={m.id}>
                    {m.displayName}
                  </option>
                ))}
              </select>
              <select
                aria-label="Image size"
                value={size}
                onChange={(e) => setSize(e.target.value)}
                style={{ padding: 6 }}
              >
                {SIZES.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </div>
            <button
              type="button"
              onClick={generate}
              disabled={generating || !prompt.trim()}
              style={{
                padding: "8px 14px",
                borderRadius: 8,
                border: "none",
                background: "var(--accent)",
                color: "var(--accent-fg)",
                fontWeight: 600,
                opacity: generating || !prompt.trim() ? 0.6 : 1,
              }}
            >
              {generating ? "Generating…" : "Generate"}
            </button>
          </>
        )}

        {genError && (
          <span role="alert" style={{ fontSize: "0.8em", color: "var(--danger)" }}>
            {genError}
          </span>
        )}

        {preview && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {/* eslint-disable-next-line @next/next/no-img-element */}
            <img
              src={preview}
              alt="Generated background preview"
              style={{
                width: "100%",
                borderRadius: 8,
                border: "1px solid var(--border)",
              }}
            />
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <button
                type="button"
                onClick={() =>
                  setBackground({ kind: "generated", dataUrl: preview })
                }
                style={{
                  padding: "6px 12px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                  color: "var(--fg)",
                }}
              >
                Use as background
              </button>
              <a
                href={preview}
                download="ai4ia-background.png"
                style={{
                  padding: "6px 12px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                  color: "var(--fg)",
                  textDecoration: "none",
                }}
              >
                Download
              </a>
              <button
                type="button"
                onClick={() => setPreview(null)}
                style={{
                  padding: "6px 12px",
                  borderRadius: 8,
                  border: "1px solid var(--border)",
                  background: "var(--bg)",
                  color: "var(--fg-muted)",
                }}
              >
                Clear
              </button>
            </div>
            <span style={{ fontSize: "0.72em", color: "var(--fg-muted)" }}>
              Large backgrounds are kept for this session only — download to keep.
            </span>
          </div>
        )}
      </div>
    </fieldset>
  );
}
