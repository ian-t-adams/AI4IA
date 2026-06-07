"use client";

import { useEffect, useRef } from "react";
import type { Message } from "@/lib/types";

interface DisplayMessage {
  id: string;
  role: Message["role"];
  content: string;
  pending?: boolean;
}

function Bubble({ msg }: { msg: DisplayMessage }) {
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";
  if (isSystem) return null;
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
          }}
        >
          {isUser ? "You" : "Assistant"}
        </div>
        {msg.content}
        {msg.pending && (
          <span aria-label="Generating" style={{ opacity: 0.6 }}>
            ▍
          </span>
        )}
      </div>
    </div>
  );
}

export function MessageList({ messages }: { messages: DisplayMessage[] }) {
  const endRef = useRef<HTMLDivElement>(null);

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
        messages.map((m) => <Bubble key={m.id} msg={m} />)
      )}
      <div ref={endRef} />
    </div>
  );
}

export type { DisplayMessage };
