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

import {
  Children,
  Fragment,
  cloneElement,
  isValidElement,
  type ReactNode,
} from "react";
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
  // The attested/unattested distinction is a property of the citation, not of
  // whether a handler happens to be wired up, so it is computed once and applied
  // to BOTH branches below. Scoping it to the static branch was a real defect: a
  // legacy token carries a filename and a timecode, so it is `seekable`, and with
  // `onCitation` supplied (which production does whenever the library is enabled)
  // it fell through to the actionable branch and was painted in the same accent as
  // a verified span. The clickable case is exactly where the reader is most likely
  // to trust the chip, so it is the last place the verdict may be dropped.
  const attested = token.status === "verified";
  if (!onCitation || !seekable) {
    // Not actionable. Still distinguish an attested source (accent, and it names
    // its span id) from a legacy token we can say nothing about (muted), so
    // "verified" is never indistinguishable from "we have no idea".
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
      title={
        attested
          ? `Play ${token.label} (source ${token.spanId})`
          : `Play ${token.label} (source not verified for this answer)`
      }
      aria-label={
        attested
          ? `Play ${token.label}, verified source ${token.spanId}`
          : `Play ${token.label}, source not verified for this answer`
      }
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        margin: "0 1px",
        padding: "0 8px",
        borderRadius: 999,
        border: attested ? "1px solid var(--accent)" : "1px solid var(--border)",
        background: "transparent",
        color: attested ? "var(--accent)" : "var(--fg-muted)",
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

// Replace citation tokens anywhere inside a text-bearing block, including
// emphasis/strong wrappers. Code and links are deliberate boundaries: citations
// inside code are examples, and turning link text into a button would create
// nested interactive content. The old direct-child pass let
// `**[[cite:S9]]**` and heading citations remain raw, bypassing the verified /
// unverified distinction.
function injectCitations(
  children: ReactNode,
  onCitation?: (target: CitationTarget) => void,
  sources?: RetrievedSource[] | null,
): ReactNode {
  return Children.map(children, (child, index) => {
    if (typeof child !== "string" || !child.includes("[[cite:")) {
      if (
        isValidElement<{ children?: ReactNode }>(child) &&
        child.props.children !== undefined &&
        child.type !== "code" &&
        child.type !== "pre" &&
        child.type !== "a"
      ) {
        return cloneElement(
          child,
          undefined,
          injectCitations(child.props.children, onCitation, sources),
        );
      }
      return child;
    }
    return parseCitations(child, sources).map((seg, i) => {
      const key = `${index}-${i}`;
      if (seg.type === "text") {
        return <Fragment key={key}>{seg.value}</Fragment>;
      }
      return <CitationChip key={key} token={seg} onCitation={onCitation} />;
    });
  });
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
    // Never let model/tool-authored Markdown make an automatic browser request
    // to an attacker-controlled (or local-network) URL. Generated image
    // artifacts are rendered by their authenticated object-URL component, not
    // through Markdown.
    img: ({ alt }) => (
      <span className="md-image-omitted" role="note">
        {alt ? `[External image omitted: ${alt}]` : "[External image omitted]"}
      </span>
    ),
    p: ({ children }) => <p>{withCitations(children)}</p>,
    h1: ({ children }) => <h1>{withCitations(children)}</h1>,
    h2: ({ children }) => <h2>{withCitations(children)}</h2>,
    h3: ({ children }) => <h3>{withCitations(children)}</h3>,
    h4: ({ children }) => <h4>{withCitations(children)}</h4>,
    h5: ({ children }) => <h5>{withCitations(children)}</h5>,
    h6: ({ children }) => <h6>{withCitations(children)}</h6>,
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
