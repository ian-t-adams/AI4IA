"use client";

// Message composer and attachment control. Handles text input with IME
// composition, @-mention agent selection and /-command menus (see lib/commands.ts),
// per-session and library document uploads, and voice dictation. Slash-command and
// agent hints are advisory; the backend re-validates every tool call at execution.

import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { AgentSummary, DocumentSummary } from "@/lib/types";
import type { LibraryDocument } from "@/lib/library";
import { SLASH_COMMANDS, type SlashCommand } from "@/lib/commands";
import { useVoiceRecorder } from "@/lib/voice";

// Mirrors the backend cap (routers/documents.py MAX_DOCS_PER_SESSION).
const MAX_DOCS = 8;
// Hint shown next to the file control. The plain-text preview of an attachment is
// injected into chat context up to ~12K chars/turn; heavier or binary files
// (PDFs, spreadsheets, images) can instead be cracked/analyzed in a sandbox when
// the agent needs their real layout/cells/pixels.
const DOC_BUDGET_HINT =
  "text up to ~12K chars/turn; PDFs, sheets & images analyzed in a sandbox";
// Hint for the file picker; the backend accepts the text family + pdf/docx/pptx.
const FILE_ACCEPT =
  ".txt,.md,.markdown,.csv,.tsv,.json,.log,.xml,.yaml,.yml,.html,.htm,.pdf,.docx,.pptx,text/plain,application/pdf";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Ingest-status pill labels/colours for library doc chips (mirrors LibraryPanel).
const LIB_STATUS_LABEL: Record<LibraryDocument["status"], string> = {
  pending: "Queued",
  stored: "Analyzing…",
  analyzing: "Analyzing…",
  ready: "Ready",
  failed: "Failed",
};

const LIB_STATUS_COLOR: Record<LibraryDocument["status"], string> = {
  pending: "var(--fg-muted)",
  stored: "#0e7490",
  analyzing: "#0e7490",
  ready: "#15803d",
  failed: "#b91c1c",
};

// An active mention/command being typed at the START of the message (ignoring
// leading whitespace), since the backend only routes a mention or command at the
// start of a turn. The same shape backs both the "@" agent menu and the "/"
// command menu.
interface ActiveToken {
  start: number; // index of the '@' or '/'
  end: number; // caret position
  query: string; // text after the sigil, lowercased
}

const MENTION_RE = /^(\s*)@([A-Za-z0-9_.-]*)$/;
const SLASH_RE = /^(\s*)\/([A-Za-z0-9_.-]*)$/;
const MAX_OPTIONS = 8;

function detectMention(value: string, caret: number): ActiveToken | null {
  const prefix = value.slice(0, caret);
  const m = prefix.match(MENTION_RE);
  if (!m) return null;
  return { start: m[1].length, end: caret, query: m[2].toLowerCase() };
}

function detectCommand(value: string, caret: number): ActiveToken | null {
  const prefix = value.slice(0, caret);
  const m = prefix.match(SLASH_RE);
  if (!m) return null;
  return { start: m[1].length, end: caret, query: m[2].toLowerCase() };
}

type MenuMode = "mention" | "command";

export function Composer({
  disabled,
  streaming,
  agents,
  documents,
  libraryDocuments = [],
  uploading,
  onSend,
  onStop,
  onUpload,
  onRemoveDocument,
  onRemoveLibraryDocument,
  onError,
  voiceLive,
}: {
  disabled: boolean;
  streaming: boolean;
  agents: AgentSummary[];
  documents: DocumentSummary[];
  libraryDocuments?: LibraryDocument[];
  uploading: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  onUpload: (file: File) => void;
  onRemoveDocument: (id: string) => void;
  onRemoveLibraryDocument?: (id: string) => void;
  onError?: (message: string) => void;
  // Inline Voice Live controller. Unlike dictation, this starts/stops the
  // realtime conversation without leaving or covering the chat.
  voiceLive?: {
    active: boolean;
    supported: boolean;
    connecting: boolean;
    ending: boolean;
    saving: boolean;
    saveBlocked: boolean;
    retrying: boolean;
    start: () => void;
    stop: () => void;
  };
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

  // The attach control tracks whichever doc set is in use this view: the
  // session-scoped docs (flag off) or the library chips (flag on). They are
  // mutually exclusive in practice, so a combined count gives the right cap.
  const totalDocs = documents.length + libraryDocuments.length;
  const atDocLimit = totalDocs >= MAX_DOCS;

  const onPickFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    // Upload sequentially (the parent dedupes the lazy session creation); the
    // backend enforces the real per-session cap and rejects extras.
    const remaining = MAX_DOCS - totalDocs;
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

  const enabledAgents = useMemo(
    () => agents.filter((a) => a.enabled),
    [agents],
  );

  const mention = useMemo(() => detectMention(text, caret), [text, caret]);
  const command = useMemo(() => detectCommand(text, caret), [text, caret]);

  const agentOptions = useMemo(() => {
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

  const commandOptions = useMemo(() => {
    if (!command) return [];
    const q = command.query;
    const matches = SLASH_COMMANDS.filter(
      (c) =>
        q === "" ||
        c.name.toLowerCase().startsWith(q) ||
        c.label.toLowerCase().startsWith(q),
    );
    return matches.slice(0, MAX_OPTIONS);
  }, [command]);

  // A "@" mention and a "/" command are mutually exclusive (the regexes anchor on
  // different sigils at the start), so at most one menu is active per keystroke.
  const menuMode: MenuMode | null =
    mention && agentOptions.length > 0
      ? "mention"
      : command && commandOptions.length > 0
        ? "command"
        : null;
  const optionCount =
    menuMode === "mention"
      ? agentOptions.length
      : menuMode === "command"
        ? commandOptions.length
        : 0;
  const menuOpen = menuMode !== null && !suppressed;

  // Keep the highlight in range as the active list changes.
  useEffect(() => {
    setHighlight(0);
  }, [mention?.query, command?.query, optionCount]);

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

  const acceptCommand = (cmd: SlashCommand) => {
    if (!command) return;
    const suffix = text.slice(command.end);
    // Trailing space (when there isn't one already) both readies any arguments
    // and closes the menu, since "/name " no longer matches SLASH_RE.
    const insert = suffix.startsWith(" ") ? `/${cmd.name}` : `/${cmd.name} `;
    const next = text.slice(0, command.start) + insert + suffix;
    const pos = command.start + insert.length;
    pendingCaret.current = pos;
    setText(next);
    setSuppressed(false);
  };

  const acceptHighlighted = () => {
    const idx = Math.min(highlight, optionCount - 1);
    if (menuMode === "mention") {
      const a = agentOptions[idx];
      if (a) acceptAgent(a);
    } else if (menuMode === "command") {
      const c = commandOptions[idx];
      if (c) acceptCommand(c);
    }
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
        setHighlight((h) => (h + 1) % optionCount);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlight((h) => (h - 1 + optionCount) % optionCount);
        return;
      }
      if (e.key === "Enter" || e.key === "Tab") {
        e.preventDefault();
        acceptHighlighted();
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

  const highlightedIndex = Math.min(highlight, optionCount - 1);
  const activeOptionId = !menuOpen
    ? undefined
    : menuMode === "mention"
      ? `agent-option-${agentOptions[highlightedIndex]?.name}`
      : `command-option-${commandOptions[highlightedIndex]?.name}`;

  return (
    <div
      style={{
        borderTop: "1px solid var(--border)",
        padding: "12px max(16px, 6%)",
        background: "var(--bg-elevated)",
      }}
    >
      {(documents.length > 0 || libraryDocuments.length > 0) && (
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
          {libraryDocuments.map((d) => {
            const label = LIB_STATUS_LABEL[d.status];
            const color = LIB_STATUS_COLOR[d.status];
            return (
              <li
                key={d.id}
                title={`${d.filename} · ${formatBytes(d.size)} · ${label}${
                  d.status === "ready" && d.chunkCount > 0
                    ? ` · ${d.chunkCount} chunks`
                    : ""
                }`}
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 8,
                  maxWidth: 300,
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
                <span
                  aria-label={`Status: ${label}`}
                  style={{
                    flexShrink: 0,
                    fontSize: "0.85em",
                    fontWeight: 600,
                    color,
                    whiteSpace: "nowrap",
                  }}
                >
                  {label}
                </span>
                <button
                  type="button"
                  onClick={() => onRemoveLibraryDocument?.(d.id)}
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
            );
          })}
          <li
            style={{
              alignSelf: "center",
              fontSize: "0.72em",
              color: "var(--fg-muted)",
            }}
          >
            {totalDocs}/{MAX_DOCS} ·{" "}
            {libraryDocuments.length > 0
              ? "ingested to your library"
              : DOC_BUDGET_HINT}
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
            id="composer-autocomplete-menu"
            role="listbox"
            aria-label={menuMode === "mention" ? "Agents" : "Commands"}
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
            {menuMode === "mention"
              ? agentOptions.map((a, i) => (
                  <li
                    key={a.name}
                    id={`agent-option-${a.name}`}
                    role="option"
                    aria-selected={i === highlightedIndex}
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
                        i === highlightedIndex ? "var(--accent)" : "transparent",
                      color:
                        i === highlightedIndex ? "var(--accent-fg)" : "var(--fg)",
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
                ))
              : commandOptions.map((c, i) => (
                  <li
                    key={c.name}
                    id={`command-option-${c.name}`}
                    role="option"
                    aria-selected={i === highlightedIndex}
                    onMouseDown={(e) => {
                      e.preventDefault();
                      acceptCommand(c);
                    }}
                    onMouseEnter={() => setHighlight(i)}
                    style={{
                      padding: "8px 10px",
                      borderRadius: 8,
                      cursor: "pointer",
                      background:
                        i === highlightedIndex ? "var(--accent)" : "transparent",
                      color:
                        i === highlightedIndex ? "var(--accent-fg)" : "var(--fg)",
                    }}
                  >
                    <div style={{ fontWeight: 600 }}>
                      /{c.name}
                      <span style={{ opacity: 0.7, fontWeight: 400 }}>
                        {" "}
                        · {c.label}
                      </span>
                    </div>
                    <div style={{ fontSize: "0.8em", opacity: 0.75 }}>
                      {c.hint}
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
          disabled={
            !voice.supported || voice.transcribing || Boolean(voiceLive?.active)
          }
          aria-pressed={voice.recording}
          aria-busy={voice.transcribing}
          aria-label={
            voiceLive?.active
              ? "Voice dictation unavailable while Voice Live is active"
              : voice.transcribing
              ? "Transcribing audio"
              : voice.recording
                ? "Stop recording"
                : "Record a voice message"
          }
          title={
            voiceLive?.active
              ? "Stop Voice Live before recording a dictated message"
              : !voice.supported
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
              !voice.supported || voice.transcribing || voiceLive?.active
                ? "not-allowed"
                : "pointer",
            opacity: voice.supported && !voiceLive?.active ? 1 : 0.45,
          }}
        >
          {voice.transcribing ? "…" : voice.recording ? "■" : "🎙"}
        </button>

        {voiceLive && (
          <button
            type="button"
            onClick={voiceLive.active ? voiceLive.stop : voiceLive.start}
            disabled={
              !voiceLive.supported || voiceLive.ending || voiceLive.saving
                || voiceLive.saveBlocked || voice.recording || voice.transcribing
            }
            aria-pressed={voiceLive.active}
            aria-busy={
              voiceLive.connecting || voiceLive.ending || voiceLive.saving
            }
            aria-label={
              voice.recording || voice.transcribing
                ? "Stop voice dictation before starting live voice"
                : voiceLive.saveBlocked
                ? "Retry saving the voice transcript below"
                : voiceLive.saving
                ? "Saving live voice transcript"
                : voiceLive.active
                ? "Stop live voice conversation"
                : voiceLive.retrying
                  ? "Retry live voice conversation"
                  : "Start live voice conversation"
            }
            title={
              !voiceLive.supported
                ? "Live voice isn't supported in this browser"
                : voice.recording || voice.transcribing
                  ? "Stop voice dictation before starting Voice Live"
                : voiceLive.saveBlocked
                  ? "Save the previous Voice Live transcript before starting again"
                : voiceLive.active
                  ? "Stop Voice Live"
                  : "Start Voice Live in this chat"
            }
            style={{
              alignSelf: "stretch",
              minHeight: 46,
              padding: "0 14px",
              borderRadius: 10,
              border: `1px solid ${voiceLive.active ? "var(--danger)" : "var(--accent)"}`,
              background: voiceLive.active ? "var(--danger)" : "var(--accent)",
              color: voiceLive.active ? "#fff" : "var(--accent-fg)",
              fontSize: "1.15em",
              lineHeight: 1,
              cursor:
                !voiceLive.supported ||
                voiceLive.ending ||
                voiceLive.saving ||
                voiceLive.saveBlocked ||
                voice.recording ||
                voice.transcribing
                  ? "not-allowed"
                  : "pointer",
              opacity: voiceLive.supported ? 1 : 0.45,
            }}
          >
            {voiceLive.saveBlocked
              ? "!"
              : voiceLive.connecting || voiceLive.saving
              ? "…"
              : voiceLive.active
                ? "■"
                : "🎤"}
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
          placeholder="Send a message…  (Enter to send, Shift+Enter for newline, @ to mention an agent, / for commands)"
          role="combobox"
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-expanded={menuOpen}
          aria-controls="composer-autocomplete-menu"
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
            : ""}
      </div>
    </div>
  );
}
