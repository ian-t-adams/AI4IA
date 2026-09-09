// @vitest-environment jsdom
import { useState } from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "@/lib/auth";
import { INITIALIZING_SESSION, PENDING_DELETION, VERIFIED_DELETION } from "@/lib/deletionTestFixtures";
import type { InitializationPage, SessionInitialization } from "@/lib/types";
import { ConversationDeletionPanel } from "./ConversationDeletionPanel";
import { MemoryPreferenceProvider, type MemoryPreferenceOwner } from "./MemoryPreferenceProvider";

vi.mock("@/lib/auth", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
const initializationRead = vi.fn<() => Promise<Response>>();
const deletionRead = vi.fn<() => Promise<Response>>();
const discardRequest = vi.fn<() => Promise<Response>>();
const resumeRequest = vi.fn<() => Promise<Response>>();
const accepted = { ...PENDING_DELETION, sessionId: INITIALIZING_SESSION.sessionId };
const verified = { ...VERIFIED_DELETION, sessionId: INITIALIZING_SESSION.sessionId };
const discardLabel = `Discard incomplete creation ${INITIALIZING_SESSION.sessionId}`;

function page<T>(items: T[], nextCursor: string | null = null) {
  return { items, hasMore: nextCursor !== null, nextCursor };
}
function initializationPage<T extends SessionInitialization>(items: T[], nextCursor: string | null = null): InitializationPage {
  return { ...page(items, nextCursor), observation: "not_completion_evidence" };
}
function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}
function renderPanel(owner?: MemoryPreferenceOwner) {
  return render(<MemoryPreferenceProvider owner={owner}><ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} /></MemoryPreferenceProvider>);
}
function Harness() {
  const [open, setOpen] = useState(false);
  return (
    <MemoryPreferenceProvider>
      <button type="button" onClick={() => setOpen(true)}>Open recovery</button>
      <ConversationDeletionPanel open={open} onShowAll={vi.fn()} onClose={() => setOpen(false)} />
    </MemoryPreferenceProvider>
  );
}

beforeEach(() => {
  vi.spyOn(window, "confirm").mockReturnValue(true);
  initializationRead.mockImplementation(async () => json(initializationPage([INITIALIZING_SESSION])));
  deletionRead.mockImplementation(async () => json(page([])));
  discardRequest.mockImplementation(async () => json(accepted, 202));
  resumeRequest.mockImplementation(async () => json(verified));
  fetchMock.mockImplementation(async (input, init) => {
    const path = String(input);
    if (path === "/api/sessions/deletions" && !init?.method) return deletionRead();
    if (path.startsWith("/api/sessions/initializations") && !init?.method) return initializationRead();
    if (path === "/api/sessions/reserved%2Fcreation" && init?.method === "DELETE") return discardRequest();
    if (path === "/api/sessions/reserved%2Fcreation/deletion/reconcile" && init?.method === "POST") return resumeRequest();
    throw new Error(`Unexpected recovery request: ${init?.method ?? "GET"} ${path}`);
  });
});
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("incomplete conversation creation recovery", () => {
  it("keeps reservations under disclosure and only reads on open or refresh, never inventing chat content", async () => {
    initializationRead.mockImplementation(async () => json(initializationPage([{
      ...INITIALIZING_SESSION, title: "PRIVATE TITLE", instructions: "PRIVATE INSTRUCTIONS", transcript: "PRIVATE TRANSCRIPT",
    }])));
    const user = userEvent.setup();
    renderPanel();
    expect(initializationRead).not.toHaveBeenCalled();
    expect(screen.queryByText(/No incomplete creations reported/)).not.toBeInTheDocument();
    await user.click(screen.getByText("Incomplete conversation creation"));
    const heading = await screen.findByRole("heading", { name: "Creation reserved/creation" });
    expect(heading.closest("li")?.querySelector("time")).toHaveAttribute("datetime", INITIALIZING_SESSION.createdAt);
    expect(screen.getByText(/These reservations may still be in progress/)).toHaveTextContent("not usable chats");
    expect(screen.queryByText(/PRIVATE TITLE|PRIVATE INSTRUCTIONS|PRIVATE TRANSCRIPT/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh incomplete creations" }));
    await screen.findByRole("heading", { name: "Creation reserved/creation" });
    expect(initializationRead).toHaveBeenCalledTimes(2);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/initializations", { cache: "no-store" });
    expect(fetchMock.mock.calls.every((call) => call[1]?.method === undefined && call[1]?.cache === "no-store")).toBe(true);
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    vi.useRealTimers();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(window.confirm).not.toHaveBeenCalled();
    expect(discardRequest).not.toHaveBeenCalled();
    expect(resumeRequest).not.toHaveBeenCalled();
  });

  it("requires confirmation, deletes only that exact id even if publication won, and explicitly resumes the returned job", async () => {
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    const discard = await screen.findByRole("button", { name: discardLabel });
    vi.mocked(window.confirm).mockReturnValue(false);
    await user.click(discard);
    expect(discardRequest).not.toHaveBeenCalled();
    expect(discard).toBeInTheDocument();
    vi.mocked(window.confirm).mockReturnValue(true);
    await user.click(discard);
    expect(window.confirm).toHaveBeenLastCalledWith(expect.stringMatching(/cancels this creation and prevents it from publishing.*already finished.*deletes that same conversation/));
    await screen.findByRole("heading", { name: "Conversation reserved/creation" });
    expect(screen.queryByRole("heading", { name: "Creation reserved/creation" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh incomplete creations" })).toHaveFocus();
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/reserved%2Fcreation", { method: "DELETE" });
    expect(discardRequest).toHaveBeenCalledTimes(1);
    expect(resumeRequest).not.toHaveBeenCalled();
    expect(screen.getByText(/Removed from chats. Cleanup pending/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Resume cleanup for reserved/creation" }));
    await screen.findByText(/Cleanup last verified:/);
    expect(resumeRequest).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? "GET")).toEqual(["GET", "GET", "DELETE", "POST"]);
  });

  it("distinguishes initialization loading, server failure, and an empty page", async () => {
    const read = deferred<Response>();
    initializationRead.mockReturnValueOnce(read.promise).mockImplementation(async () => json(initializationPage([])));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    expect(await screen.findByText("Loading incomplete conversation creations...")).toBeInTheDocument();
    expect(screen.queryByText(/No incomplete creations reported/)).not.toBeInTheDocument();
    await act(async () => read.resolve(json({ detail: "Initialization listing unavailable" }, 503)));
    expect(screen.getByRole("alert")).toHaveTextContent("Initialization listing unavailable");
    expect(screen.queryByText(/No incomplete creations reported/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh incomplete creations" }));
    const empty = await screen.findByText(/No incomplete creations reported/);
    expect(empty).toHaveTextContent("not completion evidence");
    expect(empty).toHaveTextContent("refresh later or ask an operator to investigate");
    expect(screen.queryByText(/all creations complete/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(discardRequest).not.toHaveBeenCalled();
  });

  it.each([
    { label: "missing observation", value: page([]) },
    { label: "wrong items shape", value: { ...initializationPage([]), items: {} } },
    { label: "invalid reservation", value: { ...initializationPage([]), items: [{ ...INITIALIZING_SESSION, state: "active" }] } },
  ])("shows malformed $label as an error, not an empty observation", async ({ value }) => {
    initializationRead.mockResolvedValueOnce(json(value)).mockResolvedValueOnce(json(initializationPage([])));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    expect(await screen.findByRole("alert")).toHaveTextContent("malformed incomplete-creation response");
    expect(screen.queryByText(/No incomplete creations reported/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: discardLabel })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh incomplete creations" }));
    expect(await screen.findByText(/No incomplete creations reported/)).toHaveTextContent("not completion evidence");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(discardRequest).not.toHaveBeenCalled();
    expect(resumeRequest).not.toHaveBeenCalled();
  });

  it("retains last-observed reservations when a later response is malformed", async () => {
    initializationRead.mockResolvedValueOnce(json(initializationPage([INITIALIZING_SESSION])))
      .mockResolvedValueOnce(json({ ...initializationPage([]), observation: "completion_evidence" }));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    await screen.findByRole("heading", { name: "Creation reserved/creation" });
    await user.click(screen.getByRole("button", { name: "Refresh incomplete creations" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("malformed incomplete-creation response");
    expect(screen.getByRole("heading", { name: "Creation reserved/creation" })).toBeInTheDocument();
    expect(screen.queryByText(/No incomplete creations reported/)).not.toBeInTheDocument();
    expect(discardRequest).not.toHaveBeenCalled();
  });

  it.each([404, 409, 503, 204])("does not remove a reservation or invent a job after HTTP %s lacks accepted status", async (status) => {
    discardRequest.mockImplementation(async () => status === 204
      ? new Response(null, { status }) : json({ detail: "Creation could not be discarded" }, status));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    await user.click(await screen.findByRole("button", { name: discardLabel }));
    expect(await screen.findByRole("alert")).toHaveTextContent(status === 204 ? "did not confirm deletion status" : "Creation could not be discarded");
    expect(screen.getByRole("heading", { name: "Creation reserved/creation" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: discardLabel })).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "Conversation reserved/creation" })).not.toBeInTheDocument();
    expect(discardRequest).toHaveBeenCalledTimes(1);
    expect(resumeRequest).not.toHaveBeenCalled();
  });

  it("blocks duplicate discard submissions and waits for acceptance before removing the reservation", async () => {
    const request = deferred<Response>();
    discardRequest.mockReturnValue(request.promise);
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    const discard = await screen.findByRole("button", { name: discardLabel });
    fireEvent.click(discard);
    fireEvent.click(discard);
    expect(discard).toBeDisabled();
    expect(discardRequest).toHaveBeenCalledTimes(1);
    expect(window.confirm).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("heading", { name: "Creation reserved/creation" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Conversation reserved/creation" })).not.toBeInTheDocument();
    await act(async () => request.resolve(json(accepted, 202)));
    expect(screen.queryByRole("heading", { name: "Creation reserved/creation" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Conversation reserved/creation" })).toBeInTheDocument();
  });

  it("reads bounded pages through the opaque cursor without automatically following it", async () => {
    const first = Array.from({ length: 50 }, (_, index) => ({ ...INITIALIZING_SESSION, sessionId: `reservation-${index}` }));
    initializationRead.mockResolvedValueOnce(json(initializationPage(first, "opaque/%2F+=?")))
      .mockResolvedValueOnce(json(initializationPage([{ ...INITIALIZING_SESSION, sessionId: "last" }])));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    const list = await screen.findByRole("list", { name: "Incomplete conversation creations" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(50);
    expect(initializationRead).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Load more incomplete creations" }));
    await screen.findByRole("heading", { name: "Creation last" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(51);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/initializations?cursor=opaque%2F%252F%2B%3D%3F", { cache: "no-store" });
    expect(screen.queryByRole("button", { name: "Load more incomplete creations" })).not.toBeInTheDocument();
    expect(discardRequest).not.toHaveBeenCalled();
  });

  it.each([false, true])("keeps a discarded reservation removed after a late list response only when DELETE was accepted (%s)", async (success) => {
    const oldRead = deferred<Response>();
    initializationRead.mockResolvedValueOnce(json(initializationPage([INITIALIZING_SESSION]))).mockReturnValueOnce(oldRead.promise);
    if (!success) discardRequest.mockResolvedValueOnce(json({ detail: "Creation not found" }, 404));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    await screen.findByRole("button", { name: discardLabel });
    await user.click(screen.getByRole("button", { name: "Refresh incomplete creations" }));
    await user.click(screen.getByRole("button", { name: discardLabel }));
    if (success) await screen.findByRole("heading", { name: "Conversation reserved/creation" });
    else await screen.findByRole("alert");
    await act(async () => oldRead.resolve(json(initializationPage([INITIALIZING_SESSION]))));
    if (success) {
      expect(screen.queryByRole("heading", { name: "Creation reserved/creation" })).not.toBeInTheDocument();
      expect(screen.getByRole("heading", { name: "Conversation reserved/creation" })).toBeInTheDocument();
    } else {
      expect(screen.getByRole("heading", { name: "Creation reserved/creation" })).toBeInTheDocument();
      expect(screen.queryByRole("heading", { name: "Conversation reserved/creation" })).not.toBeInTheDocument();
    }
    expect(initializationRead).toHaveBeenCalledTimes(2);
    expect(resumeRequest).not.toHaveBeenCalled();
  });

  it.each([false, true])("retains a new discard job over an older job page, but accepts a subsequent fresh status (old row=%s)", async (hasOldRow) => {
    const oldJobs = deferred<Response>();
    deletionRead.mockReturnValueOnce(oldJobs.promise).mockResolvedValueOnce(json(page([verified])));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    await user.click(await screen.findByRole("button", { name: discardLabel }));
    await screen.findByRole("heading", { name: "Conversation reserved/creation" });
    await act(async () => oldJobs.resolve(json(page(hasOldRow ? [{ ...accepted, state: "retryable", updatedAt: "2026-09-09T12:00:00Z" }] : []))));
    expect(screen.getByText(/Removed from chats. Cleanup pending/)).toBeInTheDocument();
    expect(screen.queryByText(/Removed from chats. Retry needed/)).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Creation reserved/creation" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText(/Cleanup last verified:/);
    expect(within(screen.getByRole("list", { name: "Deletion requests" })).getAllByRole("listitem")).toHaveLength(1);
    expect(screen.queryByRole("heading", { name: "Creation reserved/creation" })).not.toBeInTheDocument();
    expect(resumeRequest).not.toHaveBeenCalled();
  });

  it("ignores an older initialization read after a newer refresh", async () => {
    const old = deferred<Response>();
    initializationRead.mockReturnValueOnce(old.promise).mockResolvedValueOnce(json(initializationPage([])));
    const user = userEvent.setup();
    renderPanel();
    await user.click(screen.getByText("Incomplete conversation creation"));
    await user.click(await screen.findByRole("button", { name: "Refresh incomplete creations" }));
    await screen.findByText(/No incomplete creations reported/);
    await act(async () => old.resolve(json(initializationPage([INITIALIZING_SESSION]))));
    expect(screen.queryByRole("heading", { name: "Creation reserved/creation" })).not.toBeInTheDocument();
    expect(screen.getByText(/No incomplete creations reported/)).toBeInTheDocument();
    expect(discardRequest).not.toHaveBeenCalled();
  });

  it("keeps a discard in flight across close/reopen without applying the former view's result", async () => {
    const request = deferred<Response>();
    discardRequest.mockReturnValue(request.promise);
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open recovery" }));
    await user.click(screen.getByText("Incomplete conversation creation"));
    await user.click(await screen.findByRole("button", { name: discardLabel }));
    await user.click(screen.getByRole("button", { name: "Close deletion status" }));
    await user.click(screen.getByRole("button", { name: "Open recovery" }));
    await user.click(screen.getByText("Incomplete conversation creation"));
    const reopened = await screen.findByRole("button", { name: discardLabel });
    expect(reopened).toBeDisabled();
    await user.click(reopened);
    expect(discardRequest).toHaveBeenCalledTimes(1);
    await act(async () => request.resolve(json(accepted, 202)));
    expect(reopened).toBeEnabled();
    expect(screen.queryByRole("heading", { name: "Conversation reserved/creation" })).not.toBeInTheDocument();
    expect(initializationRead).toHaveBeenCalledTimes(2);
    expect(deletionRead).toHaveBeenCalledTimes(2);
    initializationRead.mockResolvedValueOnce(json(initializationPage([])));
    deletionRead.mockResolvedValueOnce(json(page([accepted])));
    await user.click(screen.getByRole("button", { name: "Refresh incomplete creations" }));
    await screen.findByText(/No incomplete creations reported/);
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByRole("heading", { name: "Conversation reserved/creation" });
    expect(discardRequest).toHaveBeenCalledTimes(1);
    expect(resumeRequest).not.toHaveBeenCalled();
  });

  it("ignores a former owner's discard and permits the new owner's explicit action", async () => {
    let currentOwner = "alice";
    const subscribers = new Set<() => void>();
    const owner: MemoryPreferenceOwner = {
      getSnapshot: () => currentOwner,
      subscribe: (notify) => { subscribers.add(notify); return () => { subscribers.delete(notify); }; },
    };
    const old = deferred<Response>();
    discardRequest.mockReturnValueOnce(old.promise);
    const user = userEvent.setup();
    renderPanel(owner);
    await user.click(screen.getByText("Incomplete conversation creation"));
    await user.click(await screen.findByRole("button", { name: discardLabel }));
    act(() => { currentOwner = "bob"; for (const notify of subscribers) notify(); });
    await user.click(screen.getByText("Incomplete conversation creation"));
    const bob = await screen.findByRole("button", { name: discardLabel });
    expect(bob).toBeEnabled();
    await act(async () => old.resolve(json(accepted, 202)));
    expect(bob).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Conversation reserved/creation" })).not.toBeInTheDocument();
    await user.click(bob);
    await screen.findByRole("heading", { name: "Conversation reserved/creation" });
    expect(discardRequest).toHaveBeenCalledTimes(2);
  });
});
