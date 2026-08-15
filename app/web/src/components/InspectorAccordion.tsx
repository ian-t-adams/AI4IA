"use client";

import { HelpTooltip } from "./HelpTooltip";

export type Section =
  | "model"
  | "instructions"
  | "tools"
  | "voice"
  | "documents"
  | "memory"
  | "usage";

export function SectionTitle({
  title,
  help,
}: {
  title: string;
  help: React.ReactNode;
}) {
  return (
    <div className="inspector-section-title">
      <h2>{title}</h2>
      <HelpTooltip label={title}>{help}</HelpTooltip>
    </div>
  );
}

/**
 * One collapsible section inside a group panel — the WAI-ARIA accordion
 * pattern: a heading whose only child is a `<button aria-expanded
 * aria-controls>`, controlling a `role="region"` labelled by that button.
 *
 * Three details are deliberate:
 *
 * 1. The HelpTooltip is a *sibling* of the `<h2>`, never inside the trigger.
 *    Its own `aria-label` ("Help: …") would otherwise be folded into the
 *    trigger's name-from-content, exactly as documented on HelpTooltip.
 * 2. The disclosure caret is a CSS `::before` with `content: ""`, shaped with
 *    `clip-path`, not a glyph. Generated *text* content is part of the
 *    accessible name in real browsers, so a "▸" there would silently rename
 *    every trigger; an empty string contributes nothing.
 * 3. The region element exists even while collapsed (so `aria-controls` is
 *    never a dangling IDREF) but is `hidden` and renders no children, so a
 *    collapsed section costs nothing in DOM, data binding, or focus order.
 *
 * Arrow/Home/End move focus between sibling triggers without opening
 * anything — activation is Enter/Space only. That is the APG accordion rule
 * and the opposite of the tablist above, where selection follows focus.
 */
export function SectionDisclosure({
  id,
  label,
  help,
  expanded,
  siblings,
  onToggle,
  children,
}: {
  id: Section;
  label: string;
  help: React.ReactNode;
  expanded: boolean;
  siblings: readonly Section[];
  onToggle: () => void;
  children: React.ReactNode;
}) {
  const headerId = `inspector-section-${id}`;
  const regionId = `inspector-region-${id}`;
  return (
    <div className="inspector-accordion-item">
      <div className="inspector-accordion-header">
        <h2>
          <button
            type="button"
            id={headerId}
            className="inspector-accordion-trigger"
            aria-expanded={expanded}
            aria-controls={regionId}
            onClick={onToggle}
            onKeyDown={(event) => {
              const index = siblings.indexOf(id);
              let next = index;
              if (event.key === "ArrowDown") {
                next = (index + 1) % siblings.length;
              } else if (event.key === "ArrowUp") {
                next = (index - 1 + siblings.length) % siblings.length;
              } else if (event.key === "Home") {
                next = 0;
              } else if (event.key === "End") {
                next = siblings.length - 1;
              } else {
                return;
              }
              event.preventDefault();
              document.getElementById(`inspector-section-${siblings[next]}`)?.focus();
            }}
          >
            {label}
          </button>
        </h2>
        <HelpTooltip label={label}>{help}</HelpTooltip>
      </div>
      <div
        id={regionId}
        role="region"
        aria-labelledby={headerId}
        className="inspector-accordion-region"
        hidden={!expanded}
      >
        {expanded ? children : null}
      </div>
    </div>
  );
}
