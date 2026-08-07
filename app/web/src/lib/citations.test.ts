import { describe, expect, it } from "vitest";

import {
  hasCitations,
  msToTimecode,
  parseCitations,
  timecodeToMs,
  type CitationToken,
} from "./citations";
import type { RetrievedSource } from "./types";

// Audit P1-14. This parser is one half of a contract: the grammar and the
// "verified means the id is in the registry" rule are mirrored from
// `app/api/src/ai4ia_api/citations.py`, and the tests there assert the same
// cases. Anything this module fails to parse is a citation nobody checks.

function span(over: Partial<RetrievedSource> = {}): RetrievedSource {
  return {
    spanId: "S1",
    documentId: "doc-1",
    filename: "report.pdf",
    excerpt: "Revenue grew twenty percent.",
    contentSha256: "a".repeat(64),
    retrievedAt: "2026-08-07T00:00:00Z",
    ...over,
  };
}

const cites = (segments: ReturnType<typeof parseCitations>): CitationToken[] =>
  segments.filter((s): s is CitationToken => s.type === "cite");

describe("timecode conversion", () => {
  it("round-trips the API's m:ss and h:mm:ss shapes", () => {
    expect(timecodeToMs("2:13")).toBe(133_000);
    expect(timecodeToMs("1:02:03")).toBe(3_723_000);
    expect(msToTimecode(133_000)).toBe("2:13");
    expect(msToTimecode(3_723_000)).toBe("1:02:03");
    // Matches format_timestamp: minutes are unpadded below an hour, padded above.
    expect(msToTimecode(0)).toBe("0:00");
    expect(msToTimecode(59_000)).toBe("0:59");
  });
});

describe("parseCitations on an attested turn", () => {
  it("verifies an id that is in the registry", () => {
    const tokens = cites(parseCitations("Grew [[cite:S1]].", [span()]));
    expect(tokens).toHaveLength(1);
    expect(tokens[0].status).toBe("verified");
    expect(tokens[0].documentId).toBe("doc-1");
  });

  it("reports an id that is not, for the same sentence", () => {
    // The control for the test above: only the id differs.
    const tokens = cites(parseCitations("Grew [[cite:S4]].", [span()]));
    expect(tokens).toHaveLength(1);
    expect(tokens[0].status).toBe("unverified");
    expect(tokens[0].documentId).toBeNull();
    expect(tokens[0].raw).toBe("[[cite:S4]]");
  });

  it.each([
    ["lower case", "[[cite:s1]]"],
    ["padded", "[[cite: S1 ]]"],
  ])("accepts a %s id the model may write", (_label, token) => {
    expect(cites(parseCitations(`Grew ${token}.`, [span()]))[0].status).toBe(
      "verified",
    );
  });

  it("splits a grouped token so one bad id cannot hide behind a good one", () => {
    const tokens = cites(parseCitations("Both [[cite:S1,S9]].", [span()]));
    expect(tokens.map((t) => t.status)).toEqual(["verified", "unverified"]);
  });

  it("reports a token whose payload names no id at all", () => {
    // Including the legacy filename form: on an attested turn the app cannot
    // say which document a bare name meant, so it must not pretend to.
    const tokens = cites(
      parseCitations("Old [[cite:lecture.mp3@2:13]].", [span()]),
    );
    expect(tokens.map((t) => t.status)).toEqual(["unverified"]);
    expect(tokens[0].documentId).toBeNull();
  });

  it("treats an empty registry as evidence, not as an absent one", () => {
    // Retrieval ran and injected nothing, so a cited id is provably invented.
    expect(cites(parseCitations("Grew [[cite:S1]].", []))[0].status).toBe(
      "unverified",
    );
  });

  it("labels a verified media span with its own filename and offset", () => {
    const tokens = cites(
      parseCitations("Here [[cite:S1]].", [
        span({ filename: "lecture.mp3", startMs: 133_000 }),
      ]),
    );
    expect(tokens[0].label).toBe("lecture.mp3 · 2:13");
    expect(tokens[0].ms).toBe(133_000);
  });

  it("keeps the surrounding text verbatim", () => {
    const segments = parseCitations("a [[cite:S1]] b", [span()]);
    expect(segments.map((s) => (s.type === "text" ? s.value : "<cite>"))).toEqual(
      ["a ", "<cite>", " b"],
    );
  });
});

describe("parseCitations on an unattested turn", () => {
  it("renders a legacy media token exactly as it always did", () => {
    const tokens = cites(parseCitations("Listen [[cite:lecture.mp3@12:34]] here"));
    expect(tokens).toHaveLength(1);
    expect(tokens[0].status).toBe("unattested");
    expect(tokens[0].filename).toBe("lecture.mp3");
    expect(tokens[0].ms).toBe(12 * 60_000 + 34_000);
  });

  it("makes no accusation about a span id it cannot check", () => {
    // No registry means no evidence either way. Marking this unverified would
    // be an accusation built out of a missing field.
    expect(cites(parseCitations("Grew [[cite:S1]]."))).toHaveLength(0);
    expect(hasCitations("Grew [[cite:S1]].")).toBe(false);
  });

  it("leaves plain text untouched", () => {
    expect(parseCitations("no tokens here")).toEqual([
      { type: "text", value: "no tokens here" },
    ]);
    expect(parseCitations("")).toEqual([]);
    expect(hasCitations("no tokens here")).toBe(false);
  });
});
