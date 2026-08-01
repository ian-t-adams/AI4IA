"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { BackgroundConfig } from "@/lib/types";

export type ThemeName = "light" | "dark" | "contrast";

export interface PresetBackground {
  id: string;
  label: string;
  css: string;
}

// Curated gradient presets (sci-fi / cyberpunk leaning, per the desk theme).
export const BACKGROUND_PRESETS: PresetBackground[] = [
  {
    id: "cyberpunk-orange",
    label: "Cyberpunk Orange",
    css: "linear-gradient(135deg, #160a00 0%, #2b1400 45%, #ff7a18 135%)",
  },
  {
    id: "synthwave",
    label: "Synthwave",
    css: "linear-gradient(160deg, #1a0033 0%, #3d0a5e 45%, #ff2d95 115%)",
  },
  {
    id: "deep-space",
    label: "Deep Space",
    css: "radial-gradient(circle at 30% 20%, #14264f 0%, #060912 70%)",
  },
  {
    id: "matrix",
    label: "Matrix",
    css: "linear-gradient(180deg, #001b00 0%, #00120a 60%, #001b00 100%)",
  },
];

interface ThemeState {
  theme: ThemeName;
  fontScale: number;
  accent: string | null;
  background: BackgroundConfig | null;
  backgroundDim: number;
  setTheme: (t: ThemeName) => void;
  setFontScale: (s: number) => void;
  setAccent: (c: string | null) => void;
  setBackground: (b: BackgroundConfig | null) => void;
  setBackgroundDim: (d: number) => void;
}

// `accent: null` means "use the stylesheet's brand accent". That has to be the
// default: the brand orange is deliberately a DIFFERENT hex per theme (#b4400f in
// the light theme, so it stays legible as link text on white; #fb923c in the dark
// theme, so it stays legible on near-black), and one inline hex cannot serve both.
const DEFAULTS = { theme: "light" as ThemeName, fontScale: 1, accent: null };
const STORAGE_KEY = "ai4ia-theme";
const MAX_DIM = 0.7;
// A generated background larger than this (data-URL chars) is kept for the
// session but NOT persisted, to stay well under the localStorage quota.
const MAX_PERSIST_BG_CHARS = 1_800_000;

const ThemeContext = createContext<ThemeState | null>(null);

// Pick the legible foreground for an arbitrary accent.
//
// This has to be derived, not fixed per theme. The stylesheet's --accent-fg is
// correct for the brand accent it ships beside (white on the light theme's dark
// orange, near-black on the dark theme's bright orange), but a user-chosen
// accent inverts that half the time: measured against the shipped swatches, a
// hardcoded --accent-fg failed WCAG AA on 5 of 6 in dark and on the orange in
// light -- e.g. white on #f97316 is 2.8:1. Choosing per accent keeps every
// combination >= 4.5:1 because black and white are the extremes of the scale.
function readableForeground(accent: string): string {
  const hex = accent.replace("#", "");
  const channels = [0, 2, 4].map((i) => {
    const c = Number.parseInt(hex.slice(i, i + 2), 16) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  });
  const luminance =
    0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2];
  // Contrast against white is (1.05)/(L+0.05); against black it is (L+0.05)/0.05.
  // They cross at L = sqrt(0.05 * 1.05) - 0.05 ~= 0.1791.
  return luminance > 0.1791 ? "#000000" : "#ffffff";
}

function clampDim(d: number): number {
  if (Number.isNaN(d)) return 0;
  return Math.min(MAX_DIM, Math.max(0, d));
}

// The pre-rebrand default accent. It was written to localStorage automatically on
// first render rather than chosen, so on hydration it is treated as "no choice"
// and dropped -- otherwise every existing user would stay on the old indigo and
// never see the brand accent. A user who genuinely wants indigo can re-pick it.
const LEGACY_DEFAULT_ACCENT = "#4f46e5";

// Validate a hydrated accent to a known-good shape (untrusted localStorage): it
// is written straight into a CSS custom property, so only accept a literal hex.
function sanitizeAccent(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const hex = value.trim().toLowerCase();
  if (!/^#[0-9a-f]{6}$/.test(hex)) return null;
  return hex === LEGACY_DEFAULT_ACCENT ? null : hex;
}

// Validate a hydrated background to a known-good shape (untrusted localStorage).
function sanitizeBackground(value: unknown): BackgroundConfig | null {
  if (!value || typeof value !== "object") return null;
  const v = value as Record<string, unknown>;
  if (v.kind === "preset" && typeof v.id === "string") {
    const id = v.id;
    return BACKGROUND_PRESETS.some((p) => p.id === id)
      ? { kind: "preset", id }
      : null;
  }
  if (
    v.kind === "generated" &&
    typeof v.dataUrl === "string" &&
    v.dataUrl.startsWith("data:image/png;base64,") &&
    v.dataUrl.length <= MAX_PERSIST_BG_CHARS
  ) {
    return { kind: "generated", dataUrl: v.dataUrl };
  }
  return null;
}

// Compose the final CSS `background-image` value, layering a dark overlay (for
// legibility) on top of the chosen preset gradient or generated image.
function backgroundImageValue(
  bg: BackgroundConfig | null,
  dim: number,
): string | null {
  if (!bg) return null;
  let base: string;
  if (bg.kind === "preset") {
    const preset = BACKGROUND_PRESETS.find((p) => p.id === bg.id);
    if (!preset) return null;
    base = preset.css;
  } else {
    base = `url("${bg.dataUrl}")`;
  }
  if (dim > 0) {
    const overlay = `linear-gradient(rgba(0,0,0,${dim}), rgba(0,0,0,${dim}))`;
    return `${overlay}, ${base}`;
  }
  return base;
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(DEFAULTS.theme);
  const [fontScale, setFontScaleState] = useState<number>(DEFAULTS.fontScale);
  const [accent, setAccentState] = useState<string | null>(DEFAULTS.accent);
  const [background, setBackgroundState] = useState<BackgroundConfig | null>(null);
  const [backgroundDim, setBackgroundDimState] = useState<number>(0.35);
  // Debounce timer for localStorage writes (a generated background can be ~MBs;
  // dragging the dim slider must not stringify + write it synchronously each tick).
  const persistTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Hydrate persisted preferences once on mount.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time hydration of persisted prefs from localStorage on mount; localStorage isn't readable during SSR render, so this can't be a lazy useState initializer
        if (saved.theme) setThemeState(saved.theme);
        if (saved.fontScale) setFontScaleState(saved.fontScale);
        const savedAccent = sanitizeAccent(saved.accent);
        if (savedAccent) setAccentState(savedAccent);
        const bg = sanitizeBackground(saved.background);
        if (bg) setBackgroundState(bg);
        if (typeof saved.backgroundDim === "number")
          setBackgroundDimState(clampDim(saved.backgroundDim));
      }
    } catch {
      /* ignore corrupt storage */
    }
  }, []);

  // Reflect state onto <html> + persist.
  useEffect(() => {
    const root = document.documentElement;
    root.setAttribute("data-theme", theme);
    root.style.setProperty("--font-scale", String(fontScale));
    // Accent is honored only outside the high-contrast theme, and only when the
    // user has actually picked one -- otherwise every var is removed so the
    // stylesheet's per-theme brand accent (and its matching foreground) wins.
    if (theme !== "contrast" && accent) {
      const accentFg = readableForeground(accent);
      root.style.setProperty("--accent", accent);
      root.style.setProperty("--user-bubble", accent);
      root.style.setProperty("--accent-fg", accentFg);
      root.style.setProperty("--user-bubble-fg", accentFg);
    } else {
      root.style.removeProperty("--accent");
      root.style.removeProperty("--user-bubble");
      root.style.removeProperty("--accent-fg");
      root.style.removeProperty("--user-bubble-fg");
    }
    // Custom background is disabled entirely in high-contrast (a11y floor). When
    // there is no background we REMOVE the inline var so the stylesheet's
    // `none` default wins rather than overriding it inline.
    const bgValue =
      theme === "contrast" ? null : backgroundImageValue(background, backgroundDim);
    if (bgValue) root.style.setProperty("--app-bg-image", bgValue);
    else root.style.removeProperty("--app-bg-image");

    // Debounce the (potentially large) persistence write off the render path.
    if (persistTimer.current) clearTimeout(persistTimer.current);
    persistTimer.current = setTimeout(() => {
      try {
        // Never persist an oversized generated image (quota safety): keep it for
        // the session only, falling back to no saved background on reload.
        const persistBg =
          background?.kind === "generated" &&
          background.dataUrl.length > MAX_PERSIST_BG_CHARS
            ? null
            : background;
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({
            theme,
            fontScale,
            accent,
            background: persistBg,
            backgroundDim,
          }),
        );
      } catch {
        /* ignore (e.g. quota) */
      }
    }, 300);
  }, [theme, fontScale, accent, background, backgroundDim]);

  // Flush any pending persistence on unmount.
  useEffect(() => {
    return () => {
      if (persistTimer.current) clearTimeout(persistTimer.current);
    };
  }, []);

  const setTheme = useCallback((t: ThemeName) => setThemeState(t), []);
  const setFontScale = useCallback(
    (s: number) => setFontScaleState(Math.min(1.6, Math.max(0.8, s))),
    [],
  );
  const setAccent = useCallback((c: string | null) => setAccentState(c), []);
  const setBackground = useCallback(
    (b: BackgroundConfig | null) => setBackgroundState(b),
    [],
  );
  const setBackgroundDim = useCallback(
    (d: number) => setBackgroundDimState(clampDim(d)),
    [],
  );

  const value = useMemo(
    () => ({
      theme,
      fontScale,
      accent,
      background,
      backgroundDim,
      setTheme,
      setFontScale,
      setAccent,
      setBackground,
      setBackgroundDim,
    }),
    [
      theme,
      fontScale,
      accent,
      background,
      backgroundDim,
      setTheme,
      setFontScale,
      setAccent,
      setBackground,
      setBackgroundDim,
    ],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}
