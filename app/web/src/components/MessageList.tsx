"use client";

// Chat history renderer. Draws user/assistant turns and the artifacts tools
// produce (images, video, documents), speech-playback controls, and citation
// chips that deep-link into library sources. Artifact bytes are fetched lazily
// through the same-origin API proxy, never directly from storage.

import { useEffect, useRef, useState } from "react";
import type { ActivityStep, Message, MessageAttachment } from "@/lib/types";
import { fetchImageArtifact, fetchVideoArtifact, fetchDocumentArtifact } from "@/lib/api";
import { useSpeechPlayback, type SpeechState } from "@/lib/voice";
import { Markdown } from "@/components/Markdown";
import { DOCS_INDEX_URL, STATUS_URL, USER_GUIDE_URL } from "@/lib/docs";

interface DisplayMessage {
  id: string;
  role: Message["role"];
  content: string;
  createdAt?: string;
  agent?: string | null;
  pending?: boolean;
  attachments?: MessageAttachment[];
  source?: Message["source"];
  // Agent activity: streamed live while pending, persisted for the finished turn.
  steps?: ActivityStep[] | null;
}

// A small glyph for a finalized step's outcome (running steps show a spinner).
function stepGlyph(kind: string): string {
  if (kind === "tool_result" || kind === "delegate") return "✓";
  if (kind === "tool_denied") return "⊘";
  if (kind === "tool_error") return "!";
  return "•";
}

// Renders the agent's activity: a live, animated view while the turn runs (the
// current tool spins, finished ones tick off), and a collapsed "Activity" trace
// once complete. Replaces the bare blinking cursor for tool-using turns.
function ActivityPanel({ steps, live }: { steps: ActivityStep[]; live: boolean }) {
  const lastRunning =
    live && steps.length > 0 && steps[steps.length - 1].kind === "tool_start";
  const rows = steps.map((s, i) => {
    const running = live && i === steps.length - 1 && s.kind === "tool_start";
    return (
      <div key={i} className={`activity-row${running ? " running" : ""}`}>
        {running ? (
          <span className="activity-spinner" aria-hidden="true" />
        ) : (
          <span className="activity-glyph" aria-hidden="true">
            {stepGlyph(s.kind)}
          </span>
        )}
        <span className="activity-label">{s.label}</span>
        {s.detail && <span className="activity-detail">{s.detail}</span>}
      </div>
    );
  });

  if (live) {
    return (
      <div className="activity activity-live" aria-live="polite" aria-label="Agent activity">
        {rows}
        {!lastRunning && (
          <div className="activity-row running">
            <span className="activity-spinner" aria-hidden="true" />
            <span className="activity-label">
              {steps.length ? "Composing the answer…" : "Thinking…"}
            </span>
          </div>
        )}
      </div>
    );
  }

  if (steps.length === 0) return null;
  return (
    <details className="activity activity-trace">
      <summary>
        Activity · {steps.length} step{steps.length === 1 ? "" : "s"}
      </summary>
      <div className="activity-rows">{rows}</div>
    </details>
  );
}

// Renders one tool-generated image. The bytes live behind an authenticated
// endpoint (a direct image element would not carry the bearer token), so we fetch
// the blob, wrap it in an object URL, and revoke it on unmount to avoid leaks.
function ImageAttachmentView({ attachment }: { attachment: MessageAttachment }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    fetchImageArtifact(attachment.id)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachment.id]);

  const caption = attachment.prompt?.trim() || "Generated image";

  if (failed) {
    return (
      <div style={{ fontSize: "0.8em", color: "var(--fg-muted)", marginTop: 8 }}>
        (image unavailable)
      </div>
    );
  }
  return (
    <figure style={{ margin: "10px 0 0" }}>
      {url ? (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated blob object URL; next/image adds no value
        <img
          src={url}
          alt={caption}
          style={{
            maxWidth: "100%",
            borderRadius: 10,
            border: "1px solid var(--border)",
            display: "block",
          }}
        />
      ) : (
        <div
          aria-label="Loading image"
          style={{
            width: "100%",
            aspectRatio: "1 / 1",
            maxWidth: 320,
            borderRadius: 10,
            border: "1px solid var(--border)",
            background: "var(--assistant-bubble)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--fg-muted)",
            fontSize: "0.8em",
          }}
        >
          Generating image…
        </div>
      )}
      <figcaption
        style={{ fontSize: "0.72em", color: "var(--fg-muted)", marginTop: 4 }}
      >
        {caption}
        {[
          attachment.model,
          attachment.size,
          attachment.quality && attachment.quality !== "auto"
            ? `${attachment.quality} quality`
            : null,
        ]
          .filter(Boolean)
          .map((part) => ` · ${part}`)
          .join("")}
      </figcaption>
    </figure>
  );
}

// Renders one tool-generated video. Like images, the MP4 bytes live behind an
// authenticated endpoint, so we fetch the blob, wrap it in an object URL for a
// <video controls> element, and revoke it on unmount to avoid leaks.
function VideoAttachmentView({ attachment }: { attachment: MessageAttachment }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;
    fetchVideoArtifact(attachment.id)
      .then((blob) => {
        if (cancelled) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [attachment.id]);

  const caption = attachment.prompt?.trim() || "Generated video";

  if (failed) {
    return (
      <div style={{ fontSize: "0.8em", color: "var(--fg-muted)", marginTop: 8 }}>
        (video unavailable)
      </div>
    );
  }
  return (
    <figure style={{ margin: "10px 0 0" }}>
      {url ? (
        <video
          src={url}
          controls
          playsInline
          style={{
            maxWidth: "100%",
            borderRadius: 10,
            border: "1px solid var(--border)",
            display: "block",
          }}
        />
      ) : (
        <div
          aria-label="Loading video"
          style={{
            width: "100%",
            aspectRatio: "16 / 9",
            maxWidth: 480,
            borderRadius: 10,
            border: "1px solid var(--border)",
            background: "var(--assistant-bubble)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--fg-muted)",
            fontSize: "0.8em",
          }}
        >
          Generating video…
        </div>
      )}
      <figcaption
        style={{ fontSize: "0.72em", color: "var(--fg-muted)", marginTop: 4 }}
      >
        {caption}
        {[
          attachment.model,
          attachment.size,
          attachment.durationSeconds
            ? `${attachment.durationSeconds}s`
            : null,
        ]
          .filter(Boolean)
          .map((part) => ` · ${part}`)
          .join("")}
      </figcaption>
    </figure>
  );
}

// Renders one over-cap process_document result. The markdown text lives behind an
// authenticated endpoint, so we fetch it and show it in a collapsible block with a
// download link. Small results return inline in the message text instead.
function DocumentAttachmentView({ attachment }: { attachment: MessageAttachment }) {
  const [text, setText] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchDocumentArtifact(attachment.id)
      .then((value) => {
        if (!cancelled) setText(value);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [attachment.id]);

  const name = attachment.filename?.trim() || "document";
  const summary = `Processed: ${name}`;

  if (failed) {
    return (
      <div style={{ fontSize: "0.8em", color: "var(--fg-muted)", marginTop: 8 }}>
        (processed document unavailable)
      </div>
    );
  }

  const downloadName = name.toLowerCase().endsWith(".md") ? name : `${name}.md`;

  return (
    <details style={{ margin: "10px 0 0" }}>
      <summary
        style={{
          cursor: "pointer",
          fontSize: "0.8em",
          color: "var(--fg-muted)",
          padding: "6px 10px",
          borderRadius: 10,
          border: "1px solid var(--border)",
          background: "var(--assistant-bubble)",
        }}
      >
        {summary}
        {attachment.model ? ` · ${attachment.model}` : ""}
      </summary>
      {text === null ? (
        <div style={{ fontSize: "0.8em", color: "var(--fg-muted)", marginTop: 6 }}>
          Loading…
        </div>
      ) : (
        <>
          <pre
            style={{
              maxHeight: 360,
              overflow: "auto",
              marginTop: 8,
              padding: "10px 12px",
              borderRadius: 10,
              border: "1px solid var(--border)",
              background: "var(--bg)",
              color: "var(--fg)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontSize: "0.85em",
            }}
          >
            {text}
          </pre>
          <a
            href={`data:text/markdown;charset=utf-8,${encodeURIComponent(text)}`}
            download={downloadName}
            style={{ fontSize: "0.75em", color: "var(--accent)" }}
          >
            Download {downloadName}
          </a>
        </>
      )}
    </details>
  );
}

function Bubble({
  msg,
  speechState,
  onToggleSpeak,
  onCitation,
}: {
  msg: DisplayMessage;
  speechState: SpeechState;
  onToggleSpeak: (id: string, text: string) => void;
  onCitation?: (filename: string, ms: number) => void;
}) {
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";
  if (isSystem) return null;
  const label = isUser ? "You" : "Assistant";
  const speakable = !isUser && !msg.pending && msg.content.trim().length > 0;
  return (
    <div
      style={{
        display: "flex",
        justifyContent: isUser ? "flex-end" : "flex-start",
        padding: "6px 0",
      }}
    >
      <div
        style={{
          maxWidth: "min(720px, 80%)",
          padding: "12px 16px",
          borderRadius: 14,
          background: isUser ? "var(--user-bubble)" : "var(--assistant-bubble)",
          color: isUser ? "var(--user-bubble-fg)" : "var(--assistant-bubble-fg)",
          border: isUser ? "none" : "1px solid var(--border)",
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        <div
          style={{
            fontSize: "0.7em",
            textTransform: "uppercase",
            letterSpacing: 0.6,
            opacity: 0.7,
            marginBottom: 4,
            display: "flex",
            gap: 6,
            alignItems: "center",
            justifyContent: isUser ? "flex-end" : "flex-start",
          }}
        >
          <span>{label}</span>
          {msg.source === "voice" && (
            <span
              title="From a Voice Live conversation"
              aria-label="from voice"
              style={{ textTransform: "none", letterSpacing: 0 }}
            >
              🎧
            </span>
          )}
          {msg.agent && (
            <span
              style={{
                textTransform: "none",
                letterSpacing: 0,
                padding: "1px 6px",
                borderRadius: 999,
                background: "var(--accent)",
                color: "var(--accent-fg)",
                fontWeight: 600,
              }}
            >
              @{msg.agent}
            </span>
          )}
        </div>
        {isUser ? (
          msg.content
        ) : (
          <Markdown content={msg.content} onCitation={onCitation} />
        )}
        {msg.pending ? (
          (msg.steps && msg.steps.length > 0) || msg.content.trim().length === 0 ? (
            <ActivityPanel steps={msg.steps ?? []} live />
          ) : (
            <span aria-label="Generating" style={{ opacity: 0.6 }}>
              ▍
            </span>
          )
        ) : msg.steps && msg.steps.length > 0 ? (
          <ActivityPanel steps={msg.steps} live={false} />
        ) : null}
        {msg.attachments?.map((att) =>
          att.kind === "image" ? (
            <ImageAttachmentView key={att.id} attachment={att} />
          ) : att.kind === "video" ? (
            <VideoAttachmentView key={att.id} attachment={att} />
          ) : att.kind === "document" ? (
            <DocumentAttachmentView key={att.id} attachment={att} />
          ) : null,
        )}
        {speakable && (
          <div style={{ marginTop: 8 }}>
            <button
              type="button"
              onClick={() => onToggleSpeak(msg.id, msg.content)}
              aria-pressed={speechState === "playing"}
              aria-busy={speechState === "busy"}
              aria-label={
                speechState === "playing"
                  ? "Stop reading message aloud"
                  : speechState === "busy"
                    ? "Preparing audio"
                    : "Read message aloud"
              }
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 6,
                padding: "4px 10px",
                borderRadius: 999,
                border: "1px solid var(--border)",
                background:
                  speechState === "playing" ? "var(--accent)" : "transparent",
                color:
                  speechState === "playing"
                    ? "var(--accent-fg)"
                    : "var(--fg-muted)",
                fontSize: "0.78em",
                cursor: speechState === "busy" ? "wait" : "pointer",
              }}
            >
              <span aria-hidden="true">
                {speechState === "playing"
                  ? "■"
                  : speechState === "busy"
                    ? "…"
                    : "▶"}
              </span>
              {speechState === "playing"
                ? "Stop"
                : speechState === "busy"
                  ? "Loading…"
                  : "Speak"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

export function MessageList({
  messages,
  onError,
  onCitation,
}: {
  messages: DisplayMessage[];
  onError?: (message: string) => void;
  onCitation?: (filename: string, ms: number) => void;
}) {
  const endRef = useRef<HTMLDivElement>(null);
  const playback = useSpeechPlayback((msg) => onError?.(msg));

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  return (
    <div
      id="main"
      role="log"
      aria-live="polite"
      aria-label="Conversation"
      style={{
        flex: 1,
        overflowY: "auto",
        padding: "24px max(24px, 6%)",
      }}
    >
      {messages.length === 0 ? (
        <div
          style={{
            height: "100%",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--fg-muted)",
            textAlign: "center",
          }}
        >
          <div style={{ maxWidth: 520 }}>
            <p style={{ fontSize: "1.3em", marginBottom: 8, color: "var(--fg)" }}>
              Start a conversation
            </p>
            <p style={{ marginBottom: 12 }}>
              Type <strong>/</strong> for commands, <strong>@</strong> to call an agent, or
              attach a file to ground the reply. Pick a model only when you need to.
            </p>
            <div
              style={{
                display: "flex",
                gap: 12,
                justifyContent: "center",
                flexWrap: "wrap",
                fontSize: "0.9em",
              }}
            >
              <a
                href={USER_GUIDE_URL}
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--accent)" }}
              >
                User guide
              </a>
              <a
                href={DOCS_INDEX_URL}
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--accent)" }}
              >
                Documentation
              </a>
              <a
                href={STATUS_URL}
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--accent)" }}
              >
                Live status
              </a>
            </div>
          </div>
        </div>
      ) : (
        messages.map((m) => (
          <Bubble
            key={m.id}
            msg={m}
            speechState={
              playback.activeId === m.id
                ? "playing"
                : playback.busyId === m.id
                  ? "busy"
                  : "idle"
            }
            onToggleSpeak={playback.toggle}
            onCitation={onCitation}
          />
        ))
      )}
      <div ref={endRef} />
    </div>
  );
}

export type { DisplayMessage };
