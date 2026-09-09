import { afterEach, describe, expect, it, vi } from "vitest";
import { deleteSession, getSessionDeletion, listSessionDeletions, listSessionInitializations, reconcileSessionDeletion } from "./api";
import { apiFetch } from "./auth";
import { INITIALIZING_SESSION, PENDING_DELETION, VERIFIED_DELETION } from "./deletionTestFixtures";
import type { InitializationPage } from "./types";

const initializationPage: InitializationPage = {
  items: [INITIALIZING_SESSION], hasMore: false, nextCursor: null,
  observation: "not_completion_evidence",
};

vi.mock("./auth", () => ({ apiFetch: vi.fn() }));
const fetchMock = vi.mocked(apiFetch);
afterEach(() => vi.resetAllMocks());

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

describe("conversation deletion API", () => {
  it("keeps the legacy 204 result undefined without attempting to parse a body", async () => {
    const response = new Response(null, { status: 204 });
    const parse = vi.spyOn(response, "json");
    fetchMock.mockResolvedValue(response);
    await expect(deleteSession("a/b ?#")).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/a%2Fb%20%3F%23", { method: "DELETE" });
    expect(parse).not.toHaveBeenCalled();
  });

  it.each([[202, PENDING_DELETION], [200, VERIFIED_DELETION]] as const)(
    "returns the server's deletion evidence for HTTP %s", async (status, body) => {
      fetchMock.mockResolvedValue(json(body, status));
      await expect(deleteSession(body.sessionId)).resolves.toEqual(body);
      expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/removed%2Fsession", { method: "DELETE" });
    },
  );

  it("surfaces migration_required's string detail without a fallback DELETE", async () => {
    const detail = "This conversation requires approved migration before deletion.";
    fetchMock.mockResolvedValue(json({ detail, code: "migration_required" }, 409));
    await expect(deleteSession("legacy")).rejects.toMatchObject({ status: 409, detail, message: `409: ${detail}` });
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/legacy", { method: "DELETE" });
  });

  it("reads one owner-scoped page at a time without caching or following its cursor automatically", async () => {
    const cursor = "key/+?= value";
    const first = { items: [PENDING_DELETION], hasMore: true, nextCursor: cursor };
    const last = { items: [VERIFIED_DELETION], hasMore: false, nextCursor: null };
    fetchMock.mockResolvedValueOnce(json(first)).mockResolvedValueOnce(json(last));
    await expect(listSessionDeletions()).resolves.toEqual(first);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/deletions", { cache: "no-store" });
    await expect(listSessionDeletions(cursor)).resolves.toEqual(last);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/sessions/deletions?cursor=key%2F%2B%3F%3D+value", { cache: "no-store" });
  });

  it("reads minimal initialization reservations and passes the opaque cursor back without decoding it", async () => {
    const cursor = "opaque/+%2F?= cursor";
    const first = { ...initializationPage, hasMore: true, nextCursor: cursor };
    const last = { ...initializationPage, items: [] };
    fetchMock.mockResolvedValueOnce(json(first)).mockResolvedValueOnce(json(last));
    await expect(listSessionInitializations()).resolves.toEqual(first);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/initializations", { cache: "no-store" });
    await expect(listSessionInitializations(cursor)).resolves.toEqual(last);
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/sessions/initializations?cursor=opaque%2F%2B%252F%3F%3D+cursor", { cache: "no-store" });
  });

  it.each([
    { label: "null", value: null },
    { label: "array envelope", value: [] },
    { label: "missing observation", value: { ...initializationPage, observation: undefined } },
    { label: "invalid observation", value: { ...initializationPage, observation: "completion_evidence" } },
    { label: "missing items", value: { ...initializationPage, items: undefined } },
    { label: "null items", value: { ...initializationPage, items: null } },
    { label: "object items", value: { ...initializationPage, items: {} } },
    { label: "non-Boolean hasMore", value: { ...initializationPage, hasMore: "false" } },
    { label: "missing cursor", value: { ...initializationPage, nextCursor: undefined } },
    { label: "non-string cursor", value: { ...initializationPage, nextCursor: 7 } },
    { label: "missing next page cursor", value: { ...initializationPage, hasMore: true } },
    { label: "null reservation", value: { ...initializationPage, items: [null] } },
    { label: "empty id", value: { ...initializationPage, items: [{ ...INITIALIZING_SESSION, sessionId: "" }] } },
    { label: "wrong id type", value: { ...initializationPage, items: [{ ...INITIALIZING_SESSION, sessionId: 7 }] } },
    { label: "non-initializing record", value: { ...initializationPage, items: [INITIALIZING_SESSION, { ...INITIALIZING_SESSION, state: "active" }] } },
    { label: "missing date", value: { ...initializationPage, items: [{ ...INITIALIZING_SESSION, createdAt: undefined }] } },
    { label: "invalid date", value: { ...initializationPage, items: [{ ...INITIALIZING_SESSION, createdAt: "2026-99-99T00:00:00Z" }] } },
    { label: "non-ISO date", value: { ...initializationPage, items: [{ ...INITIALIZING_SESSION, createdAt: "123" }] } },
  ])("rejects a malformed initialization page ($label), never converting it to empty success", async ({ value }) => {
    fetchMock.mockResolvedValue(json(value));
    await expect(listSessionInitializations()).rejects.toThrow("malformed incomplete-creation response");
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/initializations", { cache: "no-store" });
  });

  it.each([50, 51])("enforces the observed-page bound for %s reservations", async (count) => {
    const value = { ...initializationPage, items: Array.from({ length: count }, (_, index) => ({ ...INITIALIZING_SESSION, sessionId: `reservation-${index}` })) };
    fetchMock.mockResolvedValue(json(value));
    if (count === 50) await expect(listSessionInitializations()).resolves.toEqual(value);
    else await expect(listSessionInitializations()).rejects.toThrow("malformed incomplete-creation response");
  });

  it.each([
    ["opaque/not-json", "/api/sessions/initializations?cursor=opaque%2Fnot-json"],
    ["", "/api/sessions/initializations?cursor="],
  ])("lets the backend reject cursor '%s' without decoding or a first-page fallback", async (cursor, path) => {
    fetchMock.mockResolvedValue(json({ detail: "Invalid initialization cursor" }, 400));
    await expect(listSessionInitializations(cursor)).rejects.toMatchObject({ status: 400, detail: "Invalid initialization cursor" });
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(path, { cache: "no-store" });
  });

  it("reads the exact encoded conversation status without resuming it", async () => {
    fetchMock.mockResolvedValue(json(PENDING_DELETION));
    await expect(getSessionDeletion(PENDING_DELETION.sessionId)).resolves.toEqual(PENDING_DELETION);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/removed%2Fsession/deletion", { cache: "no-store" });
  });

  it.each([202, 200])("makes one explicit bounded reconcile POST for HTTP %s", async (status) => {
    const body = status === 200 ? VERIFIED_DELETION : PENDING_DELETION;
    fetchMock.mockResolvedValue(json(body, status));
    await expect(reconcileSessionDeletion(body.sessionId)).resolves.toEqual(body);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith("/api/sessions/removed%2Fsession/deletion/reconcile", { method: "POST" });
  });

  it("preserves read access when cleanup is disabled, but surfaces the rejected POST", async () => {
    const detail = "Conversation cleanup is disabled by the operator.";
    fetchMock.mockResolvedValueOnce(json(PENDING_DELETION))
      .mockResolvedValueOnce(json({ detail }, 409));
    await expect(getSessionDeletion("s1")).resolves.toEqual(PENDING_DELETION);
    await expect(reconcileSessionDeletion("s1")).rejects.toMatchObject({ status: 409, detail });
    expect(fetchMock.mock.calls.map((call) => call[1]?.method ?? "GET")).toEqual(["GET", "POST"]);
  });

  it.each([
    () => listSessionDeletions(),
    () => listSessionInitializations(),
    () => getSessionDeletion("s1"),
    () => reconcileSessionDeletion("s1"),
  ])("does not turn an unavailable status into empty or verified progress", async (operation) => {
    fetchMock.mockResolvedValue(json({ detail: "Deletion storage unavailable" }, 503));
    await expect(operation()).rejects.toMatchObject({ status: 503, detail: "Deletion storage unavailable" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("propagates a lost response without retrying or fabricating completion", async () => {
    fetchMock.mockRejectedValue(new TypeError("Network unavailable"));
    await expect(reconcileSessionDeletion("s1")).rejects.toThrow("Network unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
