"use client";

import type { ReactNode } from "react";

import { useModalFocus, useModalKeyDown } from "./useModalFocus";

export function ModalShell({
  ariaLabel,
  title,
  filename,
  closeLabel,
  onClose,
  width = "min(520px, 94vw)",
  zIndex = 60,
  contentGap = 16,
  headingFontSize = "1.1em",
  headerGap = 12,
  children,
}: {
  ariaLabel: string;
  title: ReactNode;
  filename?: string;
  closeLabel: string;
  onClose: () => void;
  width?: string;
  zIndex?: number;
  contentGap?: number;
  headingFontSize?: string;
  headerGap?: number;
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
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex,
      }}
    >
      <div
        onClick={(event) => event.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width,
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: contentGap,
          maxHeight: "90vh",
          overflowY: "auto",
        }}
      >
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            gap: headerGap,
          }}
        >
          <h2 style={{ margin: 0, fontSize: headingFontSize }}>
            {title}
            {filename && (
              <span
                style={{
                  display: "block",
                  fontSize: "0.7em",
                  fontWeight: 400,
                  color: "var(--fg-muted)",
                  marginTop: 2,
                }}
              >
                {filename}
              </span>
            )}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={closeLabel}
            style={{
              border: "none",
              background: "transparent",
              color: "var(--fg)",
              fontSize: "1.2em",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}
