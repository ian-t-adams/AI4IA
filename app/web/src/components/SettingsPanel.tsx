"use client";

import type { ModelEntry } from "@/lib/types";
import { ImageryPanel } from "./ImageryPanel";
import { ThemeName, useTheme } from "./ThemeProvider";

const THEMES: { id: ThemeName; label: string }[] = [
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
  { id: "contrast", label: "High contrast" },
];

const ACCENTS = ["#f97316", "#4f46e5", "#0e7490", "#b91c1c", "#15803d", "#a21caf"];

export function SettingsPanel({
  models,
  onClose,
}: {
  models: ModelEntry[];
  onClose: () => void;
}) {
  const { theme, setTheme, fontScale, setFontScale, accent, setAccent } =
    useTheme();

  return (
    <div
      role="dialog"
      aria-label="Appearance and accessibility settings"
      aria-modal="true"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 50,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(440px, 92vw)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 20,
          maxHeight: "90vh",
          overflowY: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between" }}>
          <h2 style={{ margin: 0, fontSize: "1.2em" }}>Appearance &amp; accessibility</h2>
          <button
            onClick={onClose}
            aria-label="Close settings"
            style={{ border: "none", background: "transparent", color: "var(--fg)", fontSize: "1.2em" }}
          >
            ✕
          </button>
        </div>

        <fieldset style={{ border: "none", margin: 0, padding: 0 }}>
          <legend style={{ fontSize: "0.85em", color: "var(--fg-muted)", marginBottom: 8 }}>
            Theme
          </legend>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
            {THEMES.map((t) => (
              <button
                key={t.id}
                onClick={() => setTheme(t.id)}
                aria-pressed={theme === t.id}
                style={{
                  padding: "8px 14px",
                  borderRadius: 8,
                  border:
                    theme === t.id
                      ? "2px solid var(--accent)"
                      : "1px solid var(--border)",
                  background: "var(--bg)",
                  color: "var(--fg)",
                  fontWeight: theme === t.id ? 700 : 400,
                }}
              >
                {t.label}
              </button>
            ))}
          </div>
        </fieldset>

        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <label htmlFor="font-scale" style={{ fontSize: "0.85em", color: "var(--fg-muted)" }}>
            Text size: {Math.round(fontScale * 100)}%
          </label>
          <input
            id="font-scale"
            type="range"
            min={0.8}
            max={1.6}
            step={0.1}
            value={fontScale}
            onChange={(e) => setFontScale(Number(e.target.value))}
          />
        </div>

        <fieldset
          style={{ border: "none", margin: 0, padding: 0, opacity: theme === "contrast" ? 0.4 : 1 }}
          disabled={theme === "contrast"}
        >
          <legend style={{ fontSize: "0.85em", color: "var(--fg-muted)", marginBottom: 8 }}>
            Accent color
          </legend>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {ACCENTS.map((c) => (
              <button
                key={c}
                onClick={() => setAccent(c)}
                aria-label={`Accent ${c}`}
                aria-pressed={accent === c}
                style={{
                  width: 32,
                  height: 32,
                  borderRadius: "50%",
                  background: c,
                  border: accent === c ? "3px solid var(--fg)" : "1px solid var(--border)",
                }}
              />
            ))}
          </div>
        </fieldset>

        <ImageryPanel models={models} />
      </div>
    </div>
  );
}
