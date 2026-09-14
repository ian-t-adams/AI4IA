import { afterEach, beforeEach, expect, it, vi } from "vitest";
import {
  decideWorkflowApproval, disableWorkflowSchedule, getWorkflowAutomationConfig,
  reviewWorkflowApproval, startResumableWorkflow,
} from "./api";
import { DEFAULT_AUTOMATION_LIMITS, automationKey } from "./workflowAutomation";

const fetcher = vi.hoisted(() => vi.fn());
vi.mock("./auth", () => ({ apiFetch: fetcher }));
beforeEach(() => fetcher.mockImplementation(async () =>
  new Response(JSON.stringify({ status: "running" }), { status: 200 })));
afterEach(() => vi.clearAllMocks());

it("uses the same-origin authenticated path without retrying an ambiguous approval", async () => {
  fetcher.mockRejectedValueOnce(new Error("response lost"));
  await expect(decideWorkflowApproval("owner:run", "draft", "approve", {
    requestId: "request", grant: "opaque",
  })).rejects.toThrow("response lost");
  expect(fetcher).toHaveBeenCalledTimes(1);
  const [path, options] = fetcher.mock.calls[0];
  expect(path).toBe("/api/workflows/automation/runs/owner%3Arun/approvals/draft/decision");
  expect(JSON.parse(options.body)).toEqual({ decision: "approve", requestId: "request", grant: "opaque" });
});

it("sends a denial without a grant and reviews only through an explicit POST", async () => {
  await decideWorkflowApproval("r", "d", "deny", { requestId: "unused", grant: "must-not-send" });
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ decision: "deny" });
  await reviewWorkflowApproval("r", "d");
  expect(fetcher.mock.calls[1][1]).toMatchObject({ method: "POST", cache: "no-store" });
});

it("retains caller idempotency and sends explicit bounded, uncapped-dollar intent", async () => {
  const body = {
    selection: { name: "flow", model: "model", documentIds: [] },
    input: "hello", limits: DEFAULT_AUTOMATION_LIMITS, idempotencyKey: "same-key",
  };
  await startResumableWorkflow(body);
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual(body);
  expect(body.limits.spendMode).toBe("no_hard_dollar_cap");
  expect(automationKey()).toMatch(/Z~[a-f0-9-]{36}$/);
});

it("uses revision guards for schedule disable and reports missing capability honestly", async () => {
  await disableWorkflowSchedule("schedule", 7);
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ expectedRevision: 7 });
  fetcher.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Not enabled" }), { status: 404 }));
  await expect(getWorkflowAutomationConfig()).rejects.toThrow("Not enabled");
});
