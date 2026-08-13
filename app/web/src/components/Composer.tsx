"use client";

// Message composer and attachment control. Handles text input with IME
// composition, @-mention agent selection and /-command menus (see lib/commands.ts),
// per-session and library document uploads, and Voice Live. Slash-command and
// agent hints are advisory; the backend re-validates every tool call at execution.

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type {
  AgentSummary,
  AttachmentCapabilities,
  DocumentSummary,
} from "@/lib/types";
import type { LibraryDocument } from "@/lib/library";
import { SLASH_COMMANDS, type SlashCommand } from "@/lib/commands";

export interface UploadItem {
  id: string;
  filename: string;
  sessionId?: string | null;
  status: "queued" | "uploading" | "associating" | "failed";
  error?: string;
}

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
  stored: "var(--info)",
  analyzing: "var(--info)",
  ready: "var(--success)",
  failed: "var(--danger)",
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
  capabilities,
  capabilitiesError,
  uploads = [],
  onRetryUpload,
  onDismissUpload,
  onRetryCapabilities,
  onSend,
  onStop,
  onUpload,
  onRemoveDocument,
  onRemoveLibraryDocument,
  onError,
  voiceLive,
  prefill,
}: {
  disabled: boolean;
  streaming: boolean;
  agents: AgentSummary[];
  documents: DocumentSummary[];
  libraryDocuments?: LibraryDocument[];
  uploading: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  capabilities: AttachmentCapabilities | null;
  capabilitiesError?: string | null;
  uploads?: UploadItem[];
  onUpload: (file: File) => Promise<void>;
  onRetryUpload?: (id: string) => void;
  onDismissUpload?: (id: string) => void;
  onRetryCapabilities?: () => void;
  onRemoveDocument: (id: string) => void;
  onRemoveLibraryDocument?: (id: string) => void;
  onError?: (message: string) => void;
  prefill?: { id: number; text: string } | null;
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
  const [highlightState, setHighlightState] = useState({
    key: "",
    index: 0,
  });
  // Set when the user dismisses the menu with Escape; cleared on the next edit.
  const [suppressed, setSuppressed] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  // Hidden file input driven by the attach button.
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Caret position to restore after a programmatic value change (insertion).
  const pendingCaret = useRef<number | null>(null);
  const appliedPrefillId = useRef<number | null>(null);

  useEffect(() => {
    if (!prefill || appliedPrefillId.current === prefill.id) return;
    appliedPrefillId.current = prefill.id;
    setText((current) => {
      const next = current.startsWith(prefill.text)
        ? current
        : `${prefill.text}${current}`;
      pendingCaret.current = next.length;
      setCaret(next.length);
      setSuppressed(false);
      return next;
    });
  }, [prefill]);

  const resizeTextarea = useCallback(() => {
    const element = textareaRef.current;
    if (!element) return;
    const computed = window.getComputedStyle(element);
    const lineHeight = Number.parseFloat(computed.lineHeight) || 24;
    const padding =
      (Number.parseFloat(computed.paddingTop) || 0) +
      (Number.parseFloat(computed.paddingBottom) || 0);
    const minHeight = 64;
    const maxHeight = lineHeight * 8 + padding;
    element.style.height = "0px";
    const height = Math.min(maxHeight, Math.max(minHeight, element.scrollHeight));
    element.style.height = `${height}px`;
    element.style.overflowY = element.scrollHeight > maxHeight ? "auto" : "hidden";
  }, []);

  // The attach control tracks whichever doc set is in use this view: the
  // session-scoped docs (flag off) or the library chips (flag on). They are
  // mutually exclusive in practice, so a combined count gives the right cap.
  const totalDocs = documents.length + libraryDocuments.length;
  const maxDocuments = capabilities?.maxPerSessionDocuments ?? 0;
  const atDocLimit = maxDocuments > 0 && totalDocs >= maxDocuments;
  const accept = capabilities
    ? [...capabilities.extensions, ...capabilities.mimeTypes].join(",")
    : "";

  const onPickFiles = async (files: FileList | null) => {
    if (!files || files.length === 0) return;
    if (!capabilities) {
      onError?.("Attachment capabilities are still loading.");
      return;
    }
    const remaining =
      maxDocuments > 0 ? Math.max(0, maxDocuments - totalDocs) : files.length;
    if (remaining <= 0) {
      onError?.(`You can attach at most ${maxDocuments} files here.`);
      return;
    }
    const allowedExtensions = new Set(
      capabilities.extensions.map((value) => value.toLowerCase()),
    );
    for (const file of Array.from(files).slice(0, remaining)) {
      const extension = file.name.includes(".")
        ? `.${file.name.split(".").pop()?.toLowerCase()}`
        : "";
      const mimeAllowed = capabilities.mimeTypes.some((value) =>
        value.endsWith("/*")
          ? file.type.startsWith(value.slice(0, -1))
          : file.type === value,
      );
      if (file.size > capabilities.maxBytes) {
        onError?.(`${file.name} exceeds the ${formatBytes(capabilities.maxBytes)} limit.`);
        continue;
      }
      if (!allowedExtensions.has(extension) && !mimeAllowed) {
        onError?.(`${file.name} is not supported by this environment.`);
        continue;
      }
      try {
        await onUpload(file);
      } catch (reason) {
        onError?.(
          `${file.name} could not be uploaded: ${
            reason instanceof Error ? reason.message : "Unknown upload error"
          }`,
        );
      }
    }
  };

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
  const highlightKey =
    menuMode === "mention"
      ? `mention:${mention?.query ?? ""}:${agentOptions.map((agent) => agent.name).join(",")}`
      : menuMode === "command"
        ? `command:${command?.query ?? ""}:${commandOptions.map((item) => item.name).join(",")}`
        : "";
  const highlight =
    highlightState.key === highlightKey ? highlightState.index : 0;
  const updateHighlight = (update: (index: number) => number) => {
    setHighlightState((current) => ({
      key: highlightKey,
      index: update(current.key === highlightKey ? current.index : 0),
    }));
  };

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

  useLayoutEffect(() => {
    resizeTextarea();
  }, [resizeTextarea, text]);

  useEffect(() => {
    const element = textareaRef.current;
    if (!element) return;
    if (typeof ResizeObserver === "undefined") {
      window.addEventListener("resize", resizeTextarea);
      return () => window.removeEventListener("resize", resizeTextarea);
    }
    const observer = new ResizeObserver(resizeTextarea);
    observer.observe(element);
    return () => observer.disconnect();
  }, [resizeTextarea]);

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
        updateHighlight((index) => (index + 1) % optionCount);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        updateHighlight((index) => (index - 1 + optionCount) % optionCount);
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
      className="composer-shell"
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
            {totalDocs} attached ·{" "}
            {maxDocuments > 0 ? `${maxDocuments} max` : "server-managed limit"} ·{" "}
            {capabilities?.ingestPath === "library"
              ? "Content Understanding library"
              : "session context"}
          </li>
        </ul>
      )}

      <div
        className="composer-row"
        style={{
          display: "flex",
          flexWrap: "wrap",
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
                    onMouseEnter={() =>
                      setHighlightState({ key: highlightKey, index: i })
                    }
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
                    onMouseEnter={() =>
                      setHighlightState({ key: highlightKey, index: i })
                    }
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
          accept={accept}
          className="visually-hidden"
          aria-hidden="true"
          tabIndex={-1}
          onChange={(e) => {
            void onPickFiles(e.target.files);
            // Reset so re-selecting the same file fires onChange again.
            e.target.value = "";
          }}
        />
        <button
          type="button"
          className="composer-icon-button composer-attach-button"
          onClick={() => fileInputRef.current?.click()}
          disabled={!capabilities || uploading || atDocLimit}
          aria-busy={uploading}
          aria-label={
            atDocLimit
              ? `Attachment limit reached (${maxDocuments})`
              : !capabilities
                ? capabilitiesError
                  ? "Attachments unavailable"
                  : "Loading attachment capabilities"
                : uploading
                ? "Uploading document"
                : "Attach files"
          }
          title={
            atDocLimit
              ? `You can attach at most ${maxDocuments} files here`
              : !capabilities
                ? capabilitiesError ?? "Loading attachment capabilities"
                : uploading
                ? "Uploading…"
                : capabilities
                  ? `Attach ${capabilities.modalities.join(", ")} files up to ${formatBytes(capabilities.maxBytes)}`
                  : "Loading attachment capabilities"
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
            cursor: !capabilities || uploading || atDocLimit ? "not-allowed" : "pointer",
            opacity: !capabilities || uploading || atDocLimit ? 0.45 : 1,
          }}
        >
          {uploading ? "…" : "📎"}
        </button>
        {!capabilities ? (
          <div
            className={capabilitiesError ? "inspector-error" : "inspector-empty"}
            role={capabilitiesError ? "alert" : "status"}
          >
            {capabilitiesError
              ? `Attachments unavailable: ${capabilitiesError}`
              : "Loading attachment capabilities…"}
            {capabilitiesError ? (
              <button type="button" onClick={onRetryCapabilities}>Retry</button>
            ) : null}
          </div>
        ) : null}
        <div className="upload-status-list" aria-live="polite">
            {uploads.map((upload) => (
              <div key={upload.id} className="upload-status-row">
                <span>
                  {upload.filename} · {upload.status}
                  {upload.error ? ` · ${upload.error}` : ""}
                </span>
                {upload.status === "failed" ? (
                  <>
                    <button
                      type="button"
                      aria-label={`Retry upload ${upload.filename}`}
                      onClick={() => onRetryUpload?.(upload.id)}
                    >
                      Retry
                    </button>
                    <button
                      type="button"
                      aria-label={`Dismiss failed upload ${upload.filename}`}
                      onClick={() => onDismissUpload?.(upload.id)}
                    >
                      Dismiss
                    </button>
                  </>
                ) : null}
              </div>
            ))}
        </div>

        {voiceLive && (
          <button
            type="button"
            className="composer-icon-button composer-voice-button"
            onClick={voiceLive.active ? voiceLive.stop : voiceLive.start}
            disabled={
              !voiceLive.supported ||
              (!voiceLive.active && (voiceLive.saving || voiceLive.saveBlocked))
            }
            aria-pressed={voiceLive.active}
            aria-busy={
              voiceLive.connecting || voiceLive.ending || voiceLive.saving
            }
            aria-label={
              // `active` (covers connecting and live) must always resolve to
              // the Stop label/action first: whatever saving/saveBlocked say
              // about a *previous* cycle can never leave the current, live
              // session without a way to stop it.
              voiceLive.active
                ? "Stop live voice conversation"
                : voiceLive.saveBlocked
                  ? "Retry saving the voice transcript below"
                  : voiceLive.saving
                    ? "Saving live voice transcript"
                    : voiceLive.retrying
                      ? "Retry live voice conversation"
                      : "Start live voice conversation"
            }
            title={
              !voiceLive.supported
                ? "Live voice isn't supported in this browser"
                : voiceLive.active
                  ? "Stop Voice Live"
                  : voiceLive.saveBlocked
                    ? "Save the previous Voice Live transcript before starting again"
                    : "Start Voice Live in this chat"
            }
            style={{
              alignSelf: "stretch",
              minHeight: 46,
              padding: "0 14px",
              borderRadius: 10,
              border: `1px solid ${voiceLive.active ? "var(--danger)" : "var(--accent)"}`,
              background: voiceLive.active ? "var(--danger)" : "var(--accent)",
              color: voiceLive.active ? "var(--danger-fg)" : "var(--accent-fg)",
              fontSize: "1.15em",
              lineHeight: 1,
              cursor:
                !voiceLive.supported ||
                (!voiceLive.active && (voiceLive.saving || voiceLive.saveBlocked))
                  ? "not-allowed"
                  : "pointer",
              opacity: voiceLive.supported ? 1 : 0.45,
            }}
          >
            {voiceLive.active
              ? voiceLive.connecting
                ? "…"
                : "■"
              : voiceLive.saveBlocked
                ? "!"
                : voiceLive.saving
                  ? "…"
                  : "🎤"}
          </button>
        )}

        <label htmlFor="composer" className="visually-hidden">
          Message
        </label>
        <textarea
          id="composer"
          className="composer-textarea"
          ref={textareaRef}
          value={text}
          rows={2}
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
        />
        {streaming ? (
          <button
            type="button"
            className="composer-submit-button"
            onClick={onStop}
            style={{
              padding: "12px 18px",
              borderRadius: 10,
              border: "1px solid var(--border)",
              background: "var(--danger)",
              color: "var(--danger-fg)",
              fontWeight: 600,
            }}
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            className="composer-submit-button"
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
