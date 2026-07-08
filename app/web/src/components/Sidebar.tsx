"use client";

import type { Session } from "@/lib/types";
import { DOCS_INDEX_URL, STATUS_URL } from "@/lib/docs";

export function Sidebar({
  sessions,
  activeId,
  onSelect,
  onNewChat,
  onDelete,
  onOpenSettings,
  onOpenStudio,
  onOpenImagery,
  onOpenLibrary,
  onCollapse,
  disabled = false,
}: {
  sessions: Session[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (id: string) => void;
  onOpenSettings: () => void;
  onOpenStudio: () => void;
  onOpenImagery: () => void;
  onOpenLibrary?: () => void;
  onCollapse?: () => void;
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
      <div style={{ padding: 12, borderTop: "1px solid rgba(255,255,255,0.08)", display: "flex", flexDirection: "column", gap: 8 }}>
        <button
          onClick={onOpenStudio}
          disabled={disabled}
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
        <button
          onClick={onOpenImagery}
          disabled={disabled}
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
          🖼 Imagery studio
        </button>
        {onOpenLibrary && (
          <button
            onClick={onOpenLibrary}
            disabled={disabled}
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
          style={{
            display: "grid",
            gridTemplateColumns: "1fr 1fr",
            gap: 8,
            marginTop: 8,
          }}
        >
          <a
            href={DOCS_INDEX_URL}
            target="_blank"
            rel="noopener noreferrer"
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
            href={STATUS_URL}
            target="_blank"
            rel="noopener noreferrer"
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
      </div>
    </nav>
  );
}
