"use client";

import type { CSSProperties, ReactNode } from "react";

import { useModalFocus, useModalKeyDown } from "./useModalFocus";

export function DialogFrame({
  ariaLabel,
  onClose,
  zIndex = 60,
  overlayPadding,
  children,
}: {
  ariaLabel: string;
  onClose: () => void;
  zIndex?: number;
  overlayPadding?: CSSProperties["padding"];
  children: ReactNode;
}) {
  const modalRef = useModalFocus();
  const onModalKeyDown = useModalKeyDown(onClose);

  return (
    <div
      ref={modalRef}
      onKeyDown={onModalKeyDown}
      role="dialog"
      aria-label={ariaLabel}
      aria-modal="true"
      onClick={onClose}
      style={{
        position: "fixed",
        inset: 0,
        padding: overlayPadding,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex,
      }}
    >
      {children}
    </div>
  );
}
