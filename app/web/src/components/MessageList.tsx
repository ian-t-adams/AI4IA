"use client";

// Chat history renderer. Draws user/assistant turns and the artifacts tools
// produce (images, video, documents), speech-playback controls, and citation
// chips that deep-link into library sources. Artifact bytes are fetched lazily
// through the same-origin API proxy, never directly from storage.

import { useEffect, useRef, useState } from "react";
import type {
  ActivityStep,
  ExecutionReceipt,
  Message,
  MessageAttachment,
  MessageCitation,
  MessageSafety,
  RetrievedSource,
  SafetySignal,
} from "@/lib/types";
import { SAFETY_MAX_SEVERITY_LEVEL } from "@/lib/types";
import { fetchImageArtifact, fetchVideoArtifact, fetchDocumentArtifact } from "@/lib/api";
import { useSpeechPlayback, type SpeechState } from "@/lib/voice";
import { ActivityPanel, ExecutionReceiptPanel, WorkflowStepReceiptPanels } from "./ExecutionEvidence";
import { MemoryProvenance } from "./MemoryProvenance";
import { Markdown, type CitationTarget } from "@/components/Markdown";
import { msToTimecode } from "@/lib/citations";
import { DOCS_INDEX_URL, STATUS_URL, USER_GUIDE_URL } from "@/lib/docs";
import { DialogFrame } from "./DialogFrame";
import { Icon, type IconName } from "./Icon";

export interface EmptyAction {
  label: string;
  icon: IconName;
  onClick: () => void;
}

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
  // What was supplied to the model and what it was allowed to do (see
  // ai4ia_api.receipts). Absent on turns that predate the feature.
  executionReceipt?: ExecutionReceipt | null;
  workflowStepReceipts?: ExecutionReceipt[] | null;
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

// One signal's verdict as a sentence fragment.
//
// The provider's own severity string is always shown first and never replaced.
// The normalized ordinal is appended only when the server ranked it, so
// "medium" reads as "medium (level 2 of 3)" — a scale a reader can act on —
// while an unrecognized provider value is still shown verbatim rather than
// being forced onto a scale it may not belong to.
function safetyVerdict(s: SafetySignal): string {
  if (s.detected != null) return s.detected ? "detected" : "not detected";
  const severity = s.severity ?? "unknown";
  if (typeof s.severityLevel !== "number") return severity;
  return `${severity} (level ${s.severityLevel} of ${SAFETY_MAX_SEVERITY_LEVEL})`;
}

// Renders the content-safety verdicts for a turn.
//
// Supported Azure OpenAI surfaces run under an annotate-only policy: configured
// filters assess but never block. This panel is deliberately descriptive: it
// reports what the platform returned and never implies an unavailable assessment
// was clean or that provider-native behavior was bypassed.
//
// It also renders the case where there is nothing to report. A turn with no
// assessment used to render no panel, which to a reader is indistinguishable
// from a clean result — so an `unavailable` record says so in words instead.
// Nothing here invents a verdict for a turn that was never assessed.
function SafetyPanel({ safety }: { safety: MessageSafety }) {
  const signals = safety.signals ?? [];
  // Rows written before coverage was recorded carry signals and no status, so
  // treating a missing status as "reported" preserves their original meaning.
  const status = safety.status ?? "reported";

  if (status === "unavailable" || signals.length === 0) {
    return (
      <details className="activity activity-trace safety-trace">
        <summary>Content safety · not assessed</summary>
        <div className="activity-rows">
          <p className="safety-note">
            No platform guardrail assessment was returned for this turn
            {safety.provider ? ` by ${safety.provider}` : ""}. That is not a
            verdict: nothing was checked and found clean. AI4IA did not add an
            application-level block or rewrite; provider-native enforcement may
            still apply.
            {(safety.errors ?? []).length
              ? ` Filter errors: ${(safety.errors ?? []).join(", ")}.`
              : ""}
          </p>
        </div>
      </details>
    );
  }

  const notable = signals.filter(isNotable);
  const providerFiltered = signals.some((signal) => signal.filtered);
  const coverage = safety.coverage ?? [];
  const summary =
    notable.length > 0
      ? `Content safety · ${notable.length} flagged`
      : safety.truncated
        ? "Content safety · no flags in shown assessments"
        : "Content safety · nothing flagged";
  const assessmentErrors = safety.errors ?? [];

  const rows = [...signals]
    .sort((a, b) => Number(isNotable(b)) - Number(isNotable(a)))
    .map((s, i) => (
      <div key={i} className={`safety-row${isNotable(s) ? " flagged" : ""}`}>
        {/* Never colour alone: the flagged state is carried by text too. */}
        <span className="safety-glyph" aria-hidden="true">
          {isNotable(s) ? "▲" : "•"}
        </span>
        <span className="safety-label">{safetyLabel(s.category)}</span>
        <span className="safety-scope">
          {`${s.scope === "prompt" ? "your message" : "the reply"}${
            typeof s.modelCall === "number" ? ` · model call ${s.modelCall}` : ""
          }${s.agent ? ` · @${s.agent}` : ""}`}
        </span>
        <span className="safety-verdict">{safetyVerdict(s)}</span>
      </div>
    ));

  return (
    <details className="activity activity-trace safety-trace">
      <summary>{summary}</summary>
      <div className="activity-rows">
        <p className="safety-note">
          {providerFiltered
            ? "The model platform reported filtered content on this turn. Inspect the flagged rows; this may indicate provider-native enforcement or policy drift. AI4IA did not add a separate application-level block or rewrite."
            : "These are advisory labels from the model platform. AI4IA did not add an application-level block or rewrite; provider-native enforcement may still apply."}
          {coverage.length > 0
            ? ` Assessed: ${coverage
                .map((scope) => (scope === "prompt" ? "your message" : "the reply"))
                .join(" and ")}.`
            : ""}
          {safety.truncated
            ? ` Showing ${signals.length} of ${safety.signalCount ?? signals.length} returned assessments.`
            : ""}
          {status === "partial" || assessmentErrors.length > 0
            ? ` Assessment coverage was partial${
                assessmentErrors.length
                  ? ` (${assessmentErrors.join(", ")})`
                  : ""
              }.`
            : ""}
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

// Renders one tool-generated image. The bytes live behind an authenticated
// endpoint (a direct image element would not carry the bearer token), so we fetch
// the blob, wrap it in an object URL, and revoke it on unmount to avoid leaks.
// ``onEdit`` is passed only while the server reports image editing available.
function ImageAttachmentView({
  attachment,
  onEdit,
}: {
  attachment: MessageAttachment;
  onEdit?: (attachment: MessageAttachment) => void;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const [viewerOpen, setViewerOpen] = useState(false);

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
    <figure className="generated-image">
      {url ? (
        <button
          type="button"
          className="generated-image-open"
          onClick={() => setViewerOpen(true)}
        >
          <span className="visually-hidden">Open full size: </span>
          {/* eslint-disable-next-line @next/next/no-img-element -- authenticated blob object URL; next/image adds no value */}
          <img
            src={url}
            alt={caption}
            style={{
              maxWidth: "100%",
              maxHeight: 520,
              borderRadius: 10,
              border: "1px solid var(--border)",
              display: "block",
            }}
          />
        </button>
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
          attachment.sourceKind
            ? attachment.sourceKind === "library" && attachment.filename
              ? `edited from ${attachment.filename}`
              : "edited from an earlier image"
            : null,
          attachment.masked ? "selected region" : null,
          attachment.model,
          attachment.size,
          attachment.quality && attachment.quality !== "auto"
            ? `${attachment.quality} quality`
            : null,
          attachment.provider,
          attachment.region,
          attachment.dataZone ? `${attachment.dataZone} zone` : null,
          attachment.residency
            ? `${attachment.residency} residency`
            : null,
          attachment.costKnown === true &&
          attachment.estimatedCostUsd !== null &&
          attachment.estimatedCostUsd !== undefined
            ? `estimated $${attachment.estimatedCostUsd.toFixed(
                attachment.estimatedCostUsd < 0.01 ? 4 : 3,
              )}`
            : attachment.costKnown === false
              ? "cost estimate unavailable"
              : null,
        ]
          .filter(Boolean)
          .map((part) => ` · ${part}`)
          .join("")}
      </figcaption>
      {onEdit && url ? (
        <div className="generated-image-actions">
          <button
            type="button"
            onClick={() => onEdit(attachment)}
            aria-label={`Edit image: ${caption}`}
          >
            Edit
          </button>
        </div>
      ) : null}
      {viewerOpen && url ? (
        <DialogFrame
          ariaLabel={`Image: ${caption}`}
          onClose={() => setViewerOpen(false)}
          overlayPadding={12}
        >
          <div className="image-viewer" onClick={(event) => event.stopPropagation()}>
            {/* eslint-disable-next-line @next/next/no-img-element -- authenticated blob object URL */}
            <img src={url} alt={caption} />
            <div className="image-viewer-bar">
              <p>{caption}</p>
              <a className="btn btn-sm" href={url} download={`ai4ia-image-${attachment.id}.png`}>
                Download
              </a>
              <button type="button" className="btn btn-sm" onClick={() => setViewerOpen(false)}>
                Close
              </button>
            </div>
          </div>
        </DialogFrame>
      ) : null}
    </figure>
  );
}

function ImageFailureView({ attachment }: { attachment: MessageAttachment }) {
  return (
    <div className="generated-image-failure">
      <strong>{attachment.model || "Image model"}</strong>
      <span>{attachment.error || "Image generation failed."}</span>
      <small>
        {[
          attachment.provider,
          attachment.region,
          attachment.dataZone ? `${attachment.dataZone} zone` : null,
          attachment.residency
            ? `${attachment.residency} residency`
            : null,
          attachment.size,
          attachment.quality && attachment.quality !== "auto"
            ? `${attachment.quality} quality`
            : null,
          attachment.costKnown === true &&
          attachment.estimatedCostUsd !== null &&
          attachment.estimatedCostUsd !== undefined
            ? `estimated $${attachment.estimatedCostUsd.toFixed(
                attachment.estimatedCostUsd < 0.01 ? 4 : 3,
              )}`
            : "cost estimate unavailable",
        ]
          .filter(Boolean)
          .join(" · ")}
      </small>
    </div>
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

function CopyAction({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1600);
    return () => clearTimeout(timer);
  }, [copied]);
  if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return null;
  return (
    <button
      type="button"
      className="message-action"
      onClick={() => {
        void navigator.clipboard.writeText(text).then(
          () => setCopied(true),
          () => setCopied(false),
        );
      }}
      aria-label={copied ? "Message copied" : "Copy message"}
    >
      <Icon name={copied ? "check" : "copy"} size={16} />
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

function Bubble({
  msg,
  speechState,
  onToggleSpeak,
  onCitation,
  onInspectMemory,
  onEditImage,
}: {
  msg: DisplayMessage;
  speechState: SpeechState;
  onToggleSpeak: (id: string, text: string) => void;
  onCitation?: (target: CitationTarget) => void;
  onInspectMemory?: (memoryId: string | null) => void;
  onEditImage?: (attachment: MessageAttachment) => void;
}) {
  const receiptRef = useRef<HTMLDivElement>(null);
  const isUser = msg.role === "user";
  const isSystem = msg.role === "system";
  if (isSystem) return null;
  const label = isUser ? "You" : "Assistant";
  const speakable = !isUser && !msg.pending && msg.content.trim().length > 0;
  const images = msg.attachments?.filter((attachment) =>
    ["image", "image_error"].includes(attachment.kind),
  ) ?? [];
  return (
    <article className={`message ${isUser ? "message-user" : "message-assistant"}`}>
      <div className="message-meta">
        {/* The role is announced, not drawn: alignment and the bubble carry it visually. */}
        <span className="visually-hidden">{label}</span>
        {msg.source === "voice" && (
          <span className="message-tag" title="From a Voice Live conversation" aria-label="from voice">
            <Icon name="mic" size={14} />
            Voice
          </span>
        )}
        {msg.agent && <span className="message-tag">@{msg.agent}</span>}
      </div>
      <div className="message-body">
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
        ) : null}
      </div>
      <div className="message-details">
        {!msg.pending && msg.steps && msg.steps.length > 0 ? (
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
        {/* The turn's execution receipt, shown once settled so a receipt for a
            turn still in flight is never presented as the whole record. */}
        {!isUser && !msg.pending ? (
          <MemoryProvenance
            receipt={msg.executionReceipt}
            workflowReceipts={msg.workflowStepReceipts}
            onInspectMemory={onInspectMemory}
            onOpenReceipt={() => {
              const disclosure = receiptRef.current?.querySelector("details");
              if (!disclosure) return;
              disclosure.open = true;
              disclosure.querySelector("summary")?.focus();
            }}
          />
        ) : null}
        <div ref={receiptRef}>
          {!msg.pending && msg.workflowStepReceipts?.length ? (
            <WorkflowStepReceiptPanels receipts={msg.workflowStepReceipts} />
          ) : null}
          {!msg.pending && msg.executionReceipt ? (
            <ExecutionReceiptPanel receipt={msg.executionReceipt} />
          ) : null}
        </div>
      </div>
      {images.length > 0 ? (
        <ol
          className="image-comparison-grid"
          aria-label={images.length > 1 ? "Image model comparison" : "Generated image"}
        >
          {images.map((attachment) => (
            <li key={attachment.id}>
              {attachment.kind === "image" ? (
                <ImageAttachmentView
                  attachment={attachment}
                  onEdit={msg.pending ? undefined : onEditImage}
                />
              ) : (
                <ImageFailureView attachment={attachment} />
              )}
            </li>
          ))}
        </ol>
      ) : null}
      {msg.attachments?.map((att) =>
        att.kind === "video" ? (
          <VideoAttachmentView key={att.id} attachment={att} />
        ) : att.kind === "document" ? (
          <DocumentAttachmentView key={att.id} attachment={att} />
        ) : null,
      )}
      {!isUser && !msg.pending && msg.content.trim().length > 0 ? (
        <div className="message-actions">
          <CopyAction text={msg.content} />
          {speakable && (
            <button
              type="button"
              className="message-action"
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
            >
              <Icon name={speechState === "playing" ? "stop" : "speaker"} size={16} />
              {speechState === "playing"
                ? "Stop"
                : speechState === "busy"
                  ? "Loading…"
                  : "Speak"}
            </button>
          )}
        </div>
      ) : null}
    </article>
  );
}

export function MessageList({
  messages,
  conversationId,
  onError,
  onCitation,
  onInspectMemory,
  onEditImage,
  emptyActions,
}: {
  messages: DisplayMessage[];
  conversationId?: string | null;
  onError?: (message: string) => void;
  onCitation?: (target: CitationTarget) => void;
  onInspectMemory?: (memoryId: string | null) => void;
  /** Present only while the server reports image editing available. */
  onEditImage?: (attachment: MessageAttachment) => void;
  /** Shortcuts offered in an empty conversation (attach, talk, generate). */
  emptyActions?: EmptyAction[];
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);
  const isNearBottomRef = useRef(true);
  const conversationIdRef = useRef(conversationId);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const playback = useSpeechPlayback((msg) => onError?.(msg));
  const latest = messages.at(-1);
  const messageRevision = `${messages.length}:${latest?.id ?? ""}:${latest?.content.length ?? 0}:${latest?.pending ?? false}:${latest?.steps?.length ?? 0}`;

  useEffect(() => {
    const conversationChanged = conversationIdRef.current !== conversationId;
    conversationIdRef.current = conversationId;
    if (conversationChanged) {
      isNearBottomRef.current = true;
      setShowJumpToLatest(false);
      endRef.current?.scrollIntoView({ behavior: "auto", block: "end" });
      return;
    }
    if (isNearBottomRef.current) {
      endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
      setShowJumpToLatest(false);
    } else {
      setShowJumpToLatest(true);
    }
  }, [conversationId, messageRevision]);

  const updateScrollPosition = () => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const distanceFromBottom =
      viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight;
    const isNearBottom = distanceFromBottom <= 96;
    isNearBottomRef.current = isNearBottom;
    if (isNearBottom) setShowJumpToLatest(false);
  };

  const jumpToLatest = () => {
    isNearBottomRef.current = true;
    setShowJumpToLatest(false);
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  };

  return (
    <div className="message-viewport">
      <div
        ref={viewportRef}
        role="log"
        aria-live="polite"
        aria-label="Conversation"
        onScroll={updateScrollPosition}
        className="message-scroll"
      >
        <div
          className="message-column"
          style={showJumpToLatest ? { paddingBottom: 80 } : undefined}
        >
          {messages.length === 0 ? (
            <div className="message-empty">
              <h2>Start a conversation</h2>
              <p>
                Ask anything, attach a document to ground the reply, type <strong>/</strong> for
                commands or <strong>@</strong> to bring in an agent.
              </p>
              {emptyActions && emptyActions.length > 0 ? (
                <div className="message-empty-actions">
                  {emptyActions.map((action) => (
                    <button
                      key={action.label}
                      type="button"
                      className="btn"
                      onClick={action.onClick}
                    >
                      <Icon name={action.icon} size={18} />
                      {action.label}
                    </button>
                  ))}
                </div>
              ) : null}
              <div className="message-empty-links">
                <a href={USER_GUIDE_URL} target="_blank" rel="noreferrer">
                  User guide
                  <span className="visually-hidden"> (opens in a new tab)</span>
                </a>
                <a href={DOCS_INDEX_URL} target="_blank" rel="noreferrer">
                  Documentation
                  <span className="visually-hidden"> (opens in a new tab)</span>
                </a>
                <a href={STATUS_URL} target="_blank" rel="noreferrer">
                  Deployment status
                  <span className="visually-hidden"> (opens in a new tab)</span>
                </a>
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
                onInspectMemory={onInspectMemory}
                onEditImage={onEditImage}
              />
            ))
          )}
          <div ref={endRef} />
        </div>
      </div>
      {showJumpToLatest && (
        <button type="button" className="jump-to-latest" onClick={jumpToLatest}>
          <Icon name="arrow-down" size={16} />
          Jump to latest
        </button>
      )}
    </div>
  );
}

export type { DisplayMessage };
