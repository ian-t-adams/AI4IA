"use client";

// Markdown renderer for assistant turns. Renders GFM markdown (headings, lists,
// tables, code, emphasis, links) while lifting library citation tokens out as
// chips (see `@/lib/citations`).
//
// A verified citation and an unverified one must not look the same: the whole
// point of P1-14 is that "the model wrote this" and "this came from a span we
// actually retrieved" are different statements. Verified chips keep the accent
// affordance and stay actionable; unverified ones carry the danger token, a
// warning glyph, and an explanation, and are deliberately NOT actionable —
// there is nothing to open, because nothing was retrieved.
//
// Colours come from theme tokens only. A literal hex here would be wrong in at
// least one of the three themes (see AGENTS.md, "Change the brand palette"), and
// `themeTokens.test.ts` fails the build for it.
//
// Raw HTML is intentionally disabled (no rehype-raw plugin): assistant text is
// model output and must never be able to inject markup. Links open in a new tab
// with a safe rel. Inline vs. block code is distinguished purely in CSS —
// react-markdown v9 dropped the `inline` prop on `code` — see the `.md` rules in
// globals.css.

import { Fragment, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { parseCitations, type CitationToken } from "@/lib/citations";
import type { RetrievedSource } from "@/lib/types";

// What a click on a verified citation asks the app to open. `documentId` is
// identity; `filename` is only ever a label.
export interface CitationTarget {
  documentId: string | null;
  filename: string | null;
  ms: number | null;
}

export interface MarkdownProps {
  content: string;
  onCitation?: (target: CitationTarget) => void;
  // The turn's span registry. Absent/null means the turn was never attested, so
  // citations render exactly as they did before this feature.
  sources?: RetrievedSource[] | null;
}

const UNVERIFIED_EXPLANATION =
  "This citation names a source that was not retrieved for this answer.";

// A single citation chip. Interactive (a real button) only when the citation
// resolved to a real span AND a handler is available; otherwise a static pill,
// so an unverifiable citation is never dressed up as something you can open.
function CitationChip({
  token,
  onCitation,
}: {
  token: CitationToken;
  onCitation?: (target: CitationTarget) => void;
}) {
  if (token.status === "unverified") {
    return (
      <span
        title={`${UNVERIFIED_EXPLANATION} Model wrote: ${token.raw}`}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          margin: "0 1px",
          padding: "0 8px",
          borderRadius: 999,
          border: "1px dashed var(--danger)",
          color: "var(--danger)",
          fontSize: "0.85em",
          lineHeight: 1.6,
          verticalAlign: "baseline",
        }}
      >
        <span aria-hidden="true">⚠</span>
        <span>Unverified citation</span>
        <span className="visually-hidden">
          {` ${UNVERIFIED_EXPLANATION} The model wrote ${token.raw}.`}
        </span>
      </span>
    );
  }
  const seekable =
    token.ms !== null && (token.documentId !== null || token.filename !== null);
  if (!onCitation || !seekable) {
    // Not actionable. Still distinguish an attested source (accent, and it names
    // its span id) from a legacy token we can say nothing about (muted), so
    // "verified" is never indistinguishable from "we have no idea".
    const attested = token.status === "verified";
    return (
      <span
        title={
          attested
            ? `Source ${token.spanId}: ${token.label}`
            : token.label
        }
        style={{
          padding: "0 6px",
          borderRadius: 999,
          border: attested
            ? "1px solid var(--accent)"
            : "1px solid var(--border)",
          color: attested ? "var(--accent)" : "var(--fg-muted)",
          fontSize: "0.85em",
        }}
      >
        {token.label}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={() =>
        onCitation({
          documentId: token.documentId,
          filename: token.filename,
          ms: token.ms,
        })
      }
      title={`Play ${token.label}`}
      aria-label={`Play ${token.label}`}
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
      {token.label}
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
  onCitation?: (target: CitationTarget) => void,
  sources?: RetrievedSource[] | null,
): ReactNode {
  const nodes = Array.isArray(children) ? children : [children];
  const out: ReactNode[] = [];
  nodes.forEach((child, index) => {
    if (typeof child !== "string" || !child.includes("[[cite:")) {
      out.push(child);
      return;
    }
    parseCitations(child, sources).forEach((seg, i) => {
      const key = `${index}-${i}`;
      if (seg.type === "text") {
        out.push(<Fragment key={key}>{seg.value}</Fragment>);
      } else {
        out.push(<CitationChip key={key} token={seg} onCitation={onCitation} />);
      }
    });
  });
  return out;
}

export function Markdown({ content, onCitation, sources }: MarkdownProps) {
  const withCitations = (children: ReactNode) =>
    injectCitations(children, onCitation, sources);
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
