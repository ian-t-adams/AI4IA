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

export function HelpTooltip({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
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
    <span className="help-tooltip">
      <button
        ref={triggerRef}
        type="button"
        className="help-trigger"
        aria-label={`Help: ${label}`}
        aria-describedby={open ? id : undefined}
        onClick={() => {
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
          if (event.key === "Escape") {
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
