"use client";

// Markdown renderer for assistant turns. Renders GFM markdown (headings, lists,
// tables, code, emphasis, links) while preserving the library citation tokens
// `[[cite:FILE@MM:SS]]` as clickable chips that deep-link the media player.
//
// Raw HTML is intentionally disabled (no rehype-raw plugin): assistant text is
// model output and must never be able to inject markup. Links open in a new tab
// with a safe rel. Inline vs. block code is distinguished purely in CSS —
// react-markdown v9 dropped the `inline` prop on `code` — see the `.md` rules in
// globals.css.

import { Fragment, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { parseCitations } from "@/lib/citations";

export interface MarkdownProps {
  content: string;
  onCitation?: (filename: string, ms: number) => void;
}

// A single citation chip. Interactive (a real button) when a seek handler is
// available; otherwise a static, muted pill so the label still reads cleanly.
function CitationChip({
  label,
  filename,
  ms,
  onCitation,
}: {
  label: string;
  filename: string;
  ms: number;
  onCitation?: (filename: string, ms: number) => void;
}) {
  if (!onCitation) {
    return (
      <span
        style={{
          padding: "0 6px",
          borderRadius: 999,
          border: "1px solid var(--border)",
          color: "var(--fg-muted)",
          fontSize: "0.85em",
        }}
      >
        {label}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() => onCitation(filename, ms)}
      title={`Play ${label}`}
      aria-label={`Play ${label}`}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        margin: "0 1px",
        padding: "0 8px",
        borderRadius: 999,
        border: "1px solid var(--accent)",
        background: "transparent",
        color: "var(--accent)",
        font: "inherit",
        fontSize: "0.85em",
        lineHeight: 1.6,
        cursor: "pointer",
        verticalAlign: "baseline",
      }}
    >
      <span aria-hidden="true">▶</span>
      {label}
    </button>
  );
}

// Replace citation tokens inside the plain-text children of a block element with
// chips. The token contains no markdown syntax, so remark always emits it as a
// single literal text node; a pass over the direct string children therefore
// never splits a token. Non-string children (emphasis, links, code, …) pass
// through untouched.
function injectCitations(
  children: ReactNode,
  onCitation?: (filename: string, ms: number) => void,
): ReactNode {
  const nodes = Array.isArray(children) ? children : [children];
  const out: ReactNode[] = [];
  nodes.forEach((child, index) => {
    if (typeof child !== "string" || !child.includes("[[cite:")) {
      out.push(child);
      return;
    }
    parseCitations(child).forEach((seg, i) => {
      const key = `${index}-${i}`;
      if (seg.type === "text") {
        out.push(<Fragment key={key}>{seg.value}</Fragment>);
      } else {
        out.push(
          <CitationChip
            key={key}
            label={seg.label}
            filename={seg.filename}
            ms={seg.ms}
            onCitation={onCitation}
          />,
        );
      }
    });
  });
  return out;
}

export function Markdown({ content, onCitation }: MarkdownProps) {
  const withCitations = (children: ReactNode) => injectCitations(children, onCitation);
  // Only the text-bearing block elements need citation injection; everything
  // else uses react-markdown's defaults. `a` is overridden purely to make links
  // open safely in a new tab.
  const components: Components = {
    a: ({ children, href }) => (
      <a href={href} target="_blank" rel="noreferrer noopener">
        {children}
      </a>
    ),
    p: ({ children }) => <p>{withCitations(children)}</p>,
    li: ({ children }) => <li>{withCitations(children)}</li>,
    td: ({ children }) => <td>{withCitations(children)}</td>,
    th: ({ children }) => <th>{withCitations(children)}</th>,
  };
  return (
    <div className="md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  );
}
