"use client";

import { useId } from "react";

import { DOCS_INDEX_URL, STATUS_URL, USER_GUIDE_URL } from "@/lib/docs";
import { Icon } from "./Icon";
import { ThemeName, useTheme } from "./ThemeProvider";

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

export function AppearanceSettings() {
  const { theme, setTheme, fontScale, setFontScale, accent, setAccent } = useTheme();
  const fontScaleId = useId();

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <fieldset style={{ border: "none", margin: 0, padding: 0 }}>
        <legend style={{ fontSize: "0.875rem", fontWeight: 600, marginBottom: 8 }}>Theme</legend>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {THEMES.map((t) => (
            <button
              key={t.id}
              type="button"
              className="btn"
              onClick={() => setTheme(t.id)}
              aria-pressed={theme === t.id}
              style={
                theme === t.id
                  ? { borderColor: "var(--accent)", boxShadow: "inset 0 0 0 1px var(--accent)" }
                  : undefined
              }
            >
              {t.label}
            </button>
          ))}
        </div>
      </fieldset>

      <div style={{ display: "flex", flexDirection: "column", gap: 6, maxWidth: 420 }}>
        <label htmlFor={fontScaleId} style={{ fontSize: "0.875rem", fontWeight: 600 }}>
          Text size: {Math.round(fontScale * 100)}%
        </label>
        <input
          id={fontScaleId}
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
          <p style={{ margin: 0, fontSize: "0.875rem", color: "var(--fg-muted)", maxWidth: "65ch" }}>
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
          <legend style={{ fontSize: "0.875rem", fontWeight: 600, marginBottom: 8 }}>
            Accent color
          </legend>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {ACCENTS.map((c) => (
              <button
                key={c.value ?? "brand"}
                type="button"
                onClick={() => setAccent(c.value)}
                aria-label={`Accent ${c.label}`}
                aria-pressed={accent === c.value}
                title={c.label}
                style={{
                  width: 36,
                  height: 36,
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
    </div>
  );
}

function SettingsSection({
  title,
  intro,
  children,
}: {
  title: string;
  intro: string;
  children: React.ReactNode;
}) {
  const headingId = useId();
  return (
    <section className="settings-section" aria-labelledby={headingId}>
      <div className="settings-section-intro">
        <h2 id={headingId}>{title}</h2>
        <p>{intro}</p>
      </div>
      <div className="settings-section-body">{children}</div>
    </section>
  );
}

// Personal preferences and account housekeeping. Conversation configuration
// (model, instructions, tools, voice) stays in the conversation inspector, where
// it applies; this page holds what is not tied to one conversation.
export function SettingsPage({
  onOpenDeletionStatus,
}: {
  onOpenDeletionStatus: () => void;
}) {
  return (
    <>
      <SettingsSection
        title="Appearance & accessibility"
        intro="Theme, text size and accent apply on this device. High contrast uses fixed, tested colors."
      >
        <AppearanceSettings />
      </SettingsSection>
      <SettingsSection
        title="Data & privacy"
        intro="Removing a conversation takes it off your list. That alone doesn't mean its stored data has been erased."
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 10, alignItems: "flex-start" }}>
          <p style={{ margin: 0, maxWidth: "65ch" }}>
            Deletion status shows the last observed progress of each removal request. It is not
            proof of erasure, there is no automatic cleanup, and opening it only reads status.
          </p>
          <button type="button" className="btn" onClick={onOpenDeletionStatus}>
            Deletion status
          </button>
        </div>
      </SettingsSection>
      <SettingsSection
        title="Help & resources"
        intro="Guides for using AI4IA, its documentation hub and live service status."
      >
        <ul className="settings-links">
          <li>
            <a href={USER_GUIDE_URL} target="_blank" rel="noopener noreferrer">
              <Icon name="help" size={18} />
              User guide
              <span className="visually-hidden"> (opens in a new tab)</span>
            </a>
          </li>
          <li>
            <a href={DOCS_INDEX_URL} target="_blank" rel="noopener noreferrer">
              <Icon name="docs" size={18} />
              Documentation
              <span className="visually-hidden"> (opens in a new tab)</span>
            </a>
          </li>
          <li>
            <a href={STATUS_URL} target="_blank" rel="noopener noreferrer">
              <Icon name="status" size={18} />
              Deployment status
              <span className="visually-hidden"> (opens in a new tab)</span>
            </a>
          </li>
        </ul>
      </SettingsSection>
    </>
  );
}
