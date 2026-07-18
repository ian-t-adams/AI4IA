"use client";

import { useId } from "react";
import type { RefObject } from "react";
import type { Session } from "@/lib/types";
import { DOCS_INDEX_URL, STATUS_URL } from "@/lib/docs";
import { AdminLink } from "./AdminLink";
import { UserMenu } from "./UserMenu";
import { useMediaQuery } from "./useMediaQuery";
import { useModalFocus } from "./useModalFocus";
import { EditableSessionTitle } from "./EditableSessionTitle";

export function Sidebar({
  sessions,
  activeId,
  onSelect,
  onNewChat,
  onDelete,
  onRename,
  onOpenSettings,
  onOpenStudio,
  onOpenLibrary,
  onCollapse,
  openerRef,
  disabled = false,
  disabledReason,
}: {
  sessions: Session[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (id: string) => void;
  onRename: (id: string, title: string) => Promise<void>;
  onOpenSettings: () => void;
  onOpenStudio: () => void;
  onOpenLibrary?: () => void;
  onCollapse?: () => void;
  openerRef?: RefObject<HTMLElement | null>;
  disabled?: boolean;
  /** Tooltip shown on disabled controls, explaining why and how to recover. */
  disabledReason?: string;
}) {
  const mobileDrawer = useMediaQuery("(max-width: 720px)") && Boolean(onCollapse);
  const drawerFocus = useModalFocus<HTMLElement>(
    onCollapse ?? (() => {}),
    mobileDrawer,
    openerRef,
  );
  // Shared id for the visible lock-reason hint below, referenced via
  // aria-describedby by every control this component soft-disables so
  // screen reader users get the same recovery guidance sighted users see
  // from disabledReason, not just a native title tooltip (which is neither
  // reliably announced nor keyboard-reachable).
  const lockHintId = useId();
  const describedBy = disabled && disabledReason ? lockHintId : undefined;
  return (
    <nav
      ref={drawerFocus.ref}
      onKeyDown={drawerFocus.onKeyDown}
      className="session-sidebar"
      role={mobileDrawer ? "dialog" : "navigation"}
      aria-modal={mobileDrawer ? true : undefined}
      aria-label="Chat sessions"
      style={{
        width: 280,
        maxWidth: "100vw",
        flexShrink: 0,
        background: "var(--bg-sidebar)",
        color: "var(--sidebar-fg)",
        display: "flex",
        flexDirection: "column",
        height: "100%",
        maxHeight: "100dvh",
        minHeight: 0,
        overflow: "hidden",
      }}
    >
      <div style={{ padding: 16, display: "flex", alignItems: "center", gap: 10 }}>
        {/* eslint-disable-next-line @next/next/no-img-element -- small static brand mark; next/image adds no value here */}
        <img
          src="/ai4ia-mark.png"
          alt=""
          aria-hidden="true"
          width={28}
          height={28}
          style={{ borderRadius: 6, flexShrink: 0, display: "block" }}
        />
        <span style={{ fontWeight: 700, fontSize: "1.1em", letterSpacing: 0.5 }}>
          AI4IA
        </span>
        {onCollapse && (
          <button
            onClick={onCollapse}
            aria-label="Collapse sidebar"
            title="Collapse sidebar"
            style={{
              marginLeft: "auto",
              border: "none",
              background: "transparent",
              color: "var(--sidebar-muted)",
              cursor: "pointer",
              fontSize: "1.1em",
              lineHeight: 1,
              padding: 4,
            }}
          >
            «
          </button>
        )}
      </div>
      <div
        className="sidebar-scroll"
        data-testid="sidebar-scroll"
        style={{
          display: "flex",
          flexDirection: "column",
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          overflowX: "hidden",
        }}
      >
      <div style={{ padding: "0 12px 12px" }}>
        {disabled && disabledReason && (
          <p
            id={lockHintId}
            role="status"
            style={{
              margin: "0 0 10px",
              padding: "8px 10px",
              borderRadius: 8,
              background: "rgba(255,255,255,0.08)",
              color: "var(--sidebar-fg)",
              fontSize: "0.85em",
              lineHeight: 1.4,
            }}
          >
            {disabledReason}
          </p>
        )}
        <button
          onClick={() => {
            if (disabled) return;
            onNewChat();
          }}
          aria-disabled={disabled || undefined}
          aria-describedby={describedBy}
          style={{
            width: "100%",
            padding: "10px 14px",
            borderRadius: 10,
            border: "1px solid var(--border)",
            background: "var(--accent)",
            color: "var(--accent-fg)",
            fontWeight: 600,
            opacity: disabled ? 0.5 : 1,
            cursor: disabled ? "not-allowed" : "pointer",
          }}
        >
          + New chat
        </button>
      </div>
      <ul
        style={{
          listStyle: "none",
          margin: 0,
          padding: "0 8px",
          overflowY: "visible",
          flex: "0 0 auto",
          minHeight: 0,
        }}
      >
        {sessions.length === 0 && (
          <li style={{ padding: 12, color: "var(--sidebar-muted)", fontSize: "0.9em" }}>
            No conversations yet.
          </li>
        )}
        {sessions.map((s) => {
          const active = s.id === activeId;
          return (
            <li key={s.id} style={{ display: "flex", alignItems: "center" }}>
              <div
                className="session-row-main"
                style={{
                  flex: 1,
                  minWidth: 0,
                  margin: "2px 0",
                  borderRadius: 8,
                  background: active ? "rgba(255,255,255,0.12)" : "transparent",
                }}
              >
                <EditableSessionTitle
                  title={s.title || "Untitled"}
                  onSave={(title) => onRename(s.id, title)}
                  onOpen={() => onSelect(s.id)}
                  current={active}
                  disabled={disabled}
                  disabledReasonId={lockHintId}
                  compact
                />
              </div>
              <button
                onClick={() => {
                  if (disabled) return;
                  onDelete(s.id);
                }}
                aria-disabled={disabled || undefined}
                aria-label={`Delete ${s.title || "conversation"}`}
                aria-describedby={describedBy}
                title={disabled ? undefined : "Delete"}
                style={{
                  border: "none",
                  background: "transparent",
                  color: "var(--sidebar-muted)",
                  padding: "6px 8px",
                  opacity: disabled ? 0.5 : 1,
                  cursor: disabled ? "not-allowed" : "pointer",
                }}
              >
                ✕
              </button>
            </li>
          );
        })}
      </ul>
      <div className="sidebar-utility-region">
        <button
          className="sidebar-utility-action"
          onClick={() => {
            if (disabled) return;
            onOpenStudio();
          }}
          aria-disabled={disabled || undefined}
          aria-describedby={describedBy}
          style={{
            width: "100%",
            padding: "8px 12px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "transparent",
            color: "var(--sidebar-fg)",
            cursor: disabled ? "not-allowed" : "pointer",
          }}
        >
          🛠 Agents &amp; workflows
        </button>
        {onOpenLibrary && (
          <button
            className="sidebar-utility-action"
            onClick={() => {
              if (disabled) return;
              onOpenLibrary();
            }}
            aria-disabled={disabled || undefined}
            aria-describedby={describedBy}
            style={{
              width: "100%",
              padding: "8px 12px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              background: "transparent",
              color: "var(--sidebar-fg)",
              cursor: disabled ? "not-allowed" : "pointer",
            }}
          >
            📚 Document library
          </button>
        )}
        <button
          className="sidebar-utility-action"
          onClick={onOpenSettings}
          title="Theme, text size, accessibility, and media generation options"
          style={{
            width: "100%",
            padding: "8px 12px",
            borderRadius: 8,
            border: "1px solid var(--border)",
            background: "transparent",
            color: "var(--sidebar-fg)",
          }}
        >
          ⚙ Appearance &amp; accessibility
        </button>
        <div
          className="sidebar-utility-links"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            marginTop: 8,
          }}
        >
          <a
            className="sidebar-utility-action"
            href={DOCS_INDEX_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Documentation (opens in new tab)"
            title="Browse the AI4IA documentation hub"
            style={{
              textAlign: "center",
              padding: "8px 12px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              color: "var(--sidebar-fg)",
              textDecoration: "none",
              fontSize: "0.9em",
            }}
          >
            📖 Docs
          </a>
          <a
            className="sidebar-utility-action"
            href={STATUS_URL}
            target="_blank"
            rel="noopener noreferrer"
            aria-label="Status (opens in new tab)"
            title="Live deployment health and service status"
            style={{
              textAlign: "center",
              padding: "8px 12px",
              borderRadius: 8,
              border: "1px solid var(--border)",
              color: "var(--sidebar-fg)",
              textDecoration: "none",
              fontSize: "0.9em",
            }}
          >
            📡 Status
          </a>
        </div>
        <div
          aria-label="Account and administration"
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            color: "var(--sidebar-fg)",
            ["--fg" as string]: "var(--sidebar-fg)",
            ["--fg-muted" as string]: "var(--sidebar-muted)",
            ["--bg-elevated" as string]: "transparent",
          }}
        >
          <AdminLink disabled={disabled} disabledReasonId={lockHintId} />
          <UserMenu disabled={disabled} disabledReasonId={lockHintId} />
        </div>
      </div>
      </div>
    </nav>
  );
}
