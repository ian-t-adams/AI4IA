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

export type ThemeName = "light" | "dark" | "contrast";

interface ThemeState {
  theme: ThemeName;
  fontScale: number;
  accent: string | null;
  setTheme: (t: ThemeName) => void;
  setFontScale: (s: number) => void;
  setAccent: (c: string | null) => void;
}

// `accent: null` means "use the stylesheet's brand accent". That has to be the
// default: the brand orange is deliberately a DIFFERENT hex per theme (#b4400f in
// the light theme, so it stays legible as link text on white; #fb923c in the dark
// theme, so it stays legible on near-black), and one inline hex cannot serve both.
const DEFAULTS = { theme: "light" as ThemeName, fontScale: 1, accent: null };
const STORAGE_KEY = "ai4ia-theme";

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

function sanitizeTheme(value: unknown): ThemeName | null {
  return value === "light" || value === "dark" || value === "contrast"
    ? value
    : null;
}

function preferredTheme(): ThemeName {
  return typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(DEFAULTS.theme);
  const [fontScale, setFontScaleState] = useState<number>(DEFAULTS.fontScale);
  const [accent, setAccentState] = useState<string | null>(DEFAULTS.accent);
  const persistTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Hydrate persisted preferences once on mount. A stored choice is explicit;
  // only an absent/invalid theme delegates the initial effective theme to the OS.
  useEffect(() => {
    let saved: Record<string, unknown> = {};
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      const parsed: unknown = raw ? JSON.parse(raw) : null;
      if (parsed && typeof parsed === "object") {
        saved = parsed as Record<string, unknown>;
      }
    } catch {
      // Corrupt local preferences are treated as absent.
    }

    // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time hydration from browser-only preferences
    setThemeState(sanitizeTheme(saved.theme) ?? preferredTheme());
    if (typeof saved.fontScale === "number" && Number.isFinite(saved.fontScale)) {
      setFontScaleState(Math.min(1.6, Math.max(0.8, saved.fontScale)));
    }
    setAccentState(sanitizeAccent(saved.accent));
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

    if (persistTimer.current) clearTimeout(persistTimer.current);
    persistTimer.current = setTimeout(() => {
      try {
        localStorage.setItem(
          STORAGE_KEY,
          JSON.stringify({ theme, fontScale, accent }),
        );
      } catch {
        /* ignore (e.g. quota) */
      }
    }, 300);
  }, [theme, fontScale, accent]);

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

  const value = useMemo(
    () => ({
      theme,
      fontScale,
      accent,
      setTheme,
      setFontScale,
      setAccent,
    }),
    [theme, fontScale, accent, setTheme, setFontScale, setAccent],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}
