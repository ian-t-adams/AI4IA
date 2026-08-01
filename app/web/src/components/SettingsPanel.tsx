"use client";

import type { ModelEntry } from "@/lib/types";
import { ImageryPanel } from "./ImageryPanel";
import { ThemeName, useTheme } from "./ThemeProvider";
import { useModalFocus, useModalKeyDown } from "./useModalFocus";

const THEMES: { id: ThemeName; label: string }[] = [
  { id: "light", label: "Light" },
  { id: "dark", label: "Dark" },
  { id: "contrast", label: "High contrast" },
];

// `null` is the brand accent from globals.css, which is theme-aware; the rest are
// deliberate overrides. Each is paired with a foreground derived at apply time
// (see readableForeground in ThemeProvider), so a swatch that needs black text on
// one theme and white on another stays legible instead of relying on a fixed
// --accent-fg that is only ever right for half of them.
const ACCENTS: { value: string | null; label: string; swatch: string }[] = [
  { value: null, label: "Brand orange (default)", swatch: "var(--brand)" },
  { value: "#1d4ed8", label: "Blue", swatch: "#1d4ed8" },
  { value: "#0e7490", label: "Teal", swatch: "#0e7490" },
  { value: "#b91c1c", label: "Red", swatch: "#b91c1c" },
  { value: "#15803d", label: "Green", swatch: "#15803d" },
  { value: "#a21caf", label: "Magenta", swatch: "#a21caf" },
];

export function SettingsPanel({
  models,
  onClose,
}: {
  models: ModelEntry[];
  onClose: () => void;
}) {
  const { theme, setTheme, fontScale, setFontScale, accent, setAccent } =
    useTheme();
  const modalRef = useModalFocus();
  const onModalKeyDown = useModalKeyDown(onClose);

  return (
    <div
      ref={modalRef}
      onKeyDown={onModalKeyDown}
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

        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {theme === "contrast" && (
            // Kept outside the disabled fieldset below: the fieldset's reduced
            // opacity is appropriate for controls the user can't act on, but it
            // would also wash out the very explanation that tells them why --
            // the one piece of text here that most needs full contrast.
            <p style={{ margin: 0, fontSize: "0.78em", color: "var(--fg-muted)" }}>
              Disabled while High contrast is active — that theme uses its own
              fixed, tested colors to guarantee readability, so a custom accent
              can&apos;t be applied on top of it. Switch to Light or Dark to
              pick a color.
            </p>
          )}
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
                  key={c.value ?? "brand"}
                  onClick={() => setAccent(c.value)}
                  aria-label={`Accent ${c.label}`}
                  aria-pressed={accent === c.value}
                  title={c.label}
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: "50%",
                    background: c.swatch,
                    border:
                      accent === c.value
                        ? "3px solid var(--fg)"
                        : "1px solid var(--border)",
                  }}
                />
              ))}
            </div>
          </fieldset>
        </div>

        <ImageryPanel models={models} />
      </div>
    </div>
  );
}
