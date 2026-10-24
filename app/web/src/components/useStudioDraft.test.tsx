// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useStudioDraft } from "./useStudioDraft";
import { useWorkspacePanels } from "./useWorkspacePanels";

function Workspace({ dirty = false, busy = false }) {
  const { view, navigate } = useWorkspacePanels();
  return <><p>{view}</p><button onClick={() => navigate("studio")}>Studio</button>
    <button onClick={() => navigate("chat")}>Chat</button>
    {view === "studio" && <Draft dirty={dirty} busy={busy} />}</>;
}
function Draft({ dirty, busy }: { dirty: boolean; busy: boolean }) {
  useStudioDraft(dirty, busy);
  return <input aria-label="Draft" defaultValue="Retained draft" />;
}
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("Studio navigation protection", () => {
  it("keeps a dirty editor on cancelled page navigation and permits confirmed navigation", () => {
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<Workspace dirty />);
    fireEvent.click(screen.getByText("Studio"));
    fireEvent.click(screen.getByText("Chat"));
    expect(screen.getByLabelText("Draft")).toHaveValue("Retained draft");
    expect(location.hash).toBe("#/studio");
    fireEvent.click(screen.getByText("Chat"));
    expect(screen.queryByLabelText("Draft")).toBeNull();
    expect(confirm).toHaveBeenCalledTimes(2);
  });
  it("restores the displayed page and URL when back/forward navigation is declined", () => {
    vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Workspace dirty />);
    fireEvent.click(screen.getByText("Studio"));
    act(() => {
      window.history.replaceState(null, "", "/#library");
      window.dispatchEvent(new PopStateEvent("popstate"));
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(screen.getByLabelText("Draft")).toHaveValue("Retained draft");
    expect(location.hash).toBe("#/studio");
    expect(window.confirm).toHaveBeenCalledTimes(1);
  });
  it("blocks unload for edits and in-flight saves, not clean editors", () => {
    const { rerender } = render(<Draft dirty busy={false} />);
    const unload = () => {
      const event = new Event("beforeunload", { cancelable: true });
      window.dispatchEvent(event);
      return event.defaultPrevented;
    };
    expect(unload()).toBe(true);
    rerender(<Draft dirty={false} busy />);
    expect(unload()).toBe(true);
    rerender(<Draft dirty={false} busy={false} />);
    expect(unload()).toBe(false);
  });
  it("allows clean navigation without prompting and refuses navigation during a save", () => {
    const confirm = vi.spyOn(window, "confirm");
    const alert = vi.spyOn(window, "alert").mockImplementation(() => {});
    const { rerender } = render(<Workspace />);
    fireEvent.click(screen.getByText("Studio"));
    fireEvent.click(screen.getByText("Chat"));
    expect(screen.queryByLabelText("Draft")).toBeNull();
    expect(confirm).not.toHaveBeenCalled();
    rerender(<Workspace busy />);
    fireEvent.click(screen.getByText("Studio"));
    fireEvent.click(screen.getByText("Chat"));
    expect(screen.getByLabelText("Draft")).toBeInTheDocument();
    expect(alert).toHaveBeenCalledOnce();
  });
});
