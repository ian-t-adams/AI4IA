// Citation parsing (audit P1-14). Retrieval mints a server-owned span id for
// every excerpt it injects into a turn and tells the model to cite that id with
// `[[cite:S1]]`. This module turns an assistant message into renderable segments
// and, when the turn carries its span registry, resolves each token against it —
// so a citation the model invented does not render identically to one that
// actually came from a retrieved span.
//
// Two rules are mirrored from `app/api/src/ai4ia_api/citations.py` and must stay
// in step with it: the token grammar, and "verified means the id is in the
// registry". Nothing here re-derives a verdict the server did not reach, and
// nothing here claims the cited span *supports* the sentence — that is the
// reader's judgement, which is why the excerpt travels with the answer.
//
// Legacy `[[cite:FILENAME@MM:SS]]` tokens predate span ids. On an UNATTESTED
// message (no registry — every row written before this feature) they still
// render exactly as they always did. On an attested one they are unresolvable,
// because a bare filename cannot say which document was meant, so they show as
// unverified rather than silently deep-linking to whichever ready file happened
// to match the name first.

import type { RetrievedSource } from "@/lib/types";

// One or more span ids in a single token: `[[cite:S1]]`, `[[cite:S1,S3]]`.
const SPAN_ID = String.raw`[sS]\d{1,3}`;
const SPAN_CITATION_RE = new RegExp(
  String.raw`^\[\[cite:\s*(${SPAN_ID}(?:\s*[,;]\s*${SPAN_ID})*)\s*\]\]$`,
);
// A single timecode is `M:SS`, `MM:SS`, or `H:MM:SS`. Filename is everything up
// to the `@`, trimmed; it must not contain `]` or `@` so the token can't run away.
const LEGACY_CITATION_RE =
  /^\[\[cite:\s*([^\]@]+?)\s*@\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]\]$/;
// Anything token-shaped. Scanning with this rather than with the specific forms
// is deliberate: a token nobody parses is a citation nobody checks.
const ANY_CITATION_RE = /\[\[cite:[^\]\n]{0,200}\]\]/g;
const ID_SPLIT_RE = /[,;]/;

export type SegmentCitationStatus = "verified" | "unverified" | "unattested";

export interface CitationToken {
  type: "cite";
  status: SegmentCitationStatus;
  // Resolved from the registry. Null for a legacy or unresolvable token.
  spanId: string | null;
  documentId: string | null;
  filename: string | null;
  // Media seek target in milliseconds; null when the span is not time-grounded.
  ms: number | null;
  // The human-facing label rendered on the chip.
  label: string;
  // Exactly what the model wrote, kept for an unverified token.
  raw: string;
}

export interface TextSegment {
  type: "text";
  value: string;
}

export type MessageSegment = TextSegment | CitationToken;

// Parse `M:SS`, `MM:SS`, or `H:MM:SS` into milliseconds. Returns 0 on a malformed
// value (the regex already guarantees the shape, so this is just arithmetic).
export function timecodeToMs(timecode: string): number {
  const parts = timecode.split(":").map((p) => parseInt(p, 10));
  if (parts.some((n) => Number.isNaN(n))) return 0;
  let seconds = 0;
  for (const part of parts) {
    seconds = seconds * 60 + part;
  }
  return seconds * 1000;
}

// `m:ss` (or `h:mm:ss`) label for a media offset, matching the API's format.
export function msToTimecode(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const seconds = String(total % 60).padStart(2, "0");
  if (total < 3600) return `${Math.floor(total / 60)}:${seconds}`;
  const minutes = String(Math.floor(total / 60) % 60).padStart(2, "0");
  return `${Math.floor(total / 3600)}:${minutes}:${seconds}`;
}

function labelFor(source: RetrievedSource): string {
  const timecode =
    typeof source.startMs === "number" ? msToTimecode(source.startMs) : null;
  return timecode ? `${source.filename} · ${timecode}` : source.filename;
}

function verifiedToken(source: RetrievedSource, raw: string): CitationToken {
  return {
    type: "cite",
    status: "verified",
    spanId: source.spanId,
    documentId: source.documentId,
    filename: source.filename,
    ms: typeof source.startMs === "number" ? source.startMs : null,
    label: labelFor(source),
    raw,
  };
}

function unverifiedToken(spanId: string | null, raw: string): CitationToken {
  return {
    type: "cite",
    status: "unverified",
    spanId,
    documentId: null,
    filename: null,
    ms: null,
    label: spanId ?? "unknown source",
    raw,
  };
}

function legacyToken(filename: string, timecode: string): CitationToken {
  return {
    type: "cite",
    status: "unattested",
    spanId: null,
    documentId: null,
    filename,
    ms: timecodeToMs(timecode),
    label: `${filename} · ${timecode}`,
    raw: `[[cite:${filename}@${timecode}]]`,
  };
}

// Expand one matched token into the segments it stands for. A grouped token
// yields one chip per id, because each id is a separate claim to check.
function expandToken(
  raw: string,
  byId: Map<string, RetrievedSource> | null,
): CitationToken[] {
  const spanMatch = SPAN_CITATION_RE.exec(raw);
  if (spanMatch) {
    const registry = byId;
    if (registry === null) return [];
    return spanMatch[1]
      .split(ID_SPLIT_RE)
      .map((part) => part.trim().toUpperCase())
      .map((id) => {
        const source = registry.get(id);
        return source ? verifiedToken(source, raw) : unverifiedToken(id, raw);
      });
  }
  const legacyMatch = LEGACY_CITATION_RE.exec(raw);
  if (legacyMatch && byId === null) {
    return [legacyToken(legacyMatch[1].trim(), legacyMatch[2])];
  }
  if (byId === null) return [];
  return [unverifiedToken(null, raw)];
}

// Split an assistant message into ordered text/citation segments. Adjacent text
// is preserved verbatim (including whitespace) so `whiteSpace: pre-wrap` rendering
// is unchanged; only matched tokens become citation segments.
//
// `sources` is the turn's registry: `undefined`/`null` means the turn was never
// attested, so only legacy tokens are lifted out (today's behaviour, unchanged).
// An array — including an empty one — means retrieval ran, so every token is
// resolved against it and anything missing is reported.
export function parseCitations(
  text: string,
  sources?: RetrievedSource[] | null,
): MessageSegment[] {
  if (!text || !text.includes("[[cite:")) {
    return text ? [{ type: "text", value: text }] : [];
  }
  // `new Map(array.map(...))` would infer `(string | RetrievedSource)[][]`
  // rather than a tuple list, so build it explicitly.
  let byId: Map<string, RetrievedSource> | null = null;
  if (sources != null) {
    byId = new Map<string, RetrievedSource>();
    for (const source of sources) {
      byId.set(source.spanId.toUpperCase(), source);
    }
  }
  const segments: MessageSegment[] = [];
  let lastIndex = 0;
  ANY_CITATION_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = ANY_CITATION_RE.exec(text)) !== null) {
    const expanded = expandToken(match[0], byId);
    if (expanded.length === 0) continue;
    if (match.index > lastIndex) {
      segments.push({ type: "text", value: text.slice(lastIndex, match.index) });
    }
    segments.push(...expanded);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ type: "text", value: text.slice(lastIndex) });
  }
  return segments;
}

// True when a message carries at least one token this renderer would lift out.
export function hasCitations(
  text: string,
  sources?: RetrievedSource[] | null,
): boolean {
  return parseCitations(text, sources).some((s) => s.type === "cite");
}
