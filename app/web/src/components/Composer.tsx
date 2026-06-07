"use client";

import { useState } from "react";

export function Composer({
  disabled,
  streaming,
  onSend,
  onStop,
}: {
  disabled: boolean;
  streaming: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
  };

  return (
    <div
      style={{
        borderTop: "1px solid var(--border)",
        padding: "12px max(16px, 6%)",
        background: "var(--bg-elevated)",
      }}
    >
      <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
        <label htmlFor="composer" className="visually-hidden">
          Message
        </label>
        <textarea
          id="composer"
          value={text}
          rows={1}
          placeholder="Send a message…  (Enter to send, Shift+Enter for newline)"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          style={{
            flex: 1,
            padding: "12px 14px",
            resize: "vertical",
            maxHeight: 200,
            minHeight: 46,
          }}
        />
        {streaming ? (
          <button
            onClick={onStop}
            style={{
              padding: "12px 18px",
              borderRadius: 10,
              border: "1px solid var(--border)",
              background: "var(--danger)",
              color: "#fff",
              fontWeight: 600,
            }}
          >
            Stop
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={disabled || !text.trim()}
            style={{
              padding: "12px 22px",
              borderRadius: 10,
              border: "none",
              background:
                disabled || !text.trim() ? "var(--border)" : "var(--accent)",
              color: "var(--accent-fg)",
              fontWeight: 600,
            }}
          >
            Send
          </button>
        )}
      </div>
    </div>
  );
}
