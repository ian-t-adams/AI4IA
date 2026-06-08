"use client";

import { useEffect, useRef } from "react";
import type { Message } from "@/lib/types";
import { useSpeechPlayback, type SpeechState } from "@/lib/voice";

interface DisplayMessage {
  id: string;
  role: Message["role"];
  content: string;
  agent?: string | null;
  pending?: boolean;
}

function Bubble({
  msg,
  speechState,
  onToggleSpeak,
}: {
  msg: DisplayMessage;
  speechState: SpeechState;
  onToggleSpeak: (id: string, text: string) => void;
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
        {msg.content}
        {msg.pending && (
          <span aria-label="Generating" style={{ opacity: 0.6 }}>
            ▍
          </span>
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
}: {
  messages: DisplayMessage[];
  onError?: (message: string) => void;
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
          <div>
            <p style={{ fontSize: "1.3em", marginBottom: 8 }}>Start a conversation</p>
            <p>Pick a model, adjust parameters, and send a message.</p>
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
          />
        ))
      )}
      <div ref={endRef} />
    </div>
  );
}

export type { DisplayMessage };
