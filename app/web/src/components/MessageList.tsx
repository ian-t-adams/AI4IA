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
  ReceiptPayload,
  ReceiptPromptMessage,
  RetrievedSource,
  SafetySignal,
} from "@/lib/types";
import { SAFETY_MAX_SEVERITY_LEVEL } from "@/lib/types";
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
  // What was supplied to the model and what it was allowed to do (see
  // ai4ia_api.receipts). Absent on turns that predate the feature.
  executionReceipt?: ExecutionReceipt | null;
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

// Bytes rendered for a human, without pretending to more precision than the
// number carries.
function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "unknown size";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// One receipt payload as monospaced, pre-wrapped diagnostic text.
//
// The body is server-redacted and server-bounded; this renders it verbatim and
// says plainly when it is only part of what was sent. `sha256`/`bytes` describe
// the FULL payload, so a shed or truncated body still proves how big the
// original was — which is the whole point when the payload came from a tool.
function PayloadView({ payload, label }: { payload: ReceiptPayload; label: string }) {
  return (
    <div className="activity-row" style={{ display: "block" }}>
      <span className="activity-label">
        {`${label} · ${formatBytes(payload.bytes)}`}
        {payload.truncated ? " · truncated" : ""}
      </span>
      {payload.text ? (
        <pre
          style={{
            margin: "4px 0 0",
            padding: "6px 8px",
            borderRadius: 6,
            border: "1px solid var(--border)",
            background: "var(--bg-elevated)",
            color: "var(--fg-muted)",
            fontSize: "0.78em",
            whiteSpace: "pre-wrap",
            wordBreak: "break-word",
            overflowX: "auto",
          }}
        >
          {payload.text}
        </pre>
      ) : (
        <span className="activity-detail">
          Body not retained — recorded by digest and size only.
        </span>
      )}
      <span className="activity-detail">{`sha256 ${payload.sha256.slice(0, 16)}…`}</span>
    </div>
  );
}

function PromptMessageView({
  message,
  label,
}: {
  message: ReceiptPromptMessage;
  label: string;
}) {
  return (
    <div>
      <PayloadView payload={message.content} label={label} />
      {message.toolCallId ? (
        <div className="activity-row">
          <span className="activity-label">Tool call id</span>
          <span className="activity-detail">{message.toolCallId}</span>
        </div>
      ) : null}
      {message.toolCalls ? (
        <PayloadView payload={message.toolCalls} label="Assistant tool calls" />
      ) : null}
    </div>
  );
}

// The turn's execution receipt: what was supplied to the model, what it was
// allowed to do, and what it did.
//
// Progressively disclosed on purpose — a collapsed summary, then four
// independently collapsible sections — because this is review material, not
// something to put in front of every reader of every turn. Each section is a
// native <details>/<summary>, so keyboard and screen-reader users get expand
// and collapse semantics for free rather than through re-implemented ARIA.
//
// It reports server-owned facts only. There is deliberately no "reasoning" or
// "thinking" section: the platform does not hand this app a model's internal
// deliberation, so a panel claiming to show one would be asserting something
// the system cannot support.
function ExecutionReceiptPanel({ receipt }: { receipt: ExecutionReceipt }) {
  const runtime = receipt.runtime ?? {};
  const offered = receipt.toolsOffered ?? [];
  const calls = receipt.toolCalls ?? [];
  const blocks = receipt.contextBlocks ?? [];
  const delegations = receipt.delegations ?? [];
  const notes = receipt.notes ?? [];
  const invoked = new Set(calls.map((call) => call.tool));

  const runtimeRows: [string, string | null | undefined][] = [
    ["Model", runtime.modelId],
    ["Deployment", runtime.deployment],
    ["Region", runtime.region],
    ["SKU", runtime.sku],
    ["Data zone", runtime.dataZone],
    ["Processing residency", runtime.residency],
    ["API surface", runtime.api],
    ["Agent", runtime.agent ? `@${runtime.agent}` : null],
    ["Instruction source", runtime.instructionSource],
    [
      "Instruction hash",
      runtime.instructionSha256
        ? `sha256 ${runtime.instructionSha256}`
        : null,
    ],
    [
      "Agent configuration",
      runtime.agentConfigSha256
        ? `sha256 ${runtime.agentConfigSha256}`
        : null,
    ],
    [
      "Usage",
      receipt.usage
        ? receipt.usage.known
          ? `${receipt.usage.totalTokens ?? 0} tokens across ${receipt.usage.calls} model call${
              receipt.usage.calls === 1 ? "" : "s"
            }${receipt.usage.complete ? "" : " · partial reporting"}`
          : `unavailable across ${receipt.usage.calls} model call${
              receipt.usage.calls === 1 ? "" : "s"
            }`
        : null,
    ],
    [
      "Safety coverage",
      receipt.safety
        ? `${receipt.safety.status}${
            receipt.safety.provider ? ` · ${receipt.safety.provider}` : ""
          }${
            receipt.safety.coverage.length
              ? ` · ${receipt.safety.coverage.join(" + ")}`
              : ""
          } · ${receipt.safety.signalCount} assessment${
            receipt.safety.signalCount === 1 ? "" : "s"
          }${receipt.safety.truncated ? " · truncated" : ""}`
        : null,
    ],
    ["Correlation id", receipt.correlationId],
  ];

  return (
    <details className="activity activity-trace">
      <summary>
        {`Execution receipt · ${receipt.promptMessageCount} prompt message${
          receipt.promptMessageCount === 1 ? "" : "s"
        }, ${offered.length} tool${offered.length === 1 ? "" : "s"} offered, ${
          receipt.toolCallCount
        } invoked`}
        {receipt.partial ? " · partial" : ""}
      </summary>
      <div className="activity-rows">
        <p className="safety-note">
          Exactly what this turn sent, was offered, and ran. Secrets are removed
          and large payloads are shortened by the server before they are stored;
          anything shortened says so and keeps its original size and digest.
          This does not show model-internal reasoning — the platform does not
          report any.
        </p>

        <details>
          <summary className="activity-label">Runtime</summary>
          <div className="activity-rows">
            {runtimeRows
              .filter(([, value]) => Boolean(value))
              .map(([label, value]) => (
                <div key={label} className="activity-row">
                  <span className="activity-label">{label}</span>
                  <span className="activity-detail">{value}</span>
                </div>
              ))}
            <div className="activity-row">
              <span className="activity-label">Outcome</span>
              <span className="activity-detail">
                {`${receipt.status} · ${receipt.iterations} model iteration${
                  receipt.iterations === 1 ? "" : "s"
                }`}
              </span>
            </div>
            {(receipt.approvalsRequested > 0 || receipt.approvalsGranted > 0) && (
              <div className="activity-row">
                <span className="activity-label">Approvals</span>
                <span className="activity-detail">
                  {`${receipt.approvalsRequested} requested · ${receipt.approvalsGranted} granted`}
                </span>
              </div>
            )}
            {notes.length > 0 && (
              <div className="activity-row">
                <span className="activity-label">Bounds applied</span>
                <span className="activity-detail">{notes.join(", ")}</span>
              </div>
            )}
          </div>
        </details>

        {delegations.length > 0 ? (
          <details>
            <summary className="activity-label">
              {`Delegated runs · ${delegations.length}`}
            </summary>
            <div className="activity-rows">
              {delegations.map((nested, index) => (
                <details key={`${nested.runtime?.agent ?? "agent"}-${index}`}>
                  <summary className="activity-label">
                    {`@${nested.runtime?.agent ?? "unknown"} · ${nested.iterations} model iteration${
                      nested.iterations === 1 ? "" : "s"
                    } · ${nested.toolCallCount} tool call${
                      nested.toolCallCount === 1 ? "" : "s"
                    }`}
                  </summary>
                  <div className="activity-rows">
                    {(nested.prompt ?? []).map((message, promptIndex) => (
                      <PromptMessageView
                        key={promptIndex}
                        message={message}
                        label={`Prompt ${promptIndex + 1}: ${message.role}`}
                      />
                    ))}
                    {(nested.toolsOffered ?? []).map((offer) => (
                      <div
                        key={offer.name}
                        className="activity-row"
                      >
                        <span className="activity-label">{offer.name}</span>
                        <span className="activity-detail">offered</span>
                      </div>
                    ))}
                    {(nested.toolCalls ?? []).map((call, callIndex) => (
                      <div key={callIndex}>
                        <div className="activity-row">
                          <span className="activity-label">
                            {`${call.tool} · ${call.outcome}`}
                          </span>
                        </div>
                        {call.arguments ? (
                          <PayloadView
                            payload={call.arguments}
                            label="Arguments"
                          />
                        ) : null}
                        {call.result ? (
                          <PayloadView payload={call.result} label="Result" />
                        ) : null}
                      </div>
                    ))}
                  </div>
                </details>
              ))}
            </div>
          </details>
        ) : null}

        <details>
          <summary className="activity-label">
            {`Prompt and context · ${formatBytes(receipt.promptBytes)}`}
          </summary>
          <div className="activity-rows">
            {receipt.droppedHistoryMessages > 0 && (
              <p className="safety-note">
                {`${receipt.droppedHistoryMessages} earlier message${
                  receipt.droppedHistoryMessages === 1 ? " was" : "s were"
                } dropped to fit the context budget.`}
              </p>
            )}
            {blocks.map((block, i) => (
              <div key={`${block.kind}-${i}`}>
                <div className="activity-row">
                  <span className="activity-label">
                    {`Context: ${block.kind}`}
                  </span>
                  <span className="activity-detail">
                    {block.admitted
                      ? "admitted to the prompt"
                      : "built but displaced — never reached the model"}
                  </span>
                </div>
                {block.content ? (
                  <PayloadView payload={block.content} label={`${block.kind} block`} />
                ) : null}
                {(block.sources ?? []).map((source) => (
                  <div
                    key={`${block.kind}-${source.id}-${source.version ?? ""}`}
                    className="activity-row"
                  >
                    <span className="activity-label">
                      {source.label ?? source.id}
                    </span>
                    <span className="activity-detail">
                      {[
                        source.kind,
                        source.version ? `version ${source.version}` : null,
                        source.documentId
                          ? `document ${source.documentId}`
                          : null,
                        typeof source.score === "number"
                          ? `score ${source.score.toFixed(3)}`
                          : null,
                        source.contentSha256
                          ? `sha256 ${source.contentSha256.slice(0, 16)}…`
                          : null,
                      ]
                        .filter(Boolean)
                        .join(" · ")}
                    </span>
                  </div>
                ))}
                {(block.sourceCount ?? 0) > (block.sources ?? []).length ? (
                  <p className="safety-note">
                    {`${(block.sourceCount ?? 0) - (block.sources ?? []).length} further source reference(s) are not listed.`}
                  </p>
                ) : null}
              </div>
            ))}
            {(receipt.prompt ?? []).map((message, i) => (
              <PromptMessageView
                key={i}
                message={message}
                label={`${i + 1}. ${message.role}`}
              />
            ))}
            {receipt.promptMessageCount > (receipt.prompt ?? []).length && (
              <p className="safety-note">
                {`${receipt.promptMessageCount - (receipt.prompt ?? []).length} further prompt message(s) are not shown; the byte total above covers all of them.`}
              </p>
            )}
            {(receipt.modelRequests ?? []).map((request) => (
              <details key={request.iteration}>
                <summary className="activity-label">
                  {`Model request ${request.iteration} · ${request.promptMessageCount} messages · ${formatBytes(request.promptBytes)}`}
                </summary>
                <div className="activity-rows">
                  {request.prompt.map((message, index) => (
                    <PromptMessageView
                      key={index}
                      message={message}
                      label={`${index + 1}. ${message.role}`}
                    />
                  ))}
                </div>
              </details>
            ))}
          </div>
        </details>

        <details>
          <summary className="activity-label">
            {`Tools offered · ${offered.length}`}
          </summary>
          <div className="activity-rows">
            <p className="safety-note">
              Every tool this turn advertised to the model. Offered is not
              invoked: a tool listed here that never appears below is one the
              model could have used and did not.
            </p>
            {offered.length === 0 ? (
              <div className="activity-row">
                <span className="activity-label">No tools were offered</span>
              </div>
            ) : (
              offered.map((offer) => (
                <div key={offer.name} className="activity-row">
                  {/* One text node, so the whole label reads as written. */}
                  <span className="activity-label">
                    {`${offer.name} · ${invoked.has(offer.name) ? "invoked" : "not invoked"}`}
                  </span>
                  {offer.description ? (
                    <span className="activity-detail">{offer.description}</span>
                  ) : null}
                </div>
              ))
            )}
            {receipt.toolsOfferedCount > offered.length && (
              <p className="safety-note">
                {`${receipt.toolsOfferedCount - offered.length} further offered tool(s) are not listed.`}
              </p>
            )}
          </div>
        </details>

        <details>
          <summary className="activity-label">
            {`Tool calls · ${receipt.toolCallCount}`}
          </summary>
          <div className="activity-rows">
            {calls.length === 0 ? (
              <div className="activity-row">
                <span className="activity-label">No tools were invoked</span>
              </div>
            ) : (
              calls.map((call, i) => (
                <div key={i}>
                  <div className="activity-row">
                    <span className="activity-label">
                      {`${call.tool} · ${call.outcome}`}
                    </span>
                    {call.detail ? (
                      <span className="activity-detail">{call.detail}</span>
                    ) : null}
                  </div>
                  {call.arguments ? (
                    <PayloadView payload={call.arguments} label="arguments" />
                  ) : null}
                  {call.result ? (
                    <PayloadView payload={call.result} label="result" />
                  ) : null}
                </div>
              ))
            )}
            {receipt.toolCallCount > calls.length && (
              <p className="safety-note">
                {`${receipt.toolCallCount - calls.length} further tool call(s) are not listed.`}
              </p>
            )}
          </div>
        </details>
      </div>
    </details>
  );
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
    <figure className="generated-image">
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
        {/* The turn's execution receipt, shown once settled so a receipt for a
            turn still in flight is never presented as the whole record. */}
        {!msg.pending && msg.executionReceipt ? (
          <ExecutionReceiptPanel receipt={msg.executionReceipt} />
        ) : null}
        {msg.attachments?.some((attachment) =>
          ["image", "image_error"].includes(attachment.kind)
        ) ? (
          <ol
            className="image-comparison-grid"
            aria-label={
              msg.attachments.filter((attachment) =>
                ["image", "image_error"].includes(attachment.kind)
              )
                .length > 1
                ? "Image model comparison"
                : "Generated image"
            }
          >
            {msg.attachments
              .filter((attachment) =>
                ["image", "image_error"].includes(attachment.kind)
              )
              .map((attachment) => (
                <li key={attachment.id}>
                  {attachment.kind === "image" ? (
                    <ImageAttachmentView attachment={attachment} />
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
  conversationId,
  onError,
  onCitation,
}: {
  messages: DisplayMessage[];
  conversationId?: string | null;
  onError?: (message: string) => void;
  onCitation?: (target: CitationTarget) => void;
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
    <div style={{ flex: 1, minHeight: 0, position: "relative" }}>
      <div
        ref={viewportRef}
        role="log"
        aria-live="polite"
        aria-label="Conversation"
        onScroll={updateScrollPosition}
        style={{
          height: "100%",
          overflowY: "auto",
          padding: `24px max(24px, 6%) ${showJumpToLatest ? "80px" : "24px"}`,
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
                  <span className="visually-hidden"> (opens in a new tab)</span>
                </a>
                <a
                  href={DOCS_INDEX_URL}
                  target="_blank"
                  rel="noreferrer"
                  style={{ color: "var(--accent)" }}
                >
                  Documentation
                  <span className="visually-hidden"> (opens in a new tab)</span>
                </a>
                <a
                  href={STATUS_URL}
                  target="_blank"
                  rel="noreferrer"
                  style={{ color: "var(--accent)" }}
                >
                  Deployment status
                  <span className="visually-hidden"> (opens in a new tab)</span>
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
      {showJumpToLatest && (
        <button
          type="button"
          onClick={jumpToLatest}
          style={{
            position: "absolute",
            left: "50%",
            bottom: 16,
            transform: "translateX(-50%)",
            minHeight: 44,
            padding: "8px 14px",
            border: "1px solid var(--border)",
            borderRadius: 999,
            background: "var(--bg-elevated)",
            color: "var(--fg)",
            font: "inherit",
            fontWeight: 650,
            cursor: "pointer",
            zIndex: 1,
          }}
        >
          Jump to latest
        </button>
      )}
    </div>
  );
}

export type { DisplayMessage };
