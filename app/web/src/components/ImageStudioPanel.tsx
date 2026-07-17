"use client";

import { useEffect, useMemo, useState } from "react";
import * as api from "@/lib/api";
import type { ModelEntry } from "@/lib/types";
import { useTheme } from "./ThemeProvider";
import { useModalFocus } from "./useModalFocus";

const SIZES = ["1024x1024", "1024x1536", "1536x1024", "auto"];
const QUALITIES = ["auto", "low", "medium", "high"];

type GalleryItem = {
  id: string;
  dataUrl: string;
  prompt: string;
  model: string;
  size: string;
  quality: string;
};

// A dedicated media surface for image generation. Unlike the Settings
// "Background" generator (which is scoped to picking a chat backdrop), this is a
// general studio: any image-category model, a prompt, a size, and a session
// gallery of results you can download, reuse as a background, or clear. It
// reuses the same hardened POST /api/images/generations backend (entitlement-
// gated + usage-metered), so nothing here bypasses governance.
export function ImageStudioPanel({
  models,
  onClose,
}: {
  models: ModelEntry[];
  onClose: () => void;
}) {
  const modal = useModalFocus(onClose);
  const { setBackground } = useTheme();

  const imageModels = useMemo(
    () => models.filter((m) => m.category === "image"),
    [models],
  );

  const [prompt, setPrompt] = useState("");
  const [model, setModel] = useState<string>("");
  const [size, setSize] = useState<string>("1024x1024");
  const [quality, setQuality] = useState<string>("auto");
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gallery, setGallery] = useState<GalleryItem[]>([]);

  const effectiveModel = model || imageModels[0]?.id || "";

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const generate = async () => {
    if (!prompt.trim() || generating) return;
    setGenerating(true);
    setError(null);
    try {
      const resp = await api.generateImage({
        prompt: prompt.trim(),
        model: effectiveModel || undefined,
        size,
        quality,
      });
      const b64 = resp.images[0]?.b64;
      if (!b64) {
        setError("No image was returned.");
        return;
      }
      const item: GalleryItem = {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        dataUrl: `data:image/png;base64,${b64}`,
        prompt: prompt.trim(),
        model: resp.model || effectiveModel,
        size: resp.size || size,
        quality: resp.quality || quality,
      };
      setGallery((g) => [item, ...g]);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div
      ref={modal.ref}
      onKeyDown={modal.onKeyDown}
      role="dialog"
      aria-label="Imagery studio"
      aria-modal="true"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 50,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(960px, 95vw)",
          height: "min(720px, 90vh)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", flexDirection: "column" }}>
            <strong style={{ fontSize: "1.05em" }}>🖼 Imagery studio</strong>
            <span style={{ fontSize: "0.78em", color: "var(--fg-muted)" }}>
              Generate images with any available image model.
            </span>
          </div>
          <button
            onClick={onClose}
            aria-label="Close imagery studio"
            style={{ border: "none", background: "transparent", color: "var(--fg)", fontSize: "1.2em", cursor: "pointer" }}
          >
            ✕
          </button>
        </div>

        {imageModels.length === 0 ? (
          <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--fg-muted)" }}>
            No image models are available to your account.
          </div>
        ) : (
          <div style={{ display: "flex", gap: 16, flex: 1, minHeight: 0 }}>
            {/* Controls */}
            <div style={{ width: 300, flexShrink: 0, display: "flex", flexDirection: "column", gap: 10 }}>
              <textarea
                aria-label="Image prompt"
                placeholder="e.g. an orange neon cyberpunk skyline, dark, cinematic"
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
                rows={5}
                maxLength={4000}
                style={{ padding: 8, resize: "vertical" }}
              />
              <label style={{ fontSize: "0.78em", color: "var(--fg-muted)" }}>
                Model
                <select
                  aria-label="Image model"
                  value={effectiveModel}
                  onChange={(e) => setModel(e.target.value)}
                  style={{ width: "100%", padding: 6, marginTop: 4 }}
                >
                  {imageModels.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.displayName}
                    </option>
                  ))}
                </select>
              </label>
              <label style={{ fontSize: "0.78em", color: "var(--fg-muted)" }}>
                Size
                <select
                  aria-label="Image size"
                  value={size}
                  onChange={(e) => setSize(e.target.value)}
                  style={{ width: "100%", padding: 6, marginTop: 4 }}
                >
                  {SIZES.map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </select>
              </label>
              <label style={{ fontSize: "0.78em", color: "var(--fg-muted)" }}>
                Quality
                <select
                  aria-label="Image quality"
                  value={quality}
                  onChange={(e) => setQuality(e.target.value)}
                  style={{ width: "100%", padding: 6, marginTop: 4 }}
                >
                  {QUALITIES.map((q) => (
                    <option key={q} value={q}>
                      {q}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={generate}
                disabled={generating || !prompt.trim()}
                style={{
                  padding: "10px 14px",
                  borderRadius: 8,
                  border: "none",
                  background: "var(--accent)",
                  color: "var(--accent-fg)",
                  fontWeight: 600,
                  opacity: generating || !prompt.trim() ? 0.6 : 1,
                  cursor: generating || !prompt.trim() ? "not-allowed" : "pointer",
                }}
              >
                {generating ? "Generating…" : "Generate"}
              </button>
              {error && (
                <span role="alert" style={{ fontSize: "0.8em", color: "var(--danger)" }}>
                  {error}
                </span>
              )}
              <span style={{ fontSize: "0.72em", color: "var(--fg-muted)" }}>
                Images are kept for this session only — download to keep them.
              </span>
            </div>

            {/* Gallery */}
            <div style={{ flex: 1, minWidth: 0, overflowY: "auto" }}>
              {gallery.length === 0 ? (
                <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--fg-muted)", fontSize: "0.85em" }}>
                  Generated images appear here.
                </div>
              ) : (
                <div
                  style={{
                    display: "grid",
                    gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
                    gap: 12,
                  }}
                >
                  {gallery.map((item) => (
                    <figure
                      key={item.id}
                      style={{
                        margin: 0,
                        border: "1px solid var(--border)",
                        borderRadius: 10,
                        overflow: "hidden",
                        background: "var(--bg)",
                        display: "flex",
                        flexDirection: "column",
                      }}
                    >
                      {/* eslint-disable-next-line @next/next/no-img-element -- session-only data URL; next/image adds no value */}
                      <img
                        src={item.dataUrl}
                        alt={item.prompt}
                        style={{ width: "100%", display: "block", aspectRatio: "1 / 1", objectFit: "cover" }}
                      />
                      <figcaption style={{ padding: 8, display: "flex", flexDirection: "column", gap: 6 }}>
                        <span
                          title={item.prompt}
                          style={{ fontSize: "0.72em", color: "var(--fg-muted)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
                        >
                          {item.prompt}
                        </span>
                        <span style={{ fontSize: "0.66em", color: "var(--fg-muted)" }}>
                          {item.model} · {item.size}
                          {item.quality && item.quality !== "auto"
                            ? ` · ${item.quality}`
                            : ""}
                        </span>
                        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                          <a
                            href={item.dataUrl}
                            download={`ai4ia-${item.id}.png`}
                            style={{
                              padding: "4px 8px",
                              borderRadius: 6,
                              border: "1px solid var(--border)",
                              background: "var(--bg-elevated)",
                              color: "var(--fg)",
                              fontSize: "0.72em",
                              textDecoration: "none",
                            }}
                          >
                            Download
                          </a>
                          <button
                            type="button"
                            onClick={() => setBackground({ kind: "generated", dataUrl: item.dataUrl })}
                            style={{
                              padding: "4px 8px",
                              borderRadius: 6,
                              border: "1px solid var(--border)",
                              background: "var(--bg-elevated)",
                              color: "var(--fg)",
                              fontSize: "0.72em",
                              cursor: "pointer",
                            }}
                          >
                            Use as background
                          </button>
                          <button
                            type="button"
                            onClick={() => setGallery((g) => g.filter((x) => x.id !== item.id))}
                            aria-label="Remove image"
                            style={{
                              padding: "4px 8px",
                              borderRadius: 6,
                              border: "1px solid var(--border)",
                              background: "var(--bg-elevated)",
                              color: "var(--fg-muted)",
                              fontSize: "0.72em",
                              cursor: "pointer",
                            }}
                          >
                            Remove
                          </button>
                        </div>
                      </figcaption>
                    </figure>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
