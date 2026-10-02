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
import { hashForView, viewFromHash, type WorkspaceView } from "@/lib/workspaceView";
import type { SidebarMode } from "./Sidebar";
import { useMediaQuery } from "./useMediaQuery";

// Phones: the sidebar is hidden behind a menu button and opens as a drawer.
export const PHONE_QUERY = "(max-width: 720px)";
// Tablets and landscape phones: an icon rail; the full sidebar and the
// inspector open as drawers over the conversation.
export const COMPACT_QUERY = "(max-width: 1099px)";
// Below this width the inspector starts closed so the conversation keeps its
// reading width; at and above it the inspector starts open. An explicit
// choice is remembered either way.
const INSPECTOR_DEFAULT_CLOSED_QUERY = "(max-width: 1439px)";

function readStored(key: string): boolean | null {
  try {
    const stored = localStorage.getItem(key);
    return stored === "1" ? true : stored === "0" ? false : null;
  } catch {
    return null;
  }
}

function useStoredPreference(key: string): readonly [boolean | null, (value: boolean) => void] {
  const [value, setValue] = useState<boolean | null>(null);
  useEffect(() => {
    // Client-only hydration after SSR; reading storage during render would
    // create a mismatch between the server and first browser paint.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setValue(readStored(key));
  }, [key]);
  const store = useCallback(
    (next: boolean) => {
      setValue(next);
      try {
        localStorage.setItem(key, next ? "1" : "0");
      } catch {
        // The current session still toggles when persistence is unavailable.
      }
    },
    [key],
  );
  return [value, store] as const;
}

function currentHash(): string {
  return typeof window === "undefined" ? "" : window.location.hash;
}

export function useWorkspacePanels() {
  const phone = useMediaQuery(PHONE_QUERY);
  const compact = useMediaQuery(COMPACT_QUERY);
  const inspectorStartsClosed = useMediaQuery(INSPECTOR_DEFAULT_CLOSED_QUERY);
  const [leftCollapsed, setLeftCollapsed] = useStoredPreference("ai4ia.leftCollapsed");
  const [rightCollapsed, setRightCollapsed] = useStoredPreference("ai4ia.rightCollapsed");
  const [drawer, setDrawer] = useState<MobileDrawer>(null);
  const [view, setView] = useState<WorkspaceView>("chat");

  // The hash names the page; read it after hydration and follow back/forward.
  useEffect(() => {
    const sync = () => setView(viewFromHash(currentHash()));
    sync();
    window.addEventListener("hashchange", sync);
    window.addEventListener("popstate", sync);
    return () => {
      window.removeEventListener("hashchange", sync);
      window.removeEventListener("popstate", sync);
    };
  }, []);

  const navigate = useCallback((next: WorkspaceView) => {
    setView(next);
    setDrawer(null);
    if (viewFromHash(currentHash()) === next) return;
    const { pathname, search } = window.location;
    window.history.pushState(null, "", `${pathname}${search}${hashForView(next)}`);
  }, []);

  // Close a drawer when its breakpoint stops applying. Listening to the media
  // query directly avoids a synchronous setState inside a React effect.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const compactMedia = window.matchMedia(COMPACT_QUERY);
    const closeUnavailable = () => {
      setDrawer((current) =>
        closeUnavailableMobileDrawer(current, compactMedia.matches, compactMedia.matches),
      );
    };
    compactMedia.addEventListener("change", closeUnavailable);
    return () => compactMedia.removeEventListener("change", closeUnavailable);
  }, []);

  const sidebarDrawerOpen = compact && drawer === "sidebar";
  const inspectorDrawerOpen = compact && drawer === "inspector";
  const sidebarMode: SidebarMode | "hidden" = sidebarDrawerOpen
    ? "drawer"
    : phone
      ? "hidden"
      : compact || leftCollapsed
        ? "collapsed"
        : "expanded";
  const inspectorOpen = compact
    ? inspectorDrawerOpen
    : rightCollapsed === null
      ? !inspectorStartsClosed
      : !rightCollapsed;

  const toggleLeftPanel = useCallback(() => {
    if (compact) {
      setDrawer((current) => toggleMobileDrawer(current, "sidebar"));
    } else {
      setLeftCollapsed(!leftCollapsed);
    }
  }, [compact, leftCollapsed, setLeftCollapsed]);
  const toggleRightPanel = useCallback(() => {
    if (compact) {
      setDrawer((current) => toggleMobileDrawer(current, "inspector"));
    } else {
      setRightCollapsed(inspectorOpen);
    }
  }, [compact, inspectorOpen, setRightCollapsed]);
  const openInspector = useCallback(() => {
    if (compact) setDrawer("inspector");
    else if (!inspectorOpen) setRightCollapsed(false);
  }, [compact, inspectorOpen, setRightCollapsed]);

  return {
    view,
    navigate,
    phone,
    compact,
    sidebarMode,
    sidebarDrawerOpen,
    inspectorDrawerOpen,
    inspectorOpen,
    toggleLeftPanel,
    toggleRightPanel,
    openInspector,
  };
}
