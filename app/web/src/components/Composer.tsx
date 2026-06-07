"use client";

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { AgentSummary } from "@/lib/types";

// An active mention being typed at the START of the message (ignoring leading
// whitespace), since the backend only routes a mention at the start of a turn.
interface ActiveMention {
  start: number; // index of the '@'
  end: number; // caret position
  query: string; // text after '@', lowercased
}

const MENTION_RE = /^(\s*)@([A-Za-z0-9_.-]*)$/;
const MAX_OPTIONS = 8;

function detectMention(value: string, caret: number): ActiveMention | null {
  const prefix = value.slice(0, caret);
  const m = prefix.match(MENTION_RE);
  if (!m) return null;
  return { start: m[1].length, end: caret, query: m[2].toLowerCase() };
}

export function Composer({
  disabled,
  streaming,
  agents,
  onSend,
  onStop,
}: {
  disabled: boolean;
  streaming: boolean;
  agents: AgentSummary[];
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [highlight, setHighlight] = useState(0);
  // Set when the user dismisses the menu with Escape; cleared on the next edit.
  const [suppressed, setSuppressed] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Caret position to restore after a programmatic value change (insertion).
  const pendingCaret = useRef<number | null>(null);

  const enabledAgents = useMemo(
    () => agents.filter((a) => a.enabled),
    [agents],
  );

  const mention = useMemo(() => detectMention(text, caret), [text, caret]);

  const filtered = useMemo(() => {
    if (!mention) return [];
    const q = mention.query;
    const matches = enabledAgents.filter(
      (a) =>
        q === "" ||
        a.name.toLowerCase().startsWith(q) ||
        a.displayName.toLowerCase().startsWith(q),
    );
    return matches.slice(0, MAX_OPTIONS);
  }, [mention, enabledAgents]);

  const menuOpen = mention !== null && filtered.length > 0 && !suppressed;

  // Keep the highlight in range as the filtered list changes.
  useEffect(() => {
    setHighlight(0);
  }, [mention?.query, filtered.length]);

  // Restore the caret after an insertion changed the value programmatically.
  useLayoutEffect(() => {
    if (pendingCaret.current === null) return;
    const pos = pendingCaret.current;
    pendingCaret.current = null;
    const el = textareaRef.current;
    if (el) {
      el.focus();
      el.setSelectionRange(pos, pos);
    }
    setCaret(pos);
  }, [text]);

  const syncCaret = (el: HTMLTextAreaElement) => {
    setCaret(el.selectionStart ?? el.value.length);
  };

  const acceptAgent = (agent: AgentSummary) => {
    if (!mention) return;
    const suffix = text.slice(mention.end);
    const insert = suffix.startsWith(" ") ? `@${agent.name}` : `@${agent.name} `;
    const next = text.slice(0, mention.start) + insert + suffix;
    const pos = mention.start + insert.length;
    pendingCaret.current = pos;
    setText(next);
    setSuppressed(false);
  };

  const submit = () => {
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText("");
    setCaret(0);
    setSuppressed(false);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Let IME composition (e.g. CJK input) consume Enter without intercepting.
    if (e.nativeEvent.isComposing) return;

    if (menuOpen) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlight((h) => (h + 1) % filtered.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight((h) => (h - 1 + filtered.length) % filtered.length);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        const agent = filtered[Math.min(highlight, filtered.length - 1)];
        if (agent) acceptAgent(agent);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setSuppressed(true);
        return;
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  const activeOptionId =
    menuOpen && filtered[highlight]
      ? `agent-option-${filtered[highlight].name}`
      : undefined;

  return (
    <div
      style={{
        borderTop: "1px solid var(--border)",
        padding: "12px max(16px, 6%)",
        background: "var(--bg-elevated)",
      }}
    >
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "flex-end",
          position: "relative",
        }}
      >
        {menuOpen && (
          <ul
            id="agent-mention-menu"
            role="listbox"
            aria-label="Agents"
            style={{
              position: "absolute",
              bottom: "calc(100% + 6px)",
              left: 0,
              width: "min(420px, 100%)",
              maxHeight: 280,
              overflowY: "auto",
              margin: 0,
              padding: 4,
              listStyle: "none",
              borderRadius: 12,
              border: "1px solid var(--border)",
              background: "var(--bg-elevated)",
              boxShadow: "0 10px 30px rgba(0,0,0,0.35)",
              zIndex: 20,
            }}
          >
            {filtered.map((a, i) => (
              <li
                key={a.name}
                id={`agent-option-${a.name}`}
                role="option"
                aria-selected={i === highlight}
                onMouseDown={(e) => {
                  // Keep textarea focus so insertion + caret restore work.
                  e.preventDefault();
                  acceptAgent(a);
                }}
                onMouseEnter={() => setHighlight(i)}
                style={{
                  padding: "8px 10px",
                  borderRadius: 8,
                  cursor: "pointer",
                  background:
                    i === highlight ? "var(--accent)" : "transparent",
                  color:
                    i === highlight ? "var(--accent-fg)" : "var(--fg)",
                }}
              >
                <div style={{ fontWeight: 600 }}>
                  @{a.name}
                  <span style={{ opacity: 0.7, fontWeight: 400 }}>
                    {" "}
                    · {a.displayName}
                  </span>
                </div>
                <div style={{ fontSize: "0.8em", opacity: 0.75 }}>
                  {a.description}
                </div>
              </li>
            ))}
          </ul>
        )}

        <label htmlFor="composer" className="visually-hidden">
          Message
        </label>
        <textarea
          id="composer"
          ref={textareaRef}
          value={text}
          rows={1}
          placeholder="Send a message…  (Enter to send, Shift+Enter for newline, @ to mention an agent)"
          role="combobox"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={menuOpen}
          aria-controls="agent-mention-menu"
          aria-activedescendant={activeOptionId}
          onChange={(e) => {
            setText(e.target.value);
            setSuppressed(false);
            syncCaret(e.currentTarget);
          }}
          onSelect={(e) => syncCaret(e.currentTarget)}
          onKeyDown={onKeyDown}
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
