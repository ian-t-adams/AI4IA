"use client";

import { useEffect, useState } from "react";

export function SystemPromptEditor({
  value,
  onSave,
}: {
  value: string;
  onSave: (next: string) => void;
}) {
  const [draft, setDraft] = useState(value);
  const [dirty, setDirty] = useState(false);

  useEffect(() => {
    setDraft(value);
    setDirty(false);
  }, [value]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      <span style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
        System prompt
      </span>
      <textarea
        value={draft}
        rows={6}
        placeholder="You are a helpful assistant…"
        onChange={(e) => {
          setDraft(e.target.value);
          setDirty(true);
        }}
        style={{ padding: "8px 10px", resize: "vertical", minHeight: 90 }}
      />
      <button
        disabled={!dirty}
        onClick={() => {
          onSave(draft);
          setDirty(false);
        }}
        style={{
          alignSelf: "flex-start",
          padding: "6px 12px",
          borderRadius: 8,
          border: "1px solid var(--border)",
          background: dirty ? "var(--accent)" : "var(--bg-elevated)",
          color: dirty ? "var(--accent-fg)" : "var(--fg-muted)",
        }}
      >
        Save prompt
      </button>
    </div>
  );
}
