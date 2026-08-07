"use client";

// Chat history renderer. Draws user/assistant turns and the artifacts tools
// produce (images, video, documents), speech-playback controls, and citation
// chips that deep-link into library sources. Artifact bytes are fetched lazily
// through the same-origin API proxy, never directly from storage.

import { useEffect, useRef, useState } from "react";
import type {
  ActivityStep,
  Message,
  MessageAttachment,
  MessageCitation,
  MessageSafety,
  RetrievedSource,
  SafetySignal,
} from "@/lib/types";
import { fetchImageArtifact, fetchVideoArtifact, fetchDocumentArtifact } from "@/lib/api";
import { useSpeechPlayback, type SpeechState } from "@/lib/voice";
import { Markdown, type CitationTarget } from "@/components/Markdown";
import { msToTimecode } from "@/lib/citations";
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
  // Annotate-only content-safety verdicts for the turn.
  safety?: MessageSafety | null;
  // The turn's span registry and the citations checked against it (P1-14).
  // Both absent/null means the turn was never attested.
  sources?: RetrievedSource[] | null;
  citations?: MessageCitation[] | null;
}

// Human-readable names for the categories Foundry reports. Unknown categories
// fall back to their raw key rather than being hidden, so a newly added filter
// still surfaces.
const SAFETY_LABELS: Record<string, string> = {
  hate: "Hate",
  sexual: "Sexual",
  selfharm: "Self-harm",
  self_harm: "Self-harm",
  violence: "Violence",
  jailbreak: "Jailbreak attempt",
  protected_material_text: "Protected material (text)",
  protected_material_code: "Protected material (code)",
};

function safetyLabel(category: string): string {
  return SAFETY_LABELS[category] ?? category.replace(/_/g, " ");
}

function isNotable(s: SafetySignal): boolean {
  if (s.filtered) return true;
  if (s.detected != null) return s.detected;
  return s.severity != null && s.severity !== "safe";
}

// Renders the content-safety verdicts for a turn.
//
// Every model runs under an annotate-only policy: the filters are enabled but
// never block. That makes this panel the ONLY place the safety system is
// observable — without it the filters run on every turn and no one can see or
// judge what they found. It is deliberately descriptive: it reports what the
// platform observed and never implies the answer was withheld or altered.
function SafetyPanel({ safety }: { safety: MessageSafety }) {
  const signals = safety.signals ?? [];
  if (signals.length === 0) return null;

  const notable = signals.filter(isNotable);
  const summary =
    notable.length > 0
      ? `Content safety · ${notable.length} flagged`
      : "Content safety · nothing flagged";

  const rows = [...signals]
    .sort((a, b) => Number(isNotable(b)) - Number(isNotable(a)))
    .map((s, i) => {
      const verdict =
        s.detected != null
          ? s.detected
            ? "detected"
            : "not detected"
          : (s.severity ?? "unknown");
      return (
        <div key={i} className={`safety-row${isNotable(s) ? " flagged" : ""}`}>
          {/* Never colour alone: the flagged state is carried by text too. */}
          <span className="safety-glyph" aria-hidden="true">
            {isNotable(s) ? "▲" : "•"}
          </span>
          <span className="safety-label">{safetyLabel(s.category)}</span>
          <span className="safety-scope">
            {s.scope === "prompt" ? "your message" : "the reply"}
          </span>
          <span className="safety-verdict">{verdict}</span>
        </div>
      );
    });

  return (
    <details className="activity activity-trace safety-trace">
      <summary>{summary}</summary>
      <div className="activity-rows">
        <p className="safety-note">
          These are advisory labels from the model platform. Nothing was blocked
          or rewritten — the full response is shown as generated.
        </p>
        {rows}
      </div>
    </details>
  );
}

// The turn's retrieval receipt. Lists every span that was injected — the set the
// answer *could* have cited — with the excerpt exactly as the model saw it, so a
// reader can judge whether a cited span actually supports the sentence. The app
// deliberately does not make that judgement for them: checking that a span
// entails a claim is entailment, every cheap inline approximation of it would be
// wrong some of the time, and a badge that is wrong some of the time is worse
// than no badge (audit P1-14).
//
// Rendered only for an attested turn. An unattested one has no registry, so
// there is nothing to show and nothing is implied by its absence.
function SourcesPanel({
  sources,
  citations,
}: {
  sources: RetrievedSource[];
  citations?: MessageCitation[] | null;
}) {
  if (sources.length === 0) return null;
  const cited = new Set(
    (citations ?? [])
      .filter((c) => c.status === "verified")
      .map((c) => c.spanId),
  );
  const unverified = (citations ?? []).filter((c) => c.status === "unverified");
  const summary =
    `Sources · ${sources.length} retrieved` +
    (cited.size > 0 ? `, ${cited.size} cited` : ", none cited") +
    (unverified.length > 0 ? `, ${unverified.length} unverified` : "");
  return (
    <details className="activity activity-trace">
      <summary>{summary}</summary>
      <div className="activity-rows">
        <p className="safety-note">
          These are the excerpts retrieved for this answer, shown as the model
          received them. A cited excerpt is one the answer referred to by id — it
          is not a check that the excerpt supports what was written.
        </p>
        {unverified.length > 0 && (
          <p className="safety-note" style={{ color: "var(--danger)" }}>
            {unverified.length === 1
              ? "One citation in this answer names a source that was not retrieved."
              : `${unverified.length} citations in this answer name sources that were not retrieved.`}
          </p>
        )}
        {sources.map((source) => {
          const ground = [
            source.heading,
            typeof source.startMs === "number"
              ? msToTimecode(source.startMs)
              : null,
            source.speaker,
          ].filter(Boolean);
          return (
            <div key={source.spanId} className="activity-row">
              {/* Never colour alone: the cited state is carried by text too.
                  One text node, so the whole label is assertable as written. */}
              <span className="activity-label">
                {`${source.spanId} · ${source.filename}` +
                  (ground.length > 0 ? ` · ${ground.join(" · ")}` : "") +
                  (cited.has(source.spanId) ? " · cited" : " · not cited")}
              </span>
              <span className="activity-detail">
                {source.excerpt}
                {source.excerptTruncated ? "…" : ""}
              </span>
            </div>
          );
        })}
      </div>
    </details>
  );
}

// A small glyph for a finalized step's outcome (running steps show a spinner).function stepGlyph(kind: string): string {
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
  onCitation?: (target: CitationTarget) => void;
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
          <Markdown
            content={msg.content}
            onCitation={onCitation}
            sources={msg.sources}
          />
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
        {/* Annotate-only safety verdicts, shown once the turn is settled so a
            partial verdict is never presented as final. */}
        {!msg.pending && msg.safety ? <SafetyPanel safety={msg.safety} /> : null}
        {/* The turn's retrieval receipt, shown once settled so a partial
            registry is never presented as the whole of what was retrieved. */}
        {!msg.pending && msg.sources && msg.sources.length > 0 ? (
          <SourcesPanel sources={msg.sources} citations={msg.citations} />
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
  onCitation?: (target: CitationTarget) => void;
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
