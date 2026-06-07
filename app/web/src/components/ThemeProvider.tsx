"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from "react";

export type ThemeName = "light" | "dark" | "contrast";

interface ThemeState {
  theme: ThemeName;
  fontScale: number;
  accent: string;
  setTheme: (t: ThemeName) => void;
  setFontScale: (s: number) => void;
  setAccent: (c: string) => void;
}

const DEFAULTS = { theme: "light" as ThemeName, fontScale: 1, accent: "#4f46e5" };
const STORAGE_KEY = "ai4ia-theme";

const ThemeContext = createContext<ThemeState | null>(null);

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [theme, setThemeState] = useState<ThemeName>(DEFAULTS.theme);
  const [fontScale, setFontScaleState] = useState<number>(DEFAULTS.fontScale);
  const [accent, setAccentState] = useState<string>(DEFAULTS.accent);

  // Hydrate persisted preferences once on mount.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        if (saved.theme) setThemeState(saved.theme);
        if (saved.fontScale) setFontScaleState(saved.fontScale);
        if (saved.accent) setAccentState(saved.accent);
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
    // Accent is honored only outside the high-contrast theme.
    if (theme !== "contrast") {
      root.style.setProperty("--accent", accent);
      root.style.setProperty("--user-bubble", accent);
    } else {
      root.style.removeProperty("--accent");
      root.style.removeProperty("--user-bubble");
    }
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ theme, fontScale, accent }),
      );
    } catch {
      /* ignore */
    }
  }, [theme, fontScale, accent]);

  const setTheme = useCallback((t: ThemeName) => setThemeState(t), []);
  const setFontScale = useCallback(
    (s: number) => setFontScaleState(Math.min(1.6, Math.max(0.8, s))),
    [],
  );
  const setAccent = useCallback((c: string) => setAccentState(c), []);

  const value = useMemo(
    () => ({ theme, fontScale, accent, setTheme, setFontScale, setAccent }),
    [theme, fontScale, accent, setTheme, setFontScale, setAccent],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used within ThemeProvider");
  return ctx;
}
