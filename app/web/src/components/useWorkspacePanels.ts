"use client";

import {
  useCallback,
  useEffect,
  useState,
} from "react";

import {
  closeUnavailableMobileDrawer,
  toggleMobileDrawer,
  type MobileDrawer,
} from "@/lib/workspaceLayout";
import { useMediaQuery } from "./useMediaQuery";

const MOBILE_SIDEBAR_QUERY = "(max-width: 720px)";
const MOBILE_INSPECTOR_QUERY = "(max-width: 1050px)";

function useStoredBoolean(key: string): readonly [boolean, () => void] {
  const [value, setValue] = useState(false);
  useEffect(() => {
    let stored = false;
    try {
      stored = localStorage.getItem(key) === "1";
    } catch {
      // Keep the SSR-safe expanded default when storage is unavailable.
    }
    // Client-only hydration after SSR; reading storage during render would
    // create a mismatch between the server and first browser paint.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setValue(stored);
  }, [key]);
  const toggle = useCallback(() => {
    const next = !value;
    setValue(next);
    try {
      localStorage.setItem(key, next ? "1" : "0");
    } catch {
      // The current session still toggles when persistence is unavailable.
    }
  }, [key, value]);
  return [value, toggle] as const;
}

export function useWorkspacePanels() {
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [studioOpen, setStudioOpen] = useState(false);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [leftCollapsed, toggleLeftCollapsed] = useStoredBoolean(
    "ai4ia.leftCollapsed",
  );
  const [rightCollapsed, toggleRightCollapsed] = useStoredBoolean(
    "ai4ia.rightCollapsed",
  );
  const [mobileDrawer, setMobileDrawer] = useState<MobileDrawer>(null);
  const mobileSidebar = useMediaQuery(MOBILE_SIDEBAR_QUERY);
  const drawerInspector = useMediaQuery(MOBILE_INSPECTOR_QUERY);
  const mobileSidebarOpen = mobileSidebar && mobileDrawer === "sidebar";
  const mobileInspectorOpen = drawerInspector && mobileDrawer === "inspector";

  // Close a mobile drawer when its breakpoint stops applying. Listening to the
  // media query directly avoids a synchronous setState inside a React effect.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const sidebarMedia = window.matchMedia(MOBILE_SIDEBAR_QUERY);
    const inspectorMedia = window.matchMedia(MOBILE_INSPECTOR_QUERY);
    const closeUnavailable = () => {
      setMobileDrawer((current) =>
        closeUnavailableMobileDrawer(
          current,
          sidebarMedia.matches,
          inspectorMedia.matches,
        ),
      );
    };
    sidebarMedia.addEventListener("change", closeUnavailable);
    inspectorMedia.addEventListener("change", closeUnavailable);
    return () => {
      sidebarMedia.removeEventListener("change", closeUnavailable);
      inspectorMedia.removeEventListener("change", closeUnavailable);
    };
  }, []);

  const toggleLeftPanel = useCallback(() => {
    if (mobileSidebar) {
      setMobileDrawer((current) => toggleMobileDrawer(current, "sidebar"));
    } else {
      toggleLeftCollapsed();
    }
  }, [mobileSidebar, toggleLeftCollapsed]);
  const toggleRightPanel = useCallback(() => {
    if (drawerInspector) {
      setMobileDrawer((current) => toggleMobileDrawer(current, "inspector"));
    } else {
      toggleRightCollapsed();
    }
  }, [drawerInspector, toggleRightCollapsed]);
  const openSettings = useCallback(() => setSettingsOpen(true), []);
  const closeSettings = useCallback(() => setSettingsOpen(false), []);
  const openStudio = useCallback(() => setStudioOpen(true), []);
  const closeStudio = useCallback(() => setStudioOpen(false), []);
  const openLibrary = useCallback(() => setLibraryOpen(true), []);
  const closeLibrary = useCallback(() => setLibraryOpen(false), []);

  return {
    settingsOpen,
    studioOpen,
    libraryOpen,
    openSettings,
    closeSettings,
    openStudio,
    closeStudio,
    openLibrary,
    closeLibrary,
    mobileSidebar,
    drawerInspector,
    mobileSidebarOpen,
    mobileInspectorOpen,
    leftIsCollapsed: mobileSidebar ? !mobileSidebarOpen : leftCollapsed,
    rightIsCollapsed: drawerInspector
      ? !mobileInspectorOpen
      : rightCollapsed,
    toggleLeftPanel,
    toggleRightPanel,
  };
}
