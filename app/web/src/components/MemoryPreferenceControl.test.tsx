// @vitest-environment jsdom
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { MemoryPreference } from "@/lib/inspector";
import { MemoryPreferenceControl } from "./MemoryPreferenceControl";

const mocks = vi.hoisted(() => ({ get: vi.fn(), update: vi.fn() }));
vi.mock("@/lib/inspector", () => ({
  getMemoryPreference: mocks.get,
  updateMemoryPreference: mocks.update,
}));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

const on = { automaticMemoryEnabled: true, etag: '"memory-preference-0"' };
const off = { automaticMemoryEnabled: false, etag: '"memory-preference-1"' };

beforeEach(() => {
  mocks.get.mockResolvedValue(on);
  mocks.update.mockResolvedValue(off);
});
afterEach(() => { cleanup(); vi.resetAllMocks(); });

describe("MemoryPreferenceControl", () => {
  it("defaults on but cannot be changed until the server confirms it", async () => {
    const pending = deferred<MemoryPreference>();
    mocks.get.mockReturnValue(pending.promise);
    render(<MemoryPreferenceControl />);
    const control = screen.getByRole("switch", { name: "Automatic memory" });
    expect(control).toBeChecked();
    expect(control).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Loading");
    await act(async () => pending.resolve(on));
    expect(control).toBeEnabled();
    expect(control).toHaveAccessibleDescription(/not tool consent/);
    expect(mocks.update).not.toHaveBeenCalled();
  });

  it("loads explicit off and reenables using the server's ETag without consent", async () => {
    mocks.get.mockResolvedValue(off);
    mocks.update.mockResolvedValue({ ...on, etag: '"memory-preference-2"' });
    const user = userEvent.setup();
    render(<MemoryPreferenceControl />);
    const control = await screen.findByRole("switch");
    await waitFor(() => expect(control).toBeEnabled());
    expect(control).not.toBeChecked();
    await user.click(control);
    expect(mocks.update).toHaveBeenCalledExactlyOnceWith(true, off.etag);
    expect(control).toBeChecked();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("marks a pending disable, prevents duplicate writes, then confirms off", async () => {
    const pending = deferred<MemoryPreference>();
    mocks.update.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    render(<MemoryPreferenceControl />);
    const control = screen.getByRole("switch");
    await waitFor(() => expect(control).toBeEnabled());
    await user.click(control);
    expect(control).not.toBeChecked();
    expect(control).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent("Saving");
    await user.click(control);
    expect(mocks.update).toHaveBeenCalledExactlyOnceWith(false, on.etag);
    await act(async () => pending.resolve(off));
    expect(control).toBeEnabled();
    expect(screen.getByRole("status")).toHaveTextContent("Automatic memory is off.");
  });

  it("rolls back a failed write but treats the current setting as unknown until reloaded", async () => {
    mocks.update.mockRejectedValue(new Error("503: Unavailable"));
    const user = userEvent.setup();
    render(<MemoryPreferenceControl />);
    const control = screen.getByRole("switch");
    await waitFor(() => expect(control).toBeEnabled());
    await user.click(control);
    expect(control).toBeChecked();
    expect(control).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent("last confirmed");
    expect(screen.getByRole("status")).toHaveTextContent("unconfirmed");
    // The server may have committed a PATCH whose response was lost.
    mocks.get.mockResolvedValue(off);
    await user.click(screen.getByRole("button", { name: "Reload memory preference" }));
    expect(control).not.toBeChecked();
    expect(control).toBeEnabled();
  });

  it("does not describe a failed initial read as enabled", async () => {
    mocks.get.mockRejectedValue(new Error("Preference read failed"));
    render(<MemoryPreferenceControl />);
    expect(await screen.findByRole("alert")).toHaveTextContent("default has not been confirmed");
    expect(screen.getByRole("switch")).toBeDisabled();
    expect(screen.getByRole("status")).not.toHaveTextContent("Automatic memory is on.");
  });

  it("fences an older initial read after an effect is restarted", async () => {
    const old = deferred<MemoryPreference>();
    mocks.get.mockReturnValueOnce(old.promise).mockResolvedValue(off);
    render(<StrictMode><MemoryPreferenceControl /></StrictMode>);
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    await act(async () => old.resolve(on));
    expect(screen.getByRole("switch")).not.toBeChecked();
  });

  it("ignores a departed owner's late write instead of changing a new control", async () => {
    const pending = deferred<MemoryPreference>();
    mocks.update.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    const { rerender } = render(<MemoryPreferenceControl key="alice" />);
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    await user.click(screen.getByRole("switch"));
    rerender(<MemoryPreferenceControl key="bob" />);
    await waitFor(() => expect(screen.getByRole("switch")).toBeEnabled());
    await act(async () => pending.resolve(off));
    expect(screen.getByRole("switch")).toBeChecked();
  });
});
