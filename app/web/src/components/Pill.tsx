"use client";

import { HelpTooltip } from "./HelpTooltip";

export type PillTone = "ok" | "warn" | "error" | "muted" | "neutral";

// Shared tone -> color mapping used by every status/health/approval pill across
// the builder surfaces (agents, workflows, MCP servers). Previously each file
// defined its own byte-identical copy (`healthToneColor`, `mcpHealthToneColor`).
export function pillToneColor(tone: PillTone): string {
  switch (tone) {
    case "ok":
      return "#15803d";
    case "warn":
      return "#b45309";
    case "error":
      return "var(--danger)";
    case "neutral":
      return "var(--fg)";
    default:
      return "var(--fg-muted)";
  }
}

/**
 * Small rounded status badge (health, approval posture, official/verified,
 * risk level, etc.) used across AgentBuilder, WorkflowBuilder, and
 * McpServerBuilder. When `detail` is provided it's exposed through an
 * accessible HelpTooltip rather than a bare `title=` attribute, so keyboard
 * and screen-reader users get the same explanation sighted mouse users get
 * from hovering a native tooltip.
 */
export function Pill({
  label,
  tone = "muted",
  detail,
  helpLabel,
}: {
  label: string;
  tone?: PillTone;
  detail?: string | null;
  // Distinguishes this pill's help button when several pills sit side by side
  // (e.g. "Health: Degraded" vs "Approval: Requires approval"). Defaults to label.
  helpLabel?: string;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: "0.72em",
        padding: "2px 8px",
        borderRadius: 999,
        border: "1px solid var(--border)",
        background: "var(--bg)",
        color: pillToneColor(tone),
        whiteSpace: "nowrap",
      }}
    >
      {label}
      {detail ? (
        <HelpTooltip label={helpLabel ?? label} size="sm">
          {detail}
        </HelpTooltip>
      ) : null}
    </span>
  );
}
