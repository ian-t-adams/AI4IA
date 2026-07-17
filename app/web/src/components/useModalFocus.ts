"use client";

import { useCallback, useEffect, useRef, type RefObject } from "react";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function useModalFocus<T extends HTMLElement = HTMLDivElement>(
  onClose: () => void,
  enabled = true,
  openerRef?: RefObject<HTMLElement | null>,
) {
  const ref = useRef<T>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const restoreFocus = useCallback(() => {
    (openerRef?.current ?? returnFocusRef.current)?.focus();
  }, [openerRef]);

  useEffect(() => {
    if (!enabled) return;
    returnFocusRef.current =
      openerRef?.current ??
      (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const frame = requestAnimationFrame(() => {
      ref.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus();
    });
    return () => {
      cancelAnimationFrame(frame);
      restoreFocus();
    };
  }, [enabled, openerRef, restoreFocus]);

  const onKeyDown: React.KeyboardEventHandler<HTMLDivElement> = (event) => {
    if (!enabled) return;
    if (event.key === "Escape") {
      event.preventDefault();
      onClose();
      return;
    }
    if (event.key !== "Tab" || !ref.current) return;
    const focusable = [...ref.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return { ref, onKeyDown };
}
