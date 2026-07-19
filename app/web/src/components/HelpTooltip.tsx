"use client";

import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

/**
 * Inline help affordance: a small trigger with its own `aria-label`
 * ("Help: {label}") that opens a floating, plain-language note on
 * hover/focus/click.
 *
 * Gotcha when placing this next to a form control: the trigger's
 * `aria-label` is itself an accessible name, so if you nest a HelpTooltip
 * *inside* a `<label htmlFor>` / wrapping `<label>` / `<legend>` for that
 * control, it gets pulled into the control's (or fieldset's) own
 * computed accessible name (per the "name from content" step of the
 * accname spec), overriding what you intended the name to be. Same risk for
 * any control whose name is computed from surrounding content, e.g. a
 * `<select>` inside a wrapping `<label>` that also contains other text.
 *
 * Fix: either (a) give the control an explicit `aria-label={visibleLabel}`
 * so it short-circuits before falling back to content, or (b) keep the
 * HelpTooltip as a sibling of the label/legend rather than nested inside
 * it. See AgentBuilder.tsx's tool/agent-link checkboxes and ModelPicker.tsx
 * for examples of (a).
 */
export function HelpTooltip({
  label,
  children,
  size = "default",
}: {
  label: string;
  children: React.ReactNode;
  // "sm" shrinks the trigger for use inline within dense rows (status pills,
  // per-item badges) where the default 44px touch target would overwhelm the
  // surrounding text. Defaults to the original size everywhere else.
  size?: "default" | "sm";
}) {
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const contentRef = useRef<HTMLSpanElement>(null);
  const pinnedRef = useRef(false);
  const [open, setOpen] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [position, setPosition] = useState({ top: 92, left: 16 });
  const updatePosition = useCallback(() => {
    const box = triggerRef.current?.getBoundingClientRect();
    if (box) {
      const content = contentRef.current?.getBoundingClientRect();
      const width = Math.min(content?.width || 320, Math.max(0, window.innerWidth - 32));
      const height = Math.min(content?.height || 160, Math.max(0, window.innerHeight - 32));
      const maxLeft = Math.max(16, window.innerWidth - width - 16);
      const maxTop = Math.max(16, window.innerHeight - height - 16);
      const below = box.bottom + 8;
      const preferredTop =
        below + height <= window.innerHeight - 16 ? below : box.top - height - 8;
      setPosition({
        top: Math.max(16, Math.min(maxTop, preferredTop)),
        left: Math.max(16, Math.min(maxLeft, box.right - width)),
      });
    }
  }, []);
  const show = () => {
    updatePosition();
    setOpen(true);
  };
  useEffect(() => {
    if (!open) return;
    const reposition = () => updatePosition();
    const outside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !triggerRef.current?.contains(target) &&
        !contentRef.current?.contains(target)
      ) {
        setOpen(false);
        setPinned(false);
        pinnedRef.current = false;
      }
    };
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);
    document.addEventListener("pointerdown", outside);
    return () => {
      window.removeEventListener("scroll", reposition, true);
      window.removeEventListener("resize", reposition);
      document.removeEventListener("pointerdown", outside);
    };
  }, [open, updatePosition]);
  useLayoutEffect(() => {
    if (open) updatePosition();
  }, [children, open, updatePosition]);
  return (
    <span className={size === "sm" ? "help-tooltip help-tooltip-sm" : "help-tooltip"}>
      <button
        ref={triggerRef}
        type="button"
        className={size === "sm" ? "help-trigger help-trigger-sm" : "help-trigger"}
        aria-label={`Help: ${label}`}
        aria-describedby={open ? id : undefined}
        onClick={(event) => {
          // The trigger is sometimes nested inside a larger clickable row
          // (e.g. a checkbox <label>, a list item). Stop the click from
          // bubbling so opening help never also toggles/activates whatever
          // it's embedded in.
          event.stopPropagation();
          if (pinned) {
            pinnedRef.current = false;
            setPinned(false);
            setOpen(false);
          } else {
            pinnedRef.current = true;
            setPinned(true);
            show();
          }
        }}
        onMouseEnter={show}
        onMouseLeave={() => {
          if (!pinnedRef.current) setOpen(false);
        }}
        onFocus={show}
        onBlur={() => {
          if (!pinnedRef.current) setOpen(false);
        }}
        onKeyDown={(event) => {
          // Only consume Escape while the tooltip is actually open, and stop
          // it from bubbling in that case -- otherwise a tooltip nested
          // inside a modal/drawer would also close that enclosing surface on
          // the same keypress. When the tooltip isn't open, let Escape pass
          // through untouched so the enclosing dialog can still handle it.
          if (event.key === "Escape" && open) {
            event.stopPropagation();
            setOpen(false);
            setPinned(false);
            pinnedRef.current = false;
          }
        }}
      >
        ?
      </button>
      {open && typeof document !== "undefined"
        ? createPortal(
            <span
              ref={contentRef}
              id={id}
              role="tooltip"
              className="help-content"
              style={{
                top: position.top,
                left: position.left,
                right: "auto",
                maxHeight: "calc(100dvh - 32px)",
                overflowY: "auto",
                overflowX: "hidden",
                overflowWrap: "anywhere",
              }}
            >
              {children}
            </span>,
            document.body,
          )
        : null}
    </span>
  );
}
