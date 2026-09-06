import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./auth", () => ({ apiFetch: vi.fn() }));
import { cancelWorkflowRun, cancelWorkflowRunByKey, getWorkflowRun, listWorkflows, runWorkflow, setSessionToolConsent } from "./api";
import { apiFetch } from "./auth";

const fetchMock = vi.mocked(apiFetch);
afterEach(() => fetchMock.mockReset());

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}

describe("server-owned tool consent API", () => {
  it.each([true, false])("uses the authenticated dedicated session endpoint for enabled=%s", async (enabled) => {
    const session = { id: "a/b", toolConsent: null };
    fetchMock.mockResolvedValue(json(session));
    expect(await setSessionToolConsent("a/b", enabled)).toEqual(session);
    expect(fetchMock).toHaveBeenCalledWith("/api/sessions/a%2Fb/tool-consent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
  });

  it("surfaces operator rejection instead of fabricating a grant", async () => {
    fetchMock.mockResolvedValue(json({ detail: "Tool auto-approval is disabled by the operator." }, 409));
    await expect(setSessionToolConsent("s1", true)).rejects.toMatchObject({
      status: 409, detail: "Tool auto-approval is disabled by the operator.",
    });
  });

  it.each([undefined, false, "true", 1])("fails closed for availability %s", async (available) => {
    fetchMock.mockResolvedValue(json({ workflows: [], toolAutoApproveAvailable: available }));
    expect(await listWorkflows()).toEqual({ workflows: [], durableAvailable: false, toolAutoApproveAvailable: false });
  });

  it("exposes the runtime gate only when the server reports true", async () => {
    fetchMock.mockResolvedValue(json({ workflows: [], durableAvailable: true, toolAutoApproveAvailable: true }));
    expect((await listWorkflows()).toolAutoApproveAvailable).toBe(true);
  });

  it("sends false for a fresh invocation unless explicitly opted in", async () => {
    fetchMock.mockResolvedValue(json({ sessionId: "s1", ok: true, message: {} }));
    await runWorkflow("brief", { sessionId: "s1", input: "hello" });
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      sessionId: "s1", input: "hello", autoApproveTools: false,
    });
  });
  it("revokes a workflow run with its exact server id and session ownership binding", async () => {
    const status = { runId: "user:run/id", status: "TERMINATED", ok: false };
    fetchMock.mockResolvedValue(json(status));
    expect(await cancelWorkflowRun("user:run/id", "session-1")).toEqual(status);
    expect(fetchMock).toHaveBeenCalledWith("/api/workflows/runs/user%3Arun%2Fid/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: "session-1" }),
    });
  });

  it("includes the session on durable polling so revocation is observed", async () => {
    fetchMock.mockResolvedValue(json({ runId: "u:r", status: "TERMINATED" }));
    await getWorkflowRun("u:r", "s/1");
    expect(fetchMock).toHaveBeenCalledWith("/api/workflows/runs/u%3Ar?sessionId=s%2F1", { cache: "no-store" });
  });

  it("cancels a direct invocation by its original key without requiring a run id", async () => {
    const status = { runId: "u1:direct", status: "TERMINATED", ok: false };
    fetchMock.mockResolvedValue(json(status));
    expect(await cancelWorkflowRunByKey("research/demo", "s1", "original-key")).toEqual(status);
    expect(fetchMock).toHaveBeenCalledWith("/api/workflows/research%2Fdemo/cancel", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: "s1", idempotencyKey: "original-key" }),
    });
  });

  it("surfaces a pre-claim 404 without manufacturing a cancellation acknowledgement", async () => {
    fetchMock.mockResolvedValue(json({ detail: "Run is not active yet" }, 404));
    await expect(cancelWorkflowRunByKey("research", "s1", "original-key")).rejects.toMatchObject({ status: 404 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

});
