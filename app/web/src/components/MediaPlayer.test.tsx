// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fetchLibraryMedia, fetchLibraryTimeline } from "@/lib/api";
import type { LibraryDocument } from "@/lib/library";
import { MediaPlayer } from "./MediaPlayer";

vi.mock("@/lib/api", () => ({
  fetchLibraryMedia: vi.fn(),
  fetchLibraryTimeline: vi.fn(),
}));

const media = vi.mocked(fetchLibraryMedia);
const timeline = vi.mocked(fetchLibraryTimeline);

function doc(id: string): LibraryDocument {
  return {
    id,
    filename: `${id}.mp3`,
    contentType: "audio/mpeg",
    size: 10,
    modality: "audio",
    status: "ready",
    analyzerId: null,
    summary: "",
    chunkCount: 0,
    visibility: "private",
    createdAt: "2026-08-13T00:00:00Z",
    updatedAt: "2026-08-13T00:00:00Z",
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

beforeEach(() => {
  timeline.mockResolvedValue({
    documentId: "doc",
    modality: "audio",
    durationMs: null,
    segments: [],
  });
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    value: vi.fn(() => "blob:current"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    value: vi.fn(),
  });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("MediaPlayer", () => {
  it.each(["audio", "video"])(
    "keeps native %s controls keyboard-reachable when there are no scene markers",
    async (modality) => {
      media.mockResolvedValue(new Blob(["media"]));
      const { container } = render(
        <MediaPlayer doc={{ ...doc("first"), modality }} onClose={vi.fn()} />,
      );
      const close = screen.getByRole("button", { name: "Close player" });
      await waitFor(() => expect(container.querySelector(modality)).toBeInTheDocument());
      await waitFor(() => expect(close).toHaveFocus());
      const player = container.querySelector(modality);

      await userEvent.tab();
      expect(player).toHaveFocus();
      await userEvent.tab();
      expect(close).toHaveFocus();
      await userEvent.tab({ shift: true });
      expect(player).toHaveFocus();
      await userEvent.tab({ shift: true });
      expect(close).toHaveFocus();
    },
  );

  it("closes exactly once on Escape through the shared dialog handler", () => {
    media.mockReturnValue(new Promise<Blob>(() => {}));
    const onClose = vi.fn();
    const { unmount } = render(<MediaPlayer doc={doc("first")} onClose={onClose} />);
    const close = screen.getByRole("button", { name: "Close player" });
    close.focus();

    fireEvent.keyDown(close, { key: "ArrowRight" });
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.keyDown(close, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    fireEvent.click(close);
    expect(onClose).toHaveBeenCalledTimes(2);

    unmount();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("ignores an old document fetch after the selected document changes", async () => {
    const first = deferred<Blob>();
    const second = deferred<Blob>();
    media.mockImplementation((id) =>
      id === "first" ? first.promise : second.promise,
    );
    const { rerender, unmount } = render(
      <MediaPlayer doc={doc("first")} onClose={vi.fn()} />,
    );

    rerender(<MediaPlayer doc={doc("second")} onClose={vi.fn()} />);
    await act(async () => first.resolve(new Blob(["old"])));
    expect(URL.createObjectURL).not.toHaveBeenCalled();

    const current = new Blob(["new"]);
    await act(async () => second.resolve(current));
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalledWith(current));
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);

    unmount();
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:current");
  });
});
