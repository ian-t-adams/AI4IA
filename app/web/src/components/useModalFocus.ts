"use client";

import { useCallback, useEffect, useRef, type KeyboardEvent, type RefObject } from "react";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

function focusableChildren(node: HTMLElement): HTMLElement[] {
  return [...node.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (element) => {
      const style = window.getComputedStyle(element);
      return (
        element.tabIndex >= 0 &&
        !element.matches(":disabled") &&
        !element.hidden &&
        !element.closest("[hidden], [inert]") &&
        style.display !== "none" &&
        style.visibility !== "hidden"
      );
    },
  );
}

/**
 * Traps focus within a modal/drawer/panel while it is open: auto-focuses its
 * first focusable child and restores focus to the previously-focused element
 * (or `openerRef`) on close. Attach the returned ref to the panel's outer
 * container, e.g. `<div ref={modalRef} role="dialog" ...>`.
 *
 * Pair this with `useModalKeyDown` (attached via the same element's
 * `onKeyDown`) for Escape-to-close and Tab-trapping. They are kept as two
 * separate hooks — each returning either a bare ref or a bare callback,
 * never both bundled in one object — because the React Compiler's
 * `react-hooks/refs` check conservatively taints an entire returned
 * structure the moment it combines a ref with anything else, flagging every
 * property access off it (even the ref itself) as an unsafe render-time ref
 * read.
 */
export function useModalFocus<T extends HTMLElement = HTMLDivElement>(
  enabled = true,
  openerRef?: RefObject<HTMLElement | null>,
): RefObject<T | null> {
  const ref = useRef<T>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    const node = ref.current;
    if (!enabled || !node) return;

    returnFocusRef.current =
      openerRef?.current ??
      (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const frame = requestAnimationFrame(() => {
      focusableChildren(node)[0]?.focus();
    });

    return () => {
      cancelAnimationFrame(frame);
      // Re-read `openerRef.current` fresh here (not a value captured above):
      // the opener button commonly unmounts while the panel is open and
      // remounts as a new DOM node when it closes, so a captured reference
      // would be stale (detached) and `.focus()` on it would silently
      // no-op, leaving focus stuck on <body>. This is the intentional
      // exception to the rule's general advice ("copy the ref value to a
      // variable and use that in cleanup") — doing that here would
      // reintroduce exactly the stale-focus bug this comment describes.
      // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional live re-read of an externally-owned ref in cleanup; see comment above
      (openerRef?.current ?? returnFocusRef.current)?.focus();
    };
  }, [enabled, openerRef]);

  return ref;
}

/**
 * Closes on Escape and traps Tab within the element this is attached to via
 * `onKeyDown`. Reads the container from `event.currentTarget` rather than a
 * ref, so it stays a plain memoized callback with ordinary React
 * event-bubbling semantics — a nested control (e.g. a rename `<input>`) can
 * still call `event.stopPropagation()` to cancel its own inner state on
 * Escape without also closing the whole panel, exactly as a plain
 * `onKeyDown` prop always has.
 */
export function useModalKeyDown<T extends HTMLElement = HTMLDivElement>(
  onClose: () => void,
  enabled = true,
): (event: KeyboardEvent<T>) => void {
  return useCallback(
    (event: KeyboardEvent<T>) => {
      if (!enabled) return;
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const node = event.currentTarget;
      const focusable = focusableChildren(node);
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
    },
    [enabled, onClose],
  );
}
