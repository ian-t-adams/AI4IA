// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { DocumentAnnotation } from "@/lib/library";
import AnnotationsPanel from "./AnnotationsPanel";

const mocks = vi.hoisted(() => ({
  createLibraryAnnotation: vi.fn(),
  deleteLibraryAnnotation: vi.fn(),
  listLibraryAnnotations: vi.fn(),
  updateLibraryAnnotation: vi.fn(),
}));

vi.mock("@/lib/api", () => mocks);

const NOTE: DocumentAnnotation = {
  id: "note-1",
  body: "Check this claim",
  anchor: "p. 3",
  createdAt: "2026-08-09T12:00:00Z",
  updatedAt: "2026-08-09T12:00:00Z",
};

beforeEach(() => {
  mocks.listLibraryAnnotations.mockResolvedValue([NOTE]);
  mocks.deleteLibraryAnnotation.mockResolvedValue(undefined);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("AnnotationsPanel accessibility", () => {
  it("keeps dialog semantics and closes only from Escape or the backdrop", async () => {
    const onClose = vi.fn();
    render(
      <AnnotationsPanel
        documentId="doc-1"
        filename="report.pdf"
        onClose={onClose}
      />,
    );
    const dialog = screen.getByRole("dialog", { name: "Notes for report.pdf" });
    expect(dialog).toHaveAttribute("aria-modal", "true");

    fireEvent.click(screen.getByRole("textbox", { name: "Note" }));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("requires irreversible confirmation before deleting a note", async () => {
    const confirmSpy = vi
      .spyOn(window, "confirm")
      .mockReturnValueOnce(false)
      .mockReturnValueOnce(true);
    const user = userEvent.setup();
    render(
      <AnnotationsPanel
        documentId="doc-1"
        filename="report.pdf"
        onClose={vi.fn()}
      />,
    );
    const remove = await screen.findByRole("button", { name: "Delete note" });

    await user.click(remove);
    expect(mocks.deleteLibraryAnnotation).not.toHaveBeenCalled();
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringMatching(/permanently delete "Check this claim".*can't be undone/i),
    );

    mocks.listLibraryAnnotations.mockResolvedValueOnce([]);
    await user.click(remove);
    await vi.waitFor(() =>
      expect(mocks.deleteLibraryAnnotation).toHaveBeenCalledWith(
        "doc-1",
        "note-1",
      ),
    );
  });

  it("labels create and edit fields independently of their placeholders", async () => {
    const user = userEvent.setup();
    render(
      <AnnotationsPanel
        documentId="doc-1"
        filename="report.pdf"
        onClose={vi.fn()}
      />,
    );

    expect(await screen.findByRole("textbox", { name: "Note" })).toHaveAttribute(
      "placeholder",
      "Add a note…",
    );
    expect(
      screen.getByRole("textbox", { name: "Note anchor (optional)" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Edit note" }));

    expect(
      screen.getByRole("textbox", { name: "Edit note" }),
    ).toHaveValue("Check this claim");
    expect(
      screen.getByRole("textbox", { name: "Edit note anchor (optional)" }),
    ).toHaveValue("p. 3");
  });
});
