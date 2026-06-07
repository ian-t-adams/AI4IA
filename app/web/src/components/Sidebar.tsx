"use client";

import type { Session } from "@/lib/types";

export function Sidebar({
  sessions,
  activeId,
  onSelect,
  onNewChat,
  onDelete,
  onOpenSettings,
  disabled = false,
}: {
  sessions: Session[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (id: string) => void;
  onOpenSettings: () => void;
  disabled?: boolean;
}) {
  return (
    <nav
      aria-label="Chat sessions"
      style={{
        width: 280,
        flexShrink: 0,
        background: "var(--bg-sidebar)",
        color: "var(--sidebar-fg)",
        display: "flex",
        flexDirection: "column",
        height: "100%",
      }}
    >
      <div style={{ padding: 16, display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontWeight: 700, fontSize: "1.1em", letterSpacing: 0.5 }}>
          AI4IA
        </span>
      </div>
      <div style={{ padding: "0 12px 12px" }}>
        <button
          onClick={onNewChat}
          disabled={disabled}
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
          overflowY: "auto",
          flex: 1,
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
              <button
                onClick={() => onSelect(s.id)}
                disabled={disabled}
                aria-current={active ? "true" : undefined}
                style={{
                  flex: 1,
                  textAlign: "left",
                  padding: "10px 12px",
                  margin: "2px 0",
                  borderRadius: 8,
                  border: "none",
                  background: active ? "rgba(255,255,255,0.12)" : "transparent",
                  color: "var(--sidebar-fg)",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                  cursor: disabled ? "not-allowed" : "pointer",
                }}
              >
                {s.title || "Untitled"}
              </button>
              <button
                onClick={() => onDelete(s.id)}
                disabled={disabled}
                aria-label={`Delete ${s.title || "conversation"}`}
                title="Delete"
                style={{
                  border: "none",
                  background: "transparent",
                  color: "var(--sidebar-muted)",
                  padding: "6px 8px",
                  cursor: disabled ? "not-allowed" : "pointer",
                }}
              >
                ✕
              </button>
            </li>
          );
        })}
      </ul>
      <div style={{ padding: 12, borderTop: "1px solid rgba(255,255,255,0.08)" }}>
        <button
          onClick={onOpenSettings}
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
      </div>
    </nav>
  );
}
