"use client";

import { useState } from "react";
import type { PendingToolApprovalPrompt } from "../lib/types";
import { Pill } from "./Pill";

/**
 * Live approval prompt for a tool call the server refused to run.
 *
 * Audit finding P1-13: a standing-approved external tool used to execute with
 * whatever arguments the model produced, and text that reached the model from a
 * document, a memory, a web result or a previous tool response could choose
 * those arguments. This card is the human in that loop — so it has to show the
 * things an exfiltration attempt would reveal:
 *
 * - **where** it goes (`host`), which is what an attacker actually needs to
 *   change and the one field a plausible-looking payload cannot disguise;
 * - **what** it would carry (`argumentsPreview`), already redacted and bounded
 *   server-side by the same redactor the activity trace uses — this component
 *   deliberately does no redaction of its own, because a second implementation
 *   is a second thing to get wrong; and
 * - **which tool**, by its durable governed name rather than the opaque runtime
 *   alias the model sees.
 *
 * The card is presentation only. Approving posts an opaque `{requestId, grant}`
 * pair; the server re-derives what that authorizes from its own record, so a
 * tampered card cannot approve a different call. Denying sends nothing at all —
 * absence of a grant *is* the denial, which is why there is no "deny" endpoint
 * to fail and no state to get stuck in.
 */
export function ToolApprovalPanel({
  prompts,
  onApprove,
  onDeny,
  busy = false,
}: {
  prompts: PendingToolApprovalPrompt[];
  onApprove: (prompt: PendingToolApprovalPrompt) => void;
  onDeny: (prompt: PendingToolApprovalPrompt) => void;
  busy?: boolean;
}) {
  if (prompts.length === 0) return null;
  return (
    <section
      aria-label="Tool approvals"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        margin: "0 0 8px",
      }}
    >
      {prompts.map((prompt) => (
        <ToolApprovalCard
          key={prompt.id}
          prompt={prompt}
          onApprove={onApprove}
          onDeny={onDeny}
          busy={busy}
        />
      ))}
    </section>
  );
}

function ToolApprovalCard({
  prompt,
  onApprove,
  onDeny,
  busy,
}: {
  prompt: PendingToolApprovalPrompt;
  onApprove: (prompt: PendingToolApprovalPrompt) => void;
  onDeny: (prompt: PendingToolApprovalPrompt) => void;
  busy: boolean;
}) {
  const preview = Object.entries(prompt.argumentsPreview ?? {});
  const masked = new Set(prompt.argumentsMasked ?? []);
  const elided = new Set(prompt.argumentsElided ?? []);
  const omitted = prompt.argumentsOmitted ?? 0;
  // Capture once rather than calling Date.now during render (React purity).
  const [renderedAt] = useState(() => Date.now());
  const expired = Number.isFinite(Date.parse(prompt.expiresAt))
    ? Date.parse(prompt.expiresAt) <= renderedAt
    : true;
  return (
    <div
      role="group"
      aria-label={`Approve ${prompt.label}`}
      style={{
        border: "1px solid var(--border)",
        borderRadius: "var(--radius)",
        background: "var(--bg-elevated)",
        padding: "10px 12px",
        display: "flex",
        flexDirection: "column",
        gap: 8,
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
        }}
      >
        <strong style={{ fontSize: "0.9em" }}>Approval needed</strong>
        <Pill
          label={prompt.label}
          tone="neutral"
          detail={prompt.purpose || undefined}
          helpLabel={`About ${prompt.label}`}
        />
        {prompt.host ? (
          <Pill
            label={`sends to ${prompt.host}`}
            tone="warn"
            detail="This call leaves your tenant and reaches this host. Approve it only if that destination is expected for what you asked."
            helpLabel={`About ${prompt.host}`}
          />
        ) : null}
        {prompt.risk ? <Pill label={prompt.risk} tone="warn" /> : null}
      </div>

      <p style={{ margin: 0, fontSize: "0.85em", color: "var(--fg-muted)" }}>
        This request was not run. Content in this conversation — including
        uploaded documents, saved memories and tool results — can influence what
        a tool is asked to send, so outbound calls need your approval for these
        exact values. Approval starts a retry turn; the model must re-issue this
        exact call before anything runs.
      </p>

      {expired ? (
        <p role="alert" style={{ margin: 0, fontSize: "0.82em", color: "var(--danger)" }}>
          This approval has expired. Deny it and ask again to generate a fresh request.
        </p>
      ) : null}

      {preview.length > 0 ? (
        <dl
          style={{
            margin: 0,
            display: "grid",
            gridTemplateColumns: "auto minmax(0, 1fr)",
            columnGap: 10,
            rowGap: 4,
            fontSize: "0.82em",
          }}
        >
          {preview.map(([key, value]) => (
            <div key={key} style={{ display: "contents" }}>
              <dt style={{ color: "var(--fg-muted)", whiteSpace: "nowrap" }}>
                {key}
              </dt>
              <dd
                style={{
                  margin: 0,
                  overflowWrap: "anywhere",
                  fontFamily: "monospace",
                  // A masked value is NOT the value: say so visually as well as
                  // textually, so "***REDACTED***" cannot read as literal content.
                  fontStyle: masked.has(key) ? "italic" : undefined,
                  color: masked.has(key) ? "var(--fg-muted)" : undefined,
                }}
              >
                {value}
                {masked.has(key) ? (
                  <span style={{ fontFamily: "inherit" }}>
                    {" "}
                    (hidden here, sent in full)
                  </span>
                ) : null}
                {elided.has(key) ? (
                  <span style={{ color: "var(--fg-muted)" }}> (shortened)</span>
                ) : null}
              </dd>
            </div>
          ))}
        </dl>
      ) : (
        <p style={{ margin: 0, fontSize: "0.82em", color: "var(--fg-muted)" }}>
          No arguments.
        </p>
      )}

      {omitted > 0 ? (
        // Never let the list end silently. The argument set is chosen by the
        // model, so a quietly-shortened preview is exactly how a destination
        // gets hidden from the person approving it.
        <p
          role="alert"
          style={{
            margin: 0,
            fontSize: "0.82em",
            color: "var(--danger)",
            fontWeight: 600,
          }}
        >
          {omitted} more argument{omitted === 1 ? "" : "s"} will be sent but{" "}
          {omitted === 1 ? "is" : "are"} not shown here. You are not seeing the
          whole call — deny unless you expected this.
        </p>
      ) : null}

      <div style={{ display: "flex", gap: 8 }}>
        <button
          type="button"
          disabled={busy || expired}
          onClick={() => onApprove(prompt)}
          aria-label={`Approve and retry ${prompt.label}`}
          style={{
            // --accent-fg is derived per accent by ThemeProvider, so it stays
            // readable on a user-chosen accent. Never hardcode a foreground
            // here (see the brand notes in AGENTS.md).
            background: "var(--accent)",
            color: "var(--accent-fg)",
            border: "1px solid transparent",
            borderRadius: "var(--radius)",
            padding: "6px 12px",
          }}
        >
          Approve and retry
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => onDeny(prompt)}
          aria-label={`Deny ${prompt.label}`}
          style={{
            background: "transparent",
            color: "var(--fg)",
            border: "1px solid var(--border)",
            borderRadius: "var(--radius)",
            padding: "6px 12px",
          }}
        >
          Deny
        </button>
      </div>
    </div>
  );
}
