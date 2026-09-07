"use client";

// Shared, presentational execution evidence for chat and workflow reports.
// No transcript state, speech playback, or artifact fetching belongs here.
import type { ReactNode } from "react";
import type {
  ActivityStep,
  ExecutionReceipt,
  ReceiptPayload,
  ReceiptPromptMessage,
} from "@/lib/types";
import { formatBytes } from "@/lib/library";
import { ToolApprovalProvenance } from "./ToolApprovalProvenance";

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
function ReceiptFrame({ embedded, summary, children }: { embedded: boolean; summary: ReactNode; children: ReactNode }) {
  return embedded ? <div className="activity-receipt-nested">{children}</div> : (
    <details className="activity activity-trace">
      <summary>{summary}</summary>
      {children}
    </details>
  );
}

export function ExecutionReceiptPanel({ receipt, embedded = false }: { receipt: ExecutionReceipt; embedded?: boolean }) {
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
          ? `${receipt.usage.totalTokens ?? "Unknown"} tokens across ${receipt.usage.calls} model call${
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
    <ReceiptFrame embedded={embedded} summary={<>
        {`Execution receipt · ${receipt.promptMessageCount} prompt message${
          receipt.promptMessageCount === 1 ? "" : "s"
        }, ${receipt.toolsOfferedCount} tool${receipt.toolsOfferedCount === 1 ? "" : "s"} offered, ${
          receipt.toolCallCount
        } invoked`}
        {receipt.partial ? " · partial" : ""}
      </>}>
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
                <span className="activity-label">Per-call approvals</span>
                <span className="activity-detail">
                  {`${receipt.approvalsRequested} requested · ${receipt.approvalsGranted} granted`}
                </span>
              </div>
            )}
            <div className="activity-row">
              <span className="activity-label">Auto-approved calls</span>
              <span className="activity-detail">
                {typeof receipt.autoApprovedToolCalls === "number"
                  ? `${receipt.autoApprovedToolCalls} reported by the server`
                  : "Not recorded — unknown"}
              </span>
            </div>
            {calls.some((call) => call.approval == null) ? (
              <p className="safety-note">
                Some tool-call approval provenance was not recorded. Historical approval
                for those calls is unknown, even if a server counter is zero.
              </p>
            ) : null}
            {receipt.toolConsent ? (
              <div className="activity-row">
                <span className="activity-label">Consent at execution</span>
                <span className="activity-detail">
                  {receipt.toolConsent.scope} · <code>{receipt.toolConsent.id}</code>
                  {` · ${receipt.toolConsent.toolCount} tool contracts · expires `}
                  <time dateTime={receipt.toolConsent.expiresAt}>{new Date(receipt.toolConsent.expiresAt).toLocaleString()}</time>
                </span>
              </div>
            ) : null}
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
                  <ExecutionReceiptPanel receipt={nested} embedded />
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
            {`Tools offered · ${receipt.toolsOfferedCount}`}
          </summary>
          <div className="activity-rows">
            <p className="safety-note">
              Every tool this turn advertised to the model. Offered is not
              invoked: a tool listed here that never appears below is one the
              model could have used and did not.
            </p>
            {offered.length === 0 ? (
              <div className="activity-row">
                <span className="activity-label">{receipt.toolsOfferedCount === 0 ? "No tools were offered" : "Offered-tool details were not retained"}</span>
              </div>
            ) : (
              offered.map((offer, index) => (
                <div key={`${offer.name}-${index}`} className="activity-row">
                  {/* One text node, so the whole label reads as written. */}
                  <span className="activity-label">
                    {`${offer.name} · ${invoked.has(offer.name) ? "invoked" : receipt.toolCallCount > calls.length ? "not shown among retained calls" : "not invoked"}`}
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
                <span className="activity-label">
                  {receipt.toolCallCount === 0 ? "No tools were invoked" : "Tool-call details were not retained"}
                </span>
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
                  <ToolApprovalProvenance approval={call.approval} consentId={call.consentId} />
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
    </ReceiptFrame>
  );
}

// Workflow step receipts are independent bounded records, not delegations or
// synthetic tool calls. Their list order is recorded execution order; a missing
// receipt must never shift an inferred workflow step index onto another agent.
export function WorkflowStepReceiptPanels({ receipts }: { receipts: ExecutionReceipt[] }) {
  return (
    <details className="activity activity-trace">
      <summary>Step execution receipts · {receipts.length}</summary>
      <div className="activity-rows">
        {receipts.map((receipt, index) => (
          <details key={`${receipt.correlationId ?? "step"}-${index}`}>
            <summary>
              Recorded execution {index + 1}{receipt.runtime.agent ? ` · @${receipt.runtime.agent}` : ""}
              {receipt.partial ? " · partial" : ""}
            </summary>
            <ExecutionReceiptPanel receipt={receipt} embedded />
          </details>
        ))}
      </div>
    </details>
  );
}

// A small glyph for a finalized step's outcome (running steps show a spinner).
function stepGlyph(kind: string): string {
  if (kind === "tool_result" || kind === "delegate" || kind === "workflow_step") return "✓";
  if (kind === "tool_denied") return "⊘";
  if (kind === "tool_error" || kind === "workflow_error") return "!";
  return "•";
}

// Renders the agent's activity: a live, animated view while the turn runs (the
// current tool spins, finished ones tick off), and a collapsed "Activity" trace
// once complete. Replaces the bare blinking cursor for tool-using turns.
export function ActivityPanel({ steps, live }: { steps: ActivityStep[]; live: boolean }) {
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
