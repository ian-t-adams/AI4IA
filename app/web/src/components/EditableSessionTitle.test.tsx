// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { EditableSessionTitle } from "./EditableSessionTitle";

afterEach(cleanup);

describe("EditableSessionTitle", () => {
  it("supports F2, Enter save, and focus return", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <EditableSessionTitle title="Original" onSave={onSave} onOpen={vi.fn()} />,
    );
    const title = screen.getByRole("button", { name: "Original" });
    title.focus();
    await user.keyboard("{F2}");
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    expect(input).toHaveFocus();
    await user.clear(input);
    await user.type(input, "  Renamed conversation  {Enter}");
    expect(onSave).toHaveBeenCalledWith("Renamed conversation");
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Rename" })).toHaveFocus(),
    );
  });

  it("cancels with Escape and rejects an empty title without saving", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<EditableSessionTitle title="Original" onSave={onSave} />);

    await user.click(screen.getByRole("button", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    await user.clear(input);
    await user.keyboard("{Enter}");
    expect(onSave).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Conversation title cannot be empty.",
    );
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("textbox", { name: "Conversation title" })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("Rename cancelled");
  });

  it("keeps editing and surfaces a specific server error", async () => {
    const onSave = vi.fn().mockRejectedValue(new Error("Title already changed"));
    const user = userEvent.setup();
    render(<EditableSessionTitle title="Original" onSave={onSave} />);

    await user.click(screen.getByRole("button", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    await user.clear(input);
    await user.type(input, "New title{Enter}");
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Title already changed",
    );
    expect(input).toHaveFocus();
  });

  it("leaves focus on the clicked control after blur-save", async () => {
    const onSave = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(
      <>
        <EditableSessionTitle title="Original" onSave={onSave} />
        <button type="button">Next control</button>
      </>,
    );
    await user.click(screen.getByRole("button", { name: "Rename" }));
    const input = screen.getByRole("textbox", { name: "Conversation title" });
    await user.clear(input);
    await user.type(input, "Blurred title");
    await user.click(screen.getByRole("button", { name: "Next control" }));
    await waitFor(() => expect(onSave).toHaveBeenCalledWith("Blurred title"));
    expect(screen.getByRole("button", { name: "Next control" })).toHaveFocus();
  });
});
