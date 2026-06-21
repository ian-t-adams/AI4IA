// Citation parsing. The document-retrieval layer asks the model to cite
// a specific moment in an audio/video library document with an exact token,
// `[[cite:FILENAME@MM:SS]]` (see api `library/retrieval.py`). This module turns an
// assistant message into renderable segments so the UI can show those tokens as
// clickable chips that deep-link the media player, instead of leaking the raw
// token as text. When no token is present the whole message is one text segment,
// so non-cited answers render exactly as before.

// A single timecode is `M:SS`, `MM:SS`, or `H:MM:SS`. Filename is everything up to
// the `@`, trimmed; it must not contain `]` or `@` so the token can't run away.
const CITATION_RE =
  /\[\[cite:\s*([^\]@]+?)\s*@\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*\]\]/g;

export interface CitationToken {
  type: "cite";
  filename: string;
  // Seek target in milliseconds, parsed from the token's timecode.
  ms: number;
  // The human-facing label rendered on the chip (filename + timecode).
  label: string;
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

// Split an assistant message into ordered text/citation segments. Adjacent text
// is preserved verbatim (including whitespace) so `whiteSpace: pre-wrap` rendering
// is unchanged; only the matched tokens become citation segments.
export function parseCitations(text: string): MessageSegment[] {
  if (!text || !text.includes("[[cite:")) {
    return text ? [{ type: "text", value: text }] : [];
  }
  const segments: MessageSegment[] = [];
  let lastIndex = 0;
  CITATION_RE.lastIndex = 0;
  let match: RegExpExecArray | null;
  while ((match = CITATION_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      segments.push({ type: "text", value: text.slice(lastIndex, match.index) });
    }
    const filename = match[1].trim();
    const timecode = match[2];
    segments.push({
      type: "cite",
      filename,
      ms: timecodeToMs(timecode),
      label: `${filename} · ${timecode}`,
    });
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    segments.push({ type: "text", value: text.slice(lastIndex) });
  }
  return segments;
}

// True when a message carries at least one parseable citation token.
export function hasCitations(text: string): boolean {
  if (!text || !text.includes("[[cite:")) return false;
  CITATION_RE.lastIndex = 0;
  return CITATION_RE.test(text);
}
