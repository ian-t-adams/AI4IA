// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { LibraryDocument } from "@/lib/library";
import { LibraryPanel } from "./LibraryPanel";

const mocks = vi.hoisted(() => ({
  listLibraryDocuments: vi.fn(),
  listLibraryAnalyzers: vi.fn(),
  listSharedWithMe: vi.fn(),
  deleteLibraryDocument: vi.fn(),
  uploadLibraryDocument: vi.fn(),
  saveLibraryDocumentToMemory: vi.fn(),
  forgetLibraryDocumentFromMemory: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  listLibraryDocuments: mocks.listLibraryDocuments,
  listLibraryAnalyzers: mocks.listLibraryAnalyzers,
  listSharedWithMe: mocks.listSharedWithMe,
  deleteLibraryDocument: mocks.deleteLibraryDocument,
  uploadLibraryDocument: mocks.uploadLibraryDocument,
  saveLibraryDocumentToMemory: mocks.saveLibraryDocumentToMemory,
  forgetLibraryDocumentFromMemory: mocks.forgetLibraryDocumentFromMemory,
}));

const DOC: LibraryDocument = {
  id: "doc1",
  filename: "report.pdf",
  contentType: "application/pdf",
  size: 2048,
  modality: "document",
  status: "ready",
  analyzerId: null,
  summary: "A report.",
  chunkCount: 3,
  visibility: "private",
  createdAt: "2024-01-01T00:00:00Z",
  updatedAt: "2024-01-01T00:00:00Z",
};

beforeEach(() => {
  mocks.listLibraryDocuments.mockResolvedValue([DOC]);
  mocks.listLibraryAnalyzers.mockResolvedValue([]);
  mocks.listSharedWithMe.mockResolvedValue([]);
  mocks.deleteLibraryDocument.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});

describe("LibraryPanel delete", () => {
  it("names the delete button for permanence and confirms with irreversible wording before calling the API", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<LibraryPanel onClose={vi.fn()} />);

    const deleteButton = await screen.findByRole("button", {
      name: "Permanently delete report.pdf",
    });
    await user.click(deleteButton);

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    const confirmText = confirmSpy.mock.calls[0][0];
    expect(confirmText).toMatch(/permanently delete/i);
    expect(confirmText).toMatch(/can't be undone/i);

    await waitFor(() =>
      expect(mocks.deleteLibraryDocument).toHaveBeenCalledWith("doc1"),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "Permanently delete report.pdf" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("does not delete when the confirmation is dismissed", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    render(<LibraryPanel onClose={vi.fn()} />);

    const deleteButton = await screen.findByRole("button", {
      name: "Permanently delete report.pdf",
    });
    await user.click(deleteButton);

    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(mocks.deleteLibraryDocument).not.toHaveBeenCalled();
    expect(
      screen.getByRole("button", { name: "Permanently delete report.pdf" }),
    ).toBeInTheDocument();
  });
});
