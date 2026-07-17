"use client";

import { useId, useRef, useState } from "react";

export function HelpTooltip({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  const id = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState({ top: 92, left: 16 });
  const show = () => {
    const box = triggerRef.current?.getBoundingClientRect();
    if (box) {
      setPosition({
        top: Math.min(window.innerHeight - 180, box.bottom + 8),
        left: Math.max(16, Math.min(window.innerWidth - 336, box.left - 280)),
      });
    }
    setOpen(true);
  };
  return (
    <span className="help-tooltip">
      <button
        ref={triggerRef}
        type="button"
        className="help-trigger"
        aria-label={`Help: ${label}`}
        aria-describedby={open ? id : undefined}
        onClick={show}
        onMouseEnter={show}
        onMouseLeave={() => setOpen(false)}
        onFocus={show}
        onBlur={() => setOpen(false)}
        onKeyDown={(event) => {
          if (event.key === "Escape") setOpen(false);
        }}
      >
        ?
      </button>
      {open ? (
        <span
          id={id}
          role="tooltip"
          className="help-content"
          style={{ top: position.top, left: position.left, right: "auto" }}
        >
          {children}
        </span>
      ) : null}
    </span>
  );
}
