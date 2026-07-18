// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ShareState } from "@/lib/library";
import SharePanel from "./SharePanel";

const mocks = vi.hoisted(() => ({
  getDocumentShares: vi.fn(),
  setDocumentShares: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  getDocumentShares: mocks.getDocumentShares,
  setDocumentShares: mocks.setDocumentShares,
}));

const SHARED_STATE: ShareState = {
  documentId: "doc1",
  visibility: "shared",
  grantees: ["a@example.com", "b@example.com"],
};

beforeEach(() => {
  mocks.getDocumentShares.mockReset();
  mocks.setDocumentShares.mockReset();
});

afterEach(() => {
  cleanup();
});

describe("SharePanel", () => {
  it("warns that switching away from shared and saving will drop current grantees", async () => {
    mocks.getDocumentShares.mockResolvedValue(SHARED_STATE);
    const user = userEvent.setup();
    render(<SharePanel documentId="doc1" filename="notes.pdf" onClose={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByRole("radio", { name: /^private/i })).toBeInTheDocument(),
    );

    // No warning while visibility is still "shared".
    expect(screen.queryByRole("status")).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /^private/i }));

    const warning = screen.getByRole("status");
    expect(warning.textContent).toMatch(/2\s+people currently shared with/i);
  });

  it("does not warn when there are no grantees to lose", async () => {
    mocks.getDocumentShares.mockResolvedValue({
      documentId: "doc2",
      visibility: "shared",
      grantees: [],
    } satisfies ShareState);
    const user = userEvent.setup();
    render(<SharePanel documentId="doc2" filename="empty.pdf" onClose={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByRole("radio", { name: /^private/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("radio", { name: /^private/i }));

    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("sends an empty grantee list to the server when saving as private, matching the warning", async () => {
    mocks.getDocumentShares.mockResolvedValue(SHARED_STATE);
    mocks.setDocumentShares.mockResolvedValue({
      documentId: "doc1",
      visibility: "private",
      grantees: [],
    } satisfies ShareState);
    const user = userEvent.setup();
    render(<SharePanel documentId="doc1" filename="notes.pdf" onClose={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByRole("radio", { name: /^private/i })).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("radio", { name: /^private/i }));
    await user.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(mocks.setDocumentShares).toHaveBeenCalledWith("doc1", "private", []),
    );
  });
});
