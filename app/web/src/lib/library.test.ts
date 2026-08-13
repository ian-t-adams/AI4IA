import { describe, expect, it } from "vitest";

import {
  formatBytes,
  keepMonotonicLibraryDocument,
  LIBRARY_STATUS_COLORS,
  LIBRARY_STATUS_LABELS,
  type LibraryDocument,
} from "./library";

function document(status: LibraryDocument["status"]): LibraryDocument {
  return {
    id: "doc-1",
    filename: "report.pdf",
    contentType: "application/pdf",
    size: 1,
    modality: "document",
    status,
    analyzerId: null,
    summary: "",
    chunkCount: 0,
    visibility: "private",
    createdAt: "2026-08-09T00:00:00Z",
    updatedAt: "2026-08-09T00:00:00Z",
  };
}

function at(
  status: LibraryDocument["status"],
  updatedAt: string,
  summary = "",
): LibraryDocument {
  return { ...document(status), updatedAt, summary };
}

describe("keepMonotonicLibraryDocument", () => {
  it("accepts forward ingest progress", () => {
    expect(
      keepMonotonicLibraryDocument(
        at("analyzing", "2026-08-09T00:00:01Z"),
        at("ready", "2026-08-09T00:00:02Z"),
      ).status,
    ).toBe("ready");
  });

  describe("library presentation", () => {
    it("defines one label and color for every ingest state", () => {
      const statuses: LibraryDocument["status"][] = [
        "pending",
        "stored",
        "analyzing",
        "ready",
        "failed",
      ];
      expect(Object.keys(LIBRARY_STATUS_LABELS).sort()).toEqual([...statuses].sort());
      expect(Object.keys(LIBRARY_STATUS_COLORS).sort()).toEqual([...statuses].sort());
      expect(LIBRARY_STATUS_LABELS.stored).toBe("Stored");
    });

    it("formats byte sizes consistently", () => {
      expect(formatBytes(512)).toBe("512 B");
      expect(formatBytes(1536)).toBe("1.5 KB");
      expect(formatBytes(1.5 * 1024 * 1024)).toBe("1.5 MB");
    });
  });

  it("rejects a stale response that regresses a terminal document", () => {
    expect(
      keepMonotonicLibraryDocument(
        document("ready"),
        document("analyzing"),
      ).status,
    ).toBe("ready");
  });

  it("keeps ready when an older failed snapshot arrives", () => {
    expect(
      keepMonotonicLibraryDocument(
        at("ready", "2026-08-09T00:00:02Z"),
        at("failed", "2026-08-09T00:00:01Z"),
      ).status,
    ).toBe("ready");
  });

  it("keeps newer metadata when an equal-status stale snapshot arrives", () => {
    expect(
      keepMonotonicLibraryDocument(
        at("ready", "2026-08-09T00:00:02Z", "new"),
        at("ready", "2026-08-09T00:00:01Z", "old"),
      ).summary,
    ).toBe("new");
  });

  it("accepts a genuinely newer retry transition", () => {
    expect(
      keepMonotonicLibraryDocument(
        at("failed", "2026-08-09T00:00:01Z"),
        at("stored", "2026-08-09T00:00:02Z"),
      ).status,
    ).toBe("stored");
  });
});
