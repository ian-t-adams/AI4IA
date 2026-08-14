"use client";

import {
  useCallback,
  useEffect,
  useState,
  useSyncExternalStore,
} from "react";

import {
  closeUnavailableMobileDrawer,
  toggleMobileDrawer,
  type MobileDrawer,
} from "@/lib/workspaceLayout";
import { useMediaQuery } from "./useMediaQuery";

const MOBILE_SIDEBAR_QUERY = "(max-width: 720px)";
const MOBILE_INSPECTOR_QUERY = "(max-width: 1050px)";

type StoredBooleanSnapshot = "0" | "1" | "unavailable";

function readStoredBoolean(key: string): StoredBooleanSnapshot {
  try {
    return localStorage.getItem(key) === "1" ? "1" : "0";
  } catch {
    return "unavailable";
  }
}

function useStoredBoolean(key: string): readonly [boolean, () => void] {
  const eventName = `ai4ia:storage:${key}`;
  const subscribe = useCallback(
    (listener: () => void) => {
      const onStorage = (event: StorageEvent) => {
        if (event.key === key) listener();
      };
      window.addEventListener("storage", onStorage);
      window.addEventListener(eventName, listener);
      return () => {
        window.removeEventListener("storage", onStorage);
        window.removeEventListener(eventName, listener);
      };
    },
    [eventName, key],
  );
  const getSnapshot = useCallback(() => readStoredBoolean(key), [key]);
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, () => "0");
  // A privacy mode may permit reading storage but reject writes. Preserve the
  // current-session toggle behavior even when persistence is unavailable.
  const [memoryOverride, setMemoryOverride] = useState<boolean | null>(null);
  const value = memoryOverride ?? snapshot === "1";
  const toggle = useCallback(() => {
    const next = !value;
    try {
      localStorage.setItem(key, next ? "1" : "0");
      setMemoryOverride(null);
      window.dispatchEvent(new Event(eventName));
    } catch {
      setMemoryOverride(next);
    }
  }, [eventName, key, value]);
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
