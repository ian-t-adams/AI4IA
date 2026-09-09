// @vitest-environment jsdom
import { StrictMode, useState, type ReactElement } from "react";
import { act, cleanup, fireEvent, render as rtlRender, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch } from "@/lib/auth";
import { PENDING_DELETION, VERIFIED_DELETION } from "@/lib/deletionTestFixtures";
import type { DeletionPage, DeletionStatus } from "@/lib/types";
import { ConversationDeletionPanel } from "./ConversationDeletionPanel";
import { MemoryPreferenceProvider, type MemoryPreferenceOwner } from "./MemoryPreferenceProvider";

vi.mock("@/lib/auth", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
const writes = () => fetchMock.mock.calls.filter((call) => call[1]?.method === "POST");
const page = (items: DeletionStatus[], nextCursor: string | null = null): DeletionPage => ({ items, hasMore: nextCursor !== null, nextCursor });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function render(ui: ReactElement) {
  return rtlRender(ui, { wrapper: MemoryPreferenceProvider });
}

function Harness({ initialSessionId = null }: { initialSessionId?: string | null }) {
  const [open, setOpen] = useState(false);
  const [sessionId, setSessionId] = useState(initialSessionId);
  return (
    <>
      <div inert={open ? true : undefined}>
        <button type="button" onClick={() => setOpen(true)}>Open deletion status</button>
        <p>Unrelated active conversation</p>
      </div>
      <ConversationDeletionPanel open={open} sessionId={sessionId} onShowAll={() => setSessionId(null)} onClose={() => setOpen(false)} />
    </>
  );
}

beforeEach(() => {
  fetchMock.mockImplementation(async () => json(page([PENDING_DELETION])));
});
afterEach(() => { cleanup(); vi.resetAllMocks(); vi.useRealTimers(); });

describe("owner-resumed conversation deletion", () => {
  it.each(["pending", "retryable"] as const)("only reads on open/refresh and sends one pass per explicit Resume (%s)", async (state) => {
    let current: DeletionStatus = { ...PENDING_DELETION, state };
    let passes = 0;
    fetchMock.mockImplementation(async (_input, init) => {
      if (init?.method === "POST") {
        passes += 1;
        current = passes === 1 ? { ...current, attempts: 2 } : VERIFIED_DELETION;
        return json(current, passes === 1 ? 202 : 200);
      }
      return json(page([current]));
    });
    const user = userEvent.setup();
    render(<Harness />);
    expect(fetchMock).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Open deletion status" }));
    const resume = await screen.findByRole("button", { name: "Resume cleanup for removed/session" });
    expect(screen.getByText(/There is no automatic cleanup/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await waitFor(() => expect(resume).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(writes()).toHaveLength(0);
    for (const call of fetchMock.mock.calls) expect(call[1]).toEqual({ cache: "no-store" });
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    vi.useRealTimers();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    await user.click(resume);
    expect(await screen.findByText(/Cleanup pass finished/)).toBeInTheDocument();
    expect(writes()).toEqual([["/api/sessions/removed%2Fsession/deletion/reconcile", { method: "POST" }]]);
    expect(resume).toBeEnabled();
    expect(screen.queryByText(/Cleanup last verified:/)).not.toBeInTheDocument();
    vi.useFakeTimers();
    await act(async () => { await vi.advanceTimersByTimeAsync(120_000); });
    vi.useRealTimers();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await user.click(resume);
    expect(await screen.findByText(/Cleanup last verified:/)).toBeInTheDocument();
    expect(writes()).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(screen.queryByRole("button", { name: /Resume cleanup for/ })).not.toBeInTheDocument();
  });

  it("distinguishes loading, server failure, and a confirmed empty listing", async () => {
    const pending = deferred<Response>();
    fetchMock.mockReturnValueOnce(pending.promise).mockImplementation(async () => json(page([])));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading deletion status");
    expect(screen.queryByText(/No resumable deletion/)).not.toBeInTheDocument();
    await act(async () => pending.resolve(json({ detail: "Deletion status storage is unavailable." }, 503)));
    expect(screen.getByRole("alert")).toHaveTextContent("Deletion status storage is unavailable.");
    expect(screen.queryByText(/No resumable deletion/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(await screen.findByText(/No resumable deletion requests found/)).toHaveTextContent("Older deletions may not have retained status");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it("preserves server rejection copy without claiming that cleanup failed to remove the chat", async () => {
    fetchMock.mockImplementation(async (_input, init) => init?.method === "POST"
      ? json({ detail: "Conversation cleanup is disabled by the operator." }, 409)
      : json(page([{ ...PENDING_DELETION, state: "retryable", retryReason: "storage_unavailable" }])));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: /Resume cleanup for/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Conversation cleanup is disabled by the operator.");
    expect(screen.getByRole("alert")).toHaveTextContent("Refresh status");
    expect(screen.getByText(/Removed from chats. Retry needed/)).toBeInTheDocument();
    expect(screen.queryByText(/Cleanup pass finished/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(writes()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Resume cleanup for/ })).toBeEnabled());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(writes()).toHaveLength(1);
  });

  it.each([
    ["storage_unavailable", /Storage is unavailable/],
    ["cleanup_timeout", /reached its time limit/],
    ["concurrent_change", /Data changed during the last pass/],
    ["integrity_mismatch", /inconsistent deletion records/],
    ["uploads_unresolved", /uploads have no confirmed outcome/],
    ["artifact_store_required", /original-file storage is not configured/],
  ] as const)("explains %s without replacing it with a success state", async (retryReason, expected) => {
    fetchMock.mockImplementation(async () => json(page([{ ...PENDING_DELETION, state: "retryable", retryReason }])));
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    expect(await screen.findByText(expected)).toBeInTheDocument();
    expect(screen.getByText(/Removed from chats. Retry needed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Resume cleanup for/ })).toBeEnabled();
    expect(writes()).toHaveLength(0);
  });

  it("keeps unknown uploads unresolved and discloses identifiers, never content or a force-complete action", async () => {
    const unknown: DeletionStatus = {
      ...PENDING_DELETION, state: "retryable", phase: "uploads", retryReason: "uploads_unresolved",
      pendingUploads: Array.from({ length: 25 }, (_, index) => ({ id: `upload-${index}`, documentId: `document-${index}`, startedAt: "2000-01-01T00:00:00Z", content: "PRIVATE UPLOAD CONTENT" })),
      pendingUploadsTruncated: true,
    };
    fetchMock.mockImplementation(async (_input, init) => json(init?.method === "POST" ? unknown : page([unknown]), init?.method === "POST" ? 202 : 200));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    const summary = await screen.findByText("Unresolved uploads (25+)");
    expect(summary.closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText("upload-0")).not.toBeVisible();
    await user.click(summary);
    expect(screen.getByText("upload-24")).toBeVisible();
    expect(screen.getByText("document-24")).toBeVisible();
    expect(screen.getByText(/Showing a sample; more unresolved uploads remain/)).toBeVisible();
    expect(screen.getByText(/They do not expire into verified cleanup/)).toBeVisible();
    expect(screen.queryByText("PRIVATE UPLOAD CONTENT")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /force|complete|ignore upload/i })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Resume cleanup for/ }));
    expect(await screen.findByText(/Cleanup pass finished/)).toBeInTheDocument();
    expect(screen.getByText(/Removed from chats. Retry needed/)).toBeInTheDocument();
    expect(screen.queryByText(/Cleanup last verified:/)).not.toBeInTheDocument();
    expect(writes()).toHaveLength(1);
  });

  it("labels the last verification timestamp and its exclusions, not physical erasure", async () => {
    fetchMock.mockImplementation(async () => json(page([VERIFIED_DELETION])));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    const verified = await screen.findByText(/Cleanup last verified:/);
    expect(verified.querySelector("time")).toHaveAttribute("datetime", VERIFIED_DELETION.lastVerifiedAt);
    expect(screen.getByText(/not proof of physical erasure/)).toBeInTheDocument();
    await user.click(screen.getByText("What this status covers"));
    expect(screen.getByText(/Verification covers conversation content and inline originals only/)).toHaveTextContent(
      /does not cover backups, provider sandboxes, library documents, memories, or generated and processed media/,
    );
    expect(screen.getByText(/Minimal deletion records and write fences/)).toHaveTextContent("retained indefinitely");
    expect(screen.queryByRole("button", { name: /Resume cleanup for/ })).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it("never invents a missing verification time", async () => {
    fetchMock.mockImplementation(async () => json(page([{ ...VERIFIED_DELETION, lastVerifiedAt: null }])));
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    const verified = await screen.findByText(/Cleanup last verified:/);
    expect(verified).toHaveTextContent("timestamp unavailable");
    expect(verified.querySelector("time")).toBeNull();
  });

  it("pages 50 rows using the opaque cursor, deduplicates overlap, and resets the page on refresh", async () => {
    const first = Array.from({ length: 50 }, (_, index) => ({ ...PENDING_DELETION, sessionId: `removed-${index}` }));
    fetchMock.mockResolvedValueOnce(json(page(first, "cursor/+ ?")))
      .mockResolvedValueOnce(json(page([{ ...VERIFIED_DELETION, sessionId: "removed-0" }, { ...PENDING_DELETION, sessionId: "last" }])))
      .mockResolvedValueOnce(json(page(first, "new-cursor")));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    const rows = await screen.findByRole("list", { name: "Deletion requests" });
    expect(within(rows).getAllByRole("listitem")).toHaveLength(50);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Load more deletion requests" }));
    await screen.findByRole("heading", { name: "Conversation last" });
    expect(within(rows).getAllByRole("listitem")).toHaveLength(51);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/sessions/deletions?cursor=cursor%2F%2B+%3F", { cache: "no-store" });
    expect(screen.queryByRole("button", { name: "Load more deletion requests" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByRole("button", { name: "Load more deletion requests" });
    expect(within(rows).getAllByRole("listitem")).toHaveLength(50);
    expect(screen.queryByRole("heading", { name: "Conversation last" })).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it("retains loaded rows and a retryable cursor on a page failure", async () => {
    fetchMock.mockResolvedValueOnce(json(page([PENDING_DELETION], "next")))
      .mockResolvedValueOnce(json({ detail: "Next page unavailable" }, 503))
      .mockResolvedValueOnce(json(page([{ ...PENDING_DELETION, sessionId: "second" }])));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Load more deletion requests" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Next page unavailable");
    expect(screen.getByRole("heading", { name: "Conversation removed/session" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Load more deletion requests" }));
    await screen.findByRole("heading", { name: "Conversation second" });
    expect(fetchMock.mock.calls[1][0]).toBe(fetchMock.mock.calls[2][0]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("fails visibly rather than looping back to page one when pagination is incomplete", async () => {
    fetchMock.mockResolvedValueOnce(json({ items: [], hasMore: true, nextCursor: null }));
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("next deletion-status page is unavailable");
    expect(screen.queryByText(/No resumable deletion/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more deletion requests" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])("ignores an older initial read after a newer refresh (old failure=%s)", async (fails) => {
    const old = deferred<Response>();
    fetchMock.mockReturnValueOnce(old.promise).mockResolvedValueOnce(json(page([VERIFIED_DELETION])));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText(/Cleanup last verified:/);
    await act(async () => old.resolve(fails ? json({ detail: "Stale failure" }, 503) : json(page([], "stale-cursor"))));
    expect(screen.getByText(/Cleanup last verified:/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more deletion requests" })).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it("ignores a stale next page after refreshing the list", async () => {
    const oldPage = deferred<Response>();
    fetchMock.mockResolvedValueOnce(json(page([PENDING_DELETION], "next")))
      .mockReturnValueOnce(oldPage.promise)
      .mockResolvedValueOnce(json(page([VERIFIED_DELETION])));
    const user = userEvent.setup();
    render(<ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Load more deletion requests" }));
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    await screen.findByText(/Cleanup last verified:/);
    await act(async () => oldPage.resolve(json(page([{ ...PENDING_DELETION, sessionId: "stale-row" }], "stale-next"))));
    expect(screen.queryByRole("heading", { name: "Conversation stale-row" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more deletion requests" })).not.toBeInTheDocument();
  });

  it.each([false, true])("ignores old reads after close/reopen (old failure=%s)", async (fails) => {
    const old = deferred<Response>();
    fetchMock.mockReturnValueOnce(old.promise).mockResolvedValueOnce(json(page([VERIFIED_DELETION])));
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open deletion status" }));
    await user.click(screen.getByRole("button", { name: "Close deletion status" }));
    await user.click(screen.getByRole("button", { name: "Open deletion status" }));
    await screen.findByText(/Cleanup last verified:/);
    await act(async () => old.resolve(fails ? json({ detail: "Stale error" }, 503) : json(page([PENDING_DELETION]))));
    expect(screen.getByText(/Cleanup last verified:/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it.each([false, true])("blocks duplicate resumes across reopening and ignores the former view's response (failure=%s)", async (fails) => {
    const request = deferred<Response>();
    fetchMock.mockImplementation(async (_input, init) => init?.method === "POST" ? request.promise : json(page([PENDING_DELETION])));
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "Open deletion status" }));
    const resume = await screen.findByRole("button", { name: /Resume cleanup for/ });
    fireEvent.click(resume);
    fireEvent.click(resume);
    expect(resume).toBeDisabled();
    expect(screen.getByRole("button", { name: "Refresh status" })).toBeDisabled();
    expect(writes()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Close deletion status" }));
    await user.click(screen.getByRole("button", { name: "Open deletion status" }));
    const reopened = await screen.findByRole("button", { name: /Resume cleanup for/ });
    expect(reopened).toBeDisabled();
    await user.click(reopened);
    expect(writes()).toHaveLength(1);
    await act(async () => request.resolve(fails ? json({ detail: "Former view failed" }, 503) : json(VERIFIED_DELETION)));
    expect(reopened).toBeEnabled();
    expect(screen.getByText(/Removed from chats. Cleanup pending/)).toBeInTheDocument();
    expect(screen.queryByText(/Cleanup last verified:/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    fetchMock.mockResolvedValueOnce(json(VERIFIED_DELETION));
    await user.click(reopened);
    await screen.findByText(/Cleanup last verified:/);
    expect(writes()).toHaveLength(2);
  });

  it("reads a specific deletion and preserves a pending pass when switching to all requests", async () => {
    const request = deferred<Response>();
    fetchMock.mockImplementation(async (input, init) => {
      if (init?.method === "POST") return request.promise;
      return json(String(input).endsWith("/deletion") ? PENDING_DELETION : page([PENDING_DELETION]));
    });
    const user = userEvent.setup();
    render(<Harness initialSessionId={PENDING_DELETION.sessionId} />);
    await user.click(screen.getByRole("button", { name: "Open deletion status" }));
    await user.click(await screen.findByRole("button", { name: /Resume cleanup for/ }));
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/sessions/removed%2Fsession/deletion", { cache: "no-store" });
    await user.click(screen.getByRole("button", { name: "All deletion requests" }));
    expect(await screen.findByRole("button", { name: /Resume cleanup for/ })).toBeDisabled();
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/sessions/deletions", { cache: "no-store" });
    await act(async () => request.resolve(json(VERIFIED_DELETION)));
    expect(screen.getByText(/Removed from chats. Cleanup pending/)).toBeInTheDocument();
    expect(writes()).toHaveLength(1);
  });

  it.each(["read", "resume"] as const)("isolates a former owner's late %s from the current owner", async (operation) => {
    let currentOwner = "alice";
    const subscribers = new Set<() => void>();
    const owner: MemoryPreferenceOwner = {
      getSnapshot: () => currentOwner,
      subscribe: (notify) => { subscribers.add(notify); return () => { subscribers.delete(notify); }; },
    };
    const old = deferred<Response>();
    const callOwners: string[] = [];
    fetchMock.mockImplementation(async (_input, init) => {
      callOwners.push(currentOwner);
      if (currentOwner === "alice" && (operation === "read" || init?.method === "POST")) return old.promise;
      return json(init?.method === "POST" ? VERIFIED_DELETION : page([PENDING_DELETION]));
    });
    const user = userEvent.setup();
    rtlRender(<MemoryPreferenceProvider owner={owner}><ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} /></MemoryPreferenceProvider>);
    if (operation === "resume") await user.click(await screen.findByRole("button", { name: /Resume cleanup for/ }));
    act(() => { currentOwner = "bob"; for (const notify of subscribers) notify(); });
    const bobResume = await screen.findByRole("button", { name: /Resume cleanup for/ });
    expect(bobResume).toBeEnabled();
    const calls = fetchMock.mock.calls.length;
    await act(async () => old.resolve(json(operation === "read" ? page([VERIFIED_DELETION]) : VERIFIED_DELETION)));
    expect(screen.getByText(/Removed from chats. Cleanup pending/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(calls);
    await user.click(bobResume);
    await screen.findByText(/Cleanup last verified:/);
    expect(callOwners.at(-1)).toBe("bob");
    expect(writes()).toHaveLength(operation === "resume" ? 2 : 1);
  });

  it("rechecks current identity before dispatch, even before the provider has rerendered", async () => {
    let currentOwner: string | null = "alice";
    const subscribers = new Set<() => void>();
    const owner: MemoryPreferenceOwner = {
      getSnapshot: () => currentOwner,
      subscribe: (notify) => { subscribers.add(notify); return () => { subscribers.delete(notify); }; },
    };
    fetchMock.mockImplementation(async (_input, init) => json(init?.method === "POST" ? VERIFIED_DELETION : page([PENDING_DELETION])));
    rtlRender(<MemoryPreferenceProvider owner={owner}><ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} /></MemoryPreferenceProvider>);
    const resume = await screen.findByRole("button", { name: /Resume cleanup for/ });
    currentOwner = null;
    fireEvent.click(resume);
    expect(writes()).toHaveLength(0);
    currentOwner = "alice";
    fireEvent.click(resume);
    await screen.findByText(/Cleanup last verified:/);
    expect(writes()).toHaveLength(1);
    act(() => { currentOwner = null; for (const notify of subscribers) notify(); });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps an in-flight pass disabled when its owner returns before it settles", async () => {
    let currentOwner = "alice";
    const subscribers = new Set<() => void>();
    const owner: MemoryPreferenceOwner = {
      getSnapshot: () => currentOwner,
      subscribe: (notify) => { subscribers.add(notify); return () => { subscribers.delete(notify); }; },
    };
    const pending = deferred<Response>();
    fetchMock.mockImplementation(async (_input, init) => init?.method === "POST" ? pending.promise : json(page([PENDING_DELETION])));
    const user = userEvent.setup();
    rtlRender(<MemoryPreferenceProvider owner={owner}><ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} /></MemoryPreferenceProvider>);
    await user.click(await screen.findByRole("button", { name: /Resume cleanup for/ }));
    act(() => { currentOwner = "bob"; for (const notify of subscribers) notify(); });
    expect(await screen.findByRole("button", { name: /Resume cleanup for/ })).toBeEnabled();
    act(() => { currentOwner = "alice"; for (const notify of subscribers) notify(); });
    const returning = await screen.findByRole("button", { name: /Resume cleanup for/ });
    expect(returning).toBeDisabled();
    await user.click(returning);
    expect(writes()).toHaveLength(1);
    await act(async () => pending.resolve(json(VERIFIED_DELETION)));
    expect(returning).toBeEnabled();
    expect(screen.getByText(/Removed from chats. Cleanup pending/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(4);
    fetchMock.mockResolvedValueOnce(json(VERIFIED_DELETION));
    await user.click(returning);
    await screen.findByText(/Cleanup last verified:/);
    expect(writes()).toHaveLength(2);
  });

  it("restarts only read-only work under Strict Mode and ignores the first effect's late read", async () => {
    const first = deferred<Response>();
    fetchMock.mockReturnValueOnce(first.promise).mockResolvedValueOnce(json(page([VERIFIED_DELETION])));
    rtlRender(<StrictMode><MemoryPreferenceProvider><ConversationDeletionPanel onShowAll={vi.fn()} onClose={vi.fn()} /></MemoryPreferenceProvider></StrictMode>);
    await screen.findByText(/Cleanup last verified:/);
    await act(async () => first.resolve(json(page([PENDING_DELETION]))));
    expect(screen.getByText(/Cleanup last verified:/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(writes()).toHaveLength(0);
  });

  it("keeps keyboard focus inside the narrow modal, includes disclosures, and returns it on Escape", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    const opener = screen.getByRole("button", { name: "Open deletion status" });
    await user.click(opener);
    const dialog = screen.getByRole("dialog", { name: "Conversation deletion status" });
    await screen.findByRole("button", { name: /Resume cleanup for/ });
    const close = screen.getByRole("button", { name: "Close deletion status" });
    await waitFor(() => expect(close).toHaveFocus());
    expect(dialog.firstElementChild).toHaveStyle({ overflowY: "auto" });
    expect(screen.getByRole("heading", { name: "Conversation removed/session" }).closest("li")).toHaveStyle({ overflowWrap: "anywhere" });
    await user.tab({ shift: true });
    expect(screen.getByText("Incomplete conversation creation")).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    screen.getByText("Progress details").focus();
    await user.tab();
    expect(screen.getByText("Incomplete conversation creation")).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(screen.getByText("Unrelated active conversation")).toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });
});
