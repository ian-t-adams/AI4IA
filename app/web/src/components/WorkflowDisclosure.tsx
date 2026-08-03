"use client";

// WAI-ARIA accordion section for the workflow builder.
//
// Deliberately a local copy of ConversationInspector's disclosure rather than a
// shared extraction: that surface is recently stabilised and a11y-sensitive, and
// coupling it to every workflow tweak trades a small duplication for a large
// blast radius. Worth extracting once a third consumer appears.
//
// Two details here are load-bearing and easy to lose in a "cleanup":
//
//   * The region element stays mounted while collapsed, with `hidden` doing the
//     hiding and the children conditionally rendered inside it. Unmounting the
//     region would leave `aria-controls` pointing at nothing, which assistive
//     technology reports as a broken control.
//   * The caret is drawn with clip-path, not typed as a glyph. Generated *text*
//     content participates in the accessible name, so a "▸" here would silently
//     rename every trigger.

import type { ReactNode } from "react";

export function WorkflowDisclosure({
  id,
  title,
  summary,
  expanded,
  onToggle,
  children,
}: {
  id: string;
  title: string;
  /** Short state description shown on the trigger, so it reads without opening. */
  summary?: string;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className="workflow-disclosure">
      <div className="workflow-disclosure-header">
        <h3>
          <button
            type="button"
            id={`workflow-section-${id}`}
            className="workflow-disclosure-trigger"
            aria-expanded={expanded}
            aria-controls={`workflow-region-${id}`}
            onClick={onToggle}
          >
            <span>{title}</span>
            {summary && <span className="workflow-disclosure-summary">{summary}</span>}
          </button>
        </h3>
      </div>
      <div
        id={`workflow-region-${id}`}
        className="workflow-disclosure-region"
        role="region"
        aria-labelledby={`workflow-section-${id}`}
        hidden={!expanded}
      >
        {expanded ? children : null}
      </div>
    </div>
  );
}
