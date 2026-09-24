// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { ImageOptionsResponse, Message } from "@/lib/types";
import { ImageEditDialog } from "./ImageEditDialog";

const mocks = vi.hoisted(() => ({
  fetchImageArtifact: vi.fn(),
  fetchLibraryImageSource: vi.fn(),
  editImage: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...mocks,
    ApiError: actual.ApiError,
    apiErrorDetail: actual.apiErrorDetail,
  };
});

const OPTIONS: ImageOptionsResponse = {
  enabled: true,
  maxSelectedModels: 3,
  currency: "USD",
  priceVersion: "test",
  editingEnabled: true,
  defaultEditModel: "gpt-image-2.5-sunburst",
  models: [
    {
      id: "gpt-image-2", displayName: "gpt-image-2", provider: "openai",
      sizes: ["1024x1024", "1536x1024", "auto"], qualities: ["auto", "low", "high"],
      dataZones: [], residencies: ["global"], prices: [], editing: true,
    },
    {
      id: "gpt-image-2.5-sunburst", displayName: "gpt-image-2.5-sunburst", provider: "openai",
      sizes: ["1024x1024", "auto"], qualities: ["auto", "medium"],
      dataZones: [], residencies: ["global"], prices: [], editing: true,
    },
    {
      id: "FLUX.2-pro", displayName: "FLUX.2-pro", provider: "black_forest_labs",
      sizes: ["1024x1024"], qualities: ["auto"], dataZones: [], residencies: ["global"],
      prices: [], editing: false,
    },
  ],
};

const EDITED: Message[] = [
  { id: "u", sessionId: "s1", userId: "me", role: "user", content: "Edit image: add a moon" } as Message,
  { id: "a", sessionId: "s1", userId: "me", role: "assistant", content: "Edited." } as Message,
];

beforeEach(() => {
  mocks.fetchImageArtifact.mockResolvedValue(new Blob(["png"], { type: "image/png" }));
  mocks.fetchLibraryImageSource.mockResolvedValue(new Blob(["png"], { type: "image/png" }));
  mocks.editImage.mockResolvedValue({ messages: EDITED });
  vi.stubGlobal("URL", Object.assign(URL, {
    createObjectURL: vi.fn(() => "blob:preview"),
    revokeObjectURL: vi.fn(),
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderDialog(overrides: Partial<Parameters<typeof ImageEditDialog>[0]> = {}) {
  const onClose = vi.fn();
  const onEdited = vi.fn();
  render(
    <ImageEditDialog
      sessionId="s1"
      source={{ kind: "generated", id: "a".repeat(32) }}
      label="A lighthouse"
      options={OPTIONS}
      onClose={onClose}
      onEdited={onEdited}
      {...overrides}
    />,
  );
  return { onClose, onEdited };
}

// The modal moves focus inside on the next animation frame. Wait for it, so a
// test's own typing is not redirected by that deferred focus.
async function settled() {
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Close image editor" })).toHaveFocus(),
  );
}

describe("ImageEditDialog", () => {
  it("offers only editing models, preselects the server default and shows the source", async () => {
    renderDialog();
    expect(screen.getByRole("dialog", { name: "Edit image" })).toBeInTheDocument();
    const model = screen.getByRole("combobox", { name: "Model" });
    const labels = Array.from((model as HTMLSelectElement).options).map((o) => o.textContent);
    expect(labels).toEqual(["gpt-image-2", "gpt-image-2.5-sunburst (recommended for editing)"]);
    expect(model).toHaveValue("gpt-image-2.5-sunburst");
    expect(await screen.findByRole("img", { name: "Source image: A lighthouse" })).toHaveAttribute(
      "src", "blob:preview",
    );
    expect(mocks.fetchImageArtifact).toHaveBeenCalledWith("a".repeat(32));
    expect(mocks.fetchLibraryImageSource).not.toHaveBeenCalled();
  });

  it("requires a description before it can submit", async () => {
    const user = userEvent.setup();
    renderDialog();
    await settled();
    const submit = screen.getByRole("button", { name: "Edit image" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Describe the change" }), "   ");
    expect(submit).toBeDisabled();
    await user.type(screen.getByRole("textbox", { name: "Describe the change" }), "add a moon");
    expect(submit).toBeEnabled();
  });

  it("submits a whole-image edit and hands the persisted messages back", async () => {
    const user = userEvent.setup();
    const { onClose, onEdited } = renderDialog();
    await settled();
    await user.type(screen.getByRole("textbox", { name: "Describe the change" }), " add a moon ");
    await user.click(screen.getByRole("button", { name: "Edit image" }));
    await waitFor(() => expect(onEdited).toHaveBeenCalledWith("s1", EDITED));
    expect(mocks.editImage).toHaveBeenCalledTimes(1);
    expect(mocks.editImage).toHaveBeenCalledWith({
      sessionId: "s1",
      source: { kind: "generated", id: "a".repeat(32) },
      prompt: "add a moon",
      model: "gpt-image-2.5-sunburst",
      size: "auto",
      quality: "auto",
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("sends a region only when the user selects one, with keyboard-operable edges", async () => {
    const user = userEvent.setup();
    renderDialog();
    await settled();
    expect(screen.queryByRole("slider", { name: /Left edge/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "Selected region" }));
    const left = screen.getByRole("slider", { name: /Left edge/ });
    const width = screen.getByRole("slider", { name: /Width/ });
    fireEvent.change(left, { target: { value: "10" } });
    fireEvent.change(width, { target: { value: "95" } });
    // Width is clamped so the region stays inside the image.
    expect(width).toHaveValue("90");
    await user.type(screen.getByRole("textbox", { name: "Describe the change" }), "add a moon");
    await user.click(screen.getByRole("button", { name: "Edit image" }));
    await waitFor(() => expect(mocks.editImage).toHaveBeenCalledTimes(1));
    expect(mocks.editImage.mock.calls[0][0].region).toEqual({
      x: 0.1, y: 0.25, width: 0.9, height: 0.5,
    });
  });

  it("lets a pointer drag set the region over the preview", async () => {
    const user = userEvent.setup();
    renderDialog();
    await settled();
    await user.click(screen.getByRole("radio", { name: "Selected region" }));
    const image = await screen.findByRole("img", { name: "Source image: A lighthouse" });
    const stage = image.parentElement as HTMLDivElement;
    stage.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 200, height: 100, right: 200, bottom: 100, x: 0, y: 0 }) as DOMRect;
    fireEvent.pointerDown(stage, { clientX: 20, clientY: 10, pointerId: 1 });
    fireEvent.pointerMove(stage, { clientX: 120, clientY: 60, pointerId: 1 });
    fireEvent.pointerUp(stage, { clientX: 120, clientY: 60, pointerId: 1 });
    const overlay = screen.getByTestId("image-edit-region");
    expect(overlay.style.left).toBe("10%");
    expect(overlay.style.top).toBe("10%");
    expect(overlay.style.width).toBe("50%");
    expect(overlay.style.height).toBe("50%");
  });

  it("keeps the dialog open and announces a refusal", async () => {
    const user = userEvent.setup();
    mocks.editImage.mockRejectedValueOnce(new Error("The edit was blocked by the content safety system."));
    const { onClose, onEdited } = renderDialog();
    await settled();
    await user.type(screen.getByRole("textbox", { name: "Describe the change" }), "add a moon");
    await user.click(screen.getByRole("button", { name: "Edit image" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The edit was blocked by the content safety system.",
    );
    expect(onEdited).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Edit image" })).toBeEnabled();
  });

  it("closes on Escape and from Cancel", async () => {
    const user = userEvent.setup();
    const { onClose } = renderDialog();
    await settled();
    await user.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });

  it("previews a library source through the owner-only library endpoint", async () => {
    renderDialog({ source: { kind: "library", id: "doc-1" }, label: "photo.jpg" });
    expect(await screen.findByRole("img", { name: "Source image: photo.jpg" })).toBeInTheDocument();
    expect(mocks.fetchLibraryImageSource).toHaveBeenCalledWith("doc-1");
    expect(mocks.fetchImageArtifact).not.toHaveBeenCalled();
  });

  it("adapts size and quality to the chosen model", async () => {
    const user = userEvent.setup();
    renderDialog();
    await settled();
    await user.selectOptions(screen.getByRole("combobox", { name: "Output size" }), "1024x1024");
    await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "gpt-image-2");
    expect(screen.getByRole("combobox", { name: "Output size" })).toHaveValue("1024x1024");
    await user.selectOptions(screen.getByRole("combobox", { name: "Quality" }), "high");
    await user.selectOptions(screen.getByRole("combobox", { name: "Model" }), "gpt-image-2.5-sunburst");
    expect(screen.getByRole("combobox", { name: "Quality" })).toHaveValue("auto");
  });
});
