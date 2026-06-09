"use client";

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { AgentSummary, DocumentSummary } from "@/lib/types";
import { useVoiceRecorder } from "@/lib/voice";
import { useVoiceLive, type VoiceLiveConfig } from "@/lib/voiceLive";

// Mirrors the backend cap (routers/documents.py MAX_DOCS_PER_SESSION).
const MAX_DOCS = 8;
// Hint shown next to the file control; mirrors the chat-context budget.
const DOC_BUDGET_HINT = "up to ~12K chars/turn";
// Hint for the file picker; the backend accepts the text family + pdf/docx/pptx.
const FILE_ACCEPT =
  ".txt,.md,.markdown,.csv,.tsv,.json,.log,.xml,.yaml,.yml,.html,.htm,.pdf,.docx,.pptx,text/plain,application/pdf";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// An active mention being typed at the START of the message (ignoring leading
// whitespace), since the backend only routes a mention at the start of a turn.
interface ActiveMention {
  start: number; // index of the '@'
  end: number; // caret position
  query: string; // text after '@', lowercased
}

const MENTION_RE = /^(\s*)@([A-Za-z0-9_.-]*)$/;
const MAX_OPTIONS = 8;

// A stable disabled config so the always-called useVoiceLive hook gets a constant
// reference when the feature is off (no per-render object churn).
const DISABLED_LIVE: VoiceLiveConfig = { enabled: false, wsUrl: "", devUser: "" };

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
  documents,
  uploading,
  onSend,
  onStop,
  onUpload,
  onRemoveDocument,
  onError,
  voiceLiveEnabled = false,
  voiceLiveConfig,
  voiceLiveModel = null,
}: {
  disabled: boolean;
  streaming: boolean;
  agents: AgentSummary[];
  documents: DocumentSummary[];
  uploading: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onUpload: (file: File) => void;
  onRemoveDocument: (id: string) => void;
  onError?: (message: string) => void;
  voiceLiveEnabled?: boolean;
  voiceLiveConfig?: VoiceLiveConfig;
  voiceLiveModel?: string | null;
}) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [highlight, setHighlight] = useState(0);
  // Set when the user dismisses the menu with Escape; cleared on the next edit.
  const [suppressed, setSuppressed] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Hidden file input driven by the attach button.
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Caret position to restore after a programmatic value change (insertion).
  const pendingCaret = useRef<number | null>(null);

  const atDocLimit = documents.length >= MAX_DOCS;

  const onPickFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    // Upload sequentially (the parent dedupes the lazy session creation); the
    // backend enforces the real per-session cap and rejects extras.
    const remaining = MAX_DOCS - documents.length;
    if (remaining <= 0) {
      onError?.(`You can upload at most ${MAX_DOCS} documents per chat.`);
      return;
    }
    Array.from(files)
      .slice(0, remaining)
      .forEach((f) => onUpload(f));
  };

  // Latest text, so the async voice callback appends to the current value
  // without capturing a stale closure.
  const textRef = useRef(text);
  useEffect(() => {
    textRef.current = text;
  }, [text]);

  // Append a dictated transcript at the end of the message (a space-separated
  // continuation), then place the caret at the end.
  const appendTranscript = (transcript: string) => {
    const prev = textRef.current;
    const sep = prev && !/\s$/.test(prev) ? " " : "";
    const next = prev + sep + transcript;
    pendingCaret.current = next.length;
    setText(next);
    setSuppressed(false);
  };

  const voice = useVoiceRecorder(appendTranscript, (msg) => onError?.(msg));

  // Live voice (Phase 10). The hook is always called (rules of hooks) but stays
  // inert until the user toggles it; the control below is only rendered when the
  // feature flag is on and a realtime model exists.
  const live = useVoiceLive(
    voiceLiveConfig ?? DISABLED_LIVE,
    voiceLiveModel,
    (msg) => onError?.(msg),
  );
  const showLive = voiceLiveEnabled && live.supported;

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
      {documents.length > 0 && (
        <ul
          aria-label="Attached documents"
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 8,
            margin: "0 0 8px",
            padding: 0,
            listStyle: "none",
          }}
        >
          {documents.map((d) => (
            <li
              key={d.id}
              title={`${d.filename} · ${formatBytes(d.size)} · ${d.charCount.toLocaleString()} chars${d.truncated ? " (truncated)" : ""}`}
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 8,
                maxWidth: 280,
                padding: "5px 6px 5px 10px",
                borderRadius: 999,
                border: "1px solid var(--border)",
                background: "var(--bg)",
                fontSize: "0.8em",
              }}
            >
              <span aria-hidden="true">📄</span>
              <span
                style={{
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {d.filename}
              </span>
              <span style={{ color: "var(--fg-muted)", flexShrink: 0 }}>
                {formatBytes(d.size)}
                {d.truncated ? " ·✂" : ""}
              </span>
              <button
                type="button"
                onClick={() => onRemoveDocument(d.id)}
                aria-label={`Remove ${d.filename}`}
                title="Remove document"
                style={{
                  flexShrink: 0,
                  width: 20,
                  height: 20,
                  display: "grid",
                  placeItems: "center",
                  borderRadius: "50%",
                  border: "none",
                  background: "transparent",
                  color: "var(--fg-muted)",
                  cursor: "pointer",
                  lineHeight: 1,
                }}
              >
                ✕
              </button>
            </li>
          ))}
          <li
            style={{
              alignSelf: "center",
              fontSize: "0.72em",
              color: "var(--fg-muted)",
            }}
          >
            {documents.length}/{MAX_DOCS} · {DOC_BUDGET_HINT}
          </li>
        </ul>
      )}

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

        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept={FILE_ACCEPT}
          className="visually-hidden"
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            onPickFiles(e.target.files);
            // Reset so re-selecting the same file fires onChange again.
            e.target.value = "";
          }}
        />
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          disabled={uploading || atDocLimit}
          aria-busy={uploading}
          aria-label={
            atDocLimit
              ? `Document limit reached (${MAX_DOCS})`
              : uploading
                ? "Uploading document"
                : "Attach a document"
          }
          title={
            atDocLimit
              ? `You can upload at most ${MAX_DOCS} documents per chat`
              : uploading
                ? "Uploading…"
                : `Attach a document (${DOC_BUDGET_HINT})`
          }
          style={{
            alignSelf: "stretch",
            minHeight: 46,
            padding: "0 14px",
            borderRadius: 10,
            border: "1px solid var(--border)",
            background: "var(--bg)",
            color: "var(--fg)",
            fontSize: "1.15em",
            lineHeight: 1,
            cursor: uploading || atDocLimit ? "not-allowed" : "pointer",
            opacity: uploading || atDocLimit ? 0.45 : 1,
          }}
        >
          {uploading ? "…" : "📎"}
        </button>

        <button
          type="button"
          onClick={voice.toggle}
          disabled={!voice.supported || voice.transcribing}
          aria-pressed={voice.recording}
          aria-busy={voice.transcribing}
          aria-label={
            voice.transcribing
              ? "Transcribing audio"
              : voice.recording
                ? "Stop recording"
                : "Record a voice message"
          }
          title={
            !voice.supported
              ? "Voice input isn't supported in this browser"
              : voice.transcribing
                ? "Transcribing…"
                : voice.recording
                  ? "Stop recording"
                  : "Record a voice message"
          }
          style={{
            alignSelf: "stretch",
            minHeight: 46,
            padding: "0 14px",
            borderRadius: 10,
            border: "1px solid var(--border)",
            background: voice.recording ? "var(--danger)" : "var(--bg)",
            color: voice.recording ? "#fff" : "var(--fg)",
            fontSize: "1.15em",
            lineHeight: 1,
            cursor:
              !voice.supported || voice.transcribing
                ? "not-allowed"
                : "pointer",
            opacity: voice.supported ? 1 : 0.45,
          }}
        >
          {voice.transcribing ? "…" : voice.recording ? "■" : "🎙"}
        </button>

        {showLive && (
          <button
            type="button"
            onClick={live.toggle}
            aria-pressed={live.active}
            aria-busy={live.status === "connecting"}
            aria-label={
              live.status === "connecting"
                ? "Connecting live voice"
                : live.active
                  ? "Stop live voice"
                  : "Start live voice conversation"
            }
            title={
              live.status === "connecting"
                ? "Connecting…"
                : live.active
                  ? "Stop live voice"
                  : "Start a live voice conversation"
            }
            style={{
              alignSelf: "stretch",
              minHeight: 46,
              padding: "0 14px",
              borderRadius: 10,
              border: "1px solid var(--border)",
              background: live.active ? "var(--accent)" : "var(--bg)",
              color: live.active ? "var(--accent-fg)" : "var(--fg)",
              fontSize: "1.05em",
              lineHeight: 1,
              cursor: "pointer",
              display: "inline-flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <span aria-hidden="true">
              {live.status === "connecting" ? "…" : live.active ? "◉" : "🎧"}
            </span>
            <span style={{ fontSize: "0.85em", fontWeight: 600 }}>Live</span>
          </button>
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

      <div
        aria-live="polite"
        style={{
          minHeight: 16,
          marginTop: 6,
          fontSize: "0.75em",
          color: voice.recording ? "var(--danger)" : "var(--fg-muted)",
        }}
      >
        {voice.recording
          ? "● Recording… click the mic again to stop."
          : voice.transcribing
            ? "Transcribing your audio…"
            : showLive && live.active
              ? live.status === "connecting"
                ? "Connecting live voice…"
                : "● Live — speak naturally; click Live again to end."
              : ""}
      </div>

      {showLive && live.active && (live.userTranscript || live.assistantTranscript) && (
        <div
          aria-live="polite"
          style={{
            marginTop: 4,
            fontSize: "0.8em",
            color: "var(--fg-muted)",
            display: "flex",
            flexDirection: "column",
            gap: 2,
          }}
        >
          {live.userTranscript && (
            <div>
              <strong style={{ color: "var(--fg)" }}>You:</strong>{" "}
              {live.userTranscript}
            </div>
          )}
          {live.assistantTranscript && (
            <div>
              <strong style={{ color: "var(--fg)" }}>Assistant:</strong>{" "}
              {live.assistantTranscript}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
