import { describe, expect, it, vi } from "vitest";

import type { AttachmentCapabilities } from "./types";
import { performBoundUpload } from "./uploadSession";

const libraryCapabilities: AttachmentCapabilities = {
  ingestPath: "library",
  maxBytes: 100,
  maxPerUserDocuments: 100,
  maxPerSessionDocuments: 20,
  extensions: [".pdf"],
  mimeTypes: ["application/pdf"],
  modalities: ["document"],
};

const document = {
  id: "doc-1",
  userId: "u1",
  filename: "a.pdf",
  contentType: "application/pdf",
  size: 1,
  status: "ready",
  modality: "document",
  chunkCount: 1,
  citationReady: true,
  error: null,
  createdAt: "",
  updatedAt: "",
};

describe("session-bound uploads", () => {
  it("does not return a selected document until association succeeds", async () => {
    const uploadLibrary = vi.fn().mockResolvedValue(document);
    const associateLibrary = vi.fn().mockRejectedValue(new Error("association failed"));
    await expect(
      performBoundUpload({
        capabilities: libraryCapabilities,
        sessionId: "A",
        file: new File(["x"], "a.pdf"),
        isCurrent: () => true,
        uploadLibrary,
        associateLibrary,
        uploadSession: vi.fn(),
        onAssociating: vi.fn(),
      }),
    ).rejects.toThrow("association failed");
  });

  it("drops a completion after navigation before association", async () => {
    const associateLibrary = vi.fn();
    const result = await performBoundUpload({
      capabilities: libraryCapabilities,
      sessionId: "A",
      file: new File(["x"], "a.pdf"),
      isCurrent: () => false,
      uploadLibrary: vi.fn().mockResolvedValue(document),
      associateLibrary,
      uploadSession: vi.fn(),
      onAssociating: vi.fn(),
    });
    expect(result).toBeNull();
    expect(associateLibrary).not.toHaveBeenCalled();
  });

  it("uses the server-selected session ingest route", async () => {
    const uploadSession = vi.fn().mockResolvedValue(document);
    const result = await performBoundUpload({
      capabilities: { ...libraryCapabilities, ingestPath: "session" },
      sessionId: "A",
      file: new File(["x"], "a.pdf"),
      isCurrent: () => true,
      uploadLibrary: vi.fn(),
      associateLibrary: vi.fn(),
      uploadSession,
      onAssociating: vi.fn(),
    });
    expect(result?.path).toBe("session");
    expect(uploadSession).toHaveBeenCalledWith("A", expect.any(File));
  });

  it("can retry a failed association without exposing the first attempt", async () => {
    const associateLibrary = vi
      .fn()
      .mockRejectedValueOnce(new Error("temporary"))
      .mockResolvedValueOnce({
        id: "A",
        userId: "u1",
        title: "A",
        model: null,
        systemPrompt: null,
        agentName: null,
        toolOverrides: { added: [], removed: [] },
        libraryDocumentIds: ["doc-1"],
        createdAt: "",
        updatedAt: "",
      });
    const args = {
      capabilities: libraryCapabilities,
      sessionId: "A",
      file: new File(["x"], "a.pdf"),
      isCurrent: () => true,
      uploadLibrary: vi.fn().mockResolvedValue(document),
      associateLibrary,
      uploadSession: vi.fn(),
      onAssociating: vi.fn(),
    };
    await expect(performBoundUpload(args)).rejects.toThrow("temporary");
    await expect(performBoundUpload(args)).resolves.toMatchObject({
      path: "library",
      document: { id: "doc-1" },
    });
  });
});
