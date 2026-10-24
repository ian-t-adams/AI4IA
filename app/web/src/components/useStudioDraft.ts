"use client";

import { useCallback, useEffect } from "react";

const NAVIGATION_EVENT = "ai4ia:before-workspace-navigation";

export function allowWorkspaceNavigation(): boolean {
  return window.dispatchEvent(new Event(NAVIGATION_EVENT, { cancelable: true }));
}

export function useStudioDraft(dirty: boolean, busy: boolean) {
  const confirmDiscard = useCallback(() => {
    if (busy) {
      window.alert("Wait for the current save or run to finish before leaving this editor.");
      return false;
    }
    return !dirty || window.confirm("Discard unsaved changes? Stay here to keep editing or save first.");
  }, [dirty, busy]);

  useEffect(() => {
    const beforeNavigation = (event: Event) => {
      if (!confirmDiscard()) event.preventDefault();
    };
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!dirty && !busy) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener(NAVIGATION_EVENT, beforeNavigation);
    window.addEventListener("beforeunload", beforeUnload);
    return () => {
      window.removeEventListener(NAVIGATION_EVENT, beforeNavigation);
      window.removeEventListener("beforeunload", beforeUnload);
    };
  }, [confirmDiscard, dirty, busy]);

  return confirmDiscard;
}
