// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
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
  vi.resetAllMocks();
  mocks.listLibraryDocuments.mockResolvedValue([DOC]);
  mocks.listLibraryAnalyzers.mockResolvedValue([]);
  mocks.listSharedWithMe.mockResolvedValue([]);
  mocks.deleteLibraryDocument.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
  vi.useRealTimers();
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

  it("does not let an older poll reinsert a successfully deleted document", async () => {
    let poll!: () => Promise<void>;
    let resolvePoll!: (documents: LibraryDocument[]) => void;
    vi.spyOn(window, "setInterval").mockImplementation((handler) => {
      poll = handler as () => Promise<void>;
      return 1 as unknown as ReturnType<typeof setInterval>;
    });
    const analyzing = { ...DOC, status: "analyzing" as const };
    mocks.listLibraryDocuments
      .mockResolvedValueOnce([analyzing])
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePoll = resolve;
          }),
      );
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const user = userEvent.setup();
    render(<LibraryPanel onClose={vi.fn()} />);
    const remove = await screen.findByRole("button", {
      name: "Permanently delete report.pdf",
    });
    await act(async () => {
      void poll();
      await Promise.resolve();
    });
    expect(mocks.listLibraryDocuments).toHaveBeenCalledTimes(2);

    await user.click(remove);
    await waitFor(() =>
      expect(
        screen.queryByRole("button", {
          name: "Permanently delete report.pdf",
        }),
      ).not.toBeInTheDocument(),
    );
    await act(async () => {
      resolvePoll([{ ...DOC, status: "ready" }]);
      await Promise.resolve();
    });

    expect(
      screen.queryByRole("button", {
        name: "Permanently delete report.pdf",
      }),
    ).not.toBeInTheDocument();
  });
});

describe("LibraryPanel uploads and polling", () => {
  it("gives the file picker an accessible name", async () => {
    render(<LibraryPanel onClose={vi.fn()} />);
    expect(
      await screen.findByLabelText("Upload library documents"),
    ).toHaveAttribute("type", "file");
  });

  it("continues a batch after one file fails and reports that file by name", async () => {
    const uploaded = {
      ...DOC,
      id: "doc2",
      filename: "good.pdf",
    };
    mocks.uploadLibraryDocument
      .mockRejectedValueOnce(new Error("unsupported contents"))
      .mockResolvedValueOnce(uploaded);
    mocks.listLibraryDocuments
      .mockResolvedValueOnce([DOC]);
    const user = userEvent.setup();
    render(<LibraryPanel onClose={vi.fn()} />);
    const input = await screen.findByLabelText("Upload library documents");

    await user.upload(input, [
      new File(["bad"], "bad.pdf", { type: "application/pdf" }),
      new File(["good"], "good.pdf", { type: "application/pdf" }),
    ]);

    await waitFor(() =>
      expect(mocks.uploadLibraryDocument).toHaveBeenCalledTimes(2),
    );
    expect(mocks.uploadLibraryDocument.mock.calls.map(([file]) => file.name)).toEqual([
      "bad.pdf",
      "good.pdf",
    ]);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "bad.pdf: unsupported contents",
    );
    expect(screen.getByText("good.pdf")).toBeInTheDocument();
  });

  it("uses a direct retry response to move a failed document back into progress", async () => {
    const failed = { ...DOC, status: "failed" as const, error: "old failure" };
    const analyzing = {
      ...DOC,
      status: "analyzing" as const,
      error: null,
    };
    mocks.listLibraryDocuments.mockResolvedValueOnce([failed]);
    mocks.uploadLibraryDocument.mockResolvedValueOnce(analyzing);
    const user = userEvent.setup();
    render(<LibraryPanel onClose={vi.fn()} />);
    expect(await screen.findByText("Failed")).toBeInTheDocument();

    await user.upload(screen.getByLabelText("Upload library documents"), [
      new File(["retry"], "report.pdf", { type: "application/pdf" }),
    ]);

    expect(await screen.findByText("Analyzing…")).toBeInTheDocument();
    expect(screen.queryByText("Failed")).not.toBeInTheDocument();
  });

  it("invalidates an older poll after every successful file in a batch", async () => {
    vi.useFakeTimers();
    let resolvePoll!: (documents: LibraryDocument[]) => void;
    let resolveSecond!: (document: LibraryDocument) => void;
    const first = {
      ...DOC,
      id: "first",
      filename: "first.pdf",
      status: "analyzing" as const,
    };
    const second = {
      ...DOC,
      id: "second",
      filename: "second.pdf",
      status: "ready" as const,
    };
    mocks.listLibraryDocuments
      .mockResolvedValueOnce([])
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePoll = resolve;
          }),
      );
    mocks.uploadLibraryDocument
      .mockResolvedValueOnce(first)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSecond = resolve;
          }),
      );
    render(<LibraryPanel onClose={vi.fn()} />);
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("No documents yet.")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Upload library documents"), {
      target: {
        files: [
          new File(["first"], "first.pdf", { type: "application/pdf" }),
          new File(["second"], "second.pdf", { type: "application/pdf" }),
        ],
      },
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("first.pdf")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(mocks.listLibraryDocuments).toHaveBeenCalledTimes(2);

    await act(async () => {
      resolveSecond(second);
      await Promise.resolve();
    });
    expect(screen.getByText("second.pdf")).toBeInTheDocument();
    await act(async () => {
      resolvePoll([first]);
      await Promise.resolve();
    });

    expect(screen.getByText("second.pdf")).toBeInTheDocument();
  });

  it("keeps initial analyzers and shared documents when an upload supersedes the initial list", async () => {
    let resolveInitial!: (documents: LibraryDocument[]) => void;
    const uploaded = { ...DOC, id: "uploaded", filename: "uploaded.pdf" };
    const shared = {
      ...DOC,
      id: "shared",
      filename: "shared.pdf",
      visibility: "shared",
    };
    mocks.listLibraryDocuments.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveInitial = resolve;
        }),
    );
    mocks.listLibraryAnalyzers.mockResolvedValueOnce([
      {
        id: "layout",
        name: "Layout",
        description: "Layout analyzer",
        kind: "builtin",
        modalities: ["document"],
        baseAnalyzerId: null,
      },
    ]);
    mocks.listSharedWithMe.mockResolvedValueOnce([shared]);
    mocks.uploadLibraryDocument.mockResolvedValueOnce(uploaded);
    const user = userEvent.setup();
    render(<LibraryPanel onClose={vi.fn()} />);

    await user.upload(screen.getByLabelText("Upload library documents"), [
      new File(["new"], "uploaded.pdf", { type: "application/pdf" }),
    ]);
    await waitFor(() =>
      expect(mocks.uploadLibraryDocument).toHaveBeenCalledTimes(1),
    );

    await act(async () => {
      resolveInitial([]);
      await Promise.resolve();
    });
    expect(screen.getByRole("combobox", { name: "Analyzer" })).toBeInTheDocument();
    expect(screen.getByText("shared.pdf")).toBeInTheDocument();
    expect(screen.getByText("uploaded.pdf")).toBeInTheDocument();
  });

  it("keeps polling single-flight and never regresses a ready document", async () => {
    let resolvePoll!: (documents: LibraryDocument[]) => void;
    let poll!: () => Promise<void>;
    vi.spyOn(window, "setInterval").mockImplementation((handler) => {
      poll = handler as () => Promise<void>;
      return 1 as unknown as ReturnType<typeof setInterval>;
    });
    const analyzing = { ...DOC, status: "analyzing" as const };
    const ready = { ...DOC, status: "ready" as const };
    mocks.listLibraryDocuments
      .mockResolvedValueOnce([analyzing])
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolvePoll = resolve;
          }),
      )
      .mockResolvedValueOnce([ready]);
    mocks.uploadLibraryDocument.mockResolvedValue(ready);
    render(<LibraryPanel onClose={vi.fn()} />);
    expect(await screen.findByText("Analyzing…")).toBeInTheDocument();

    await act(async () => {
      void poll();
      await Promise.resolve();
      void poll();
    });
    expect(mocks.listLibraryDocuments).toHaveBeenCalledTimes(2);

    fireEvent.change(screen.getByLabelText("Upload library documents"), {
      target: {
        files: [new File(["new"], "new.pdf", { type: "application/pdf" })],
      },
    });
    expect(await screen.findByText("Ready")).toBeInTheDocument();

    await act(async () => {
      resolvePoll([analyzing]);
      await Promise.resolve();
    });
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(screen.queryByText("Analyzing…")).not.toBeInTheDocument();
  });
});
