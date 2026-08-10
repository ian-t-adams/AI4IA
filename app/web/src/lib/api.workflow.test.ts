import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./auth", () => ({ apiFetch: vi.fn() }));

import { runWorkflow } from "./api";
import { apiFetch } from "./auth";

const mockApiFetch = vi.mocked(apiFetch);

afterEach(() => mockApiFetch.mockReset());

describe("runWorkflow durable idempotency", () => {
  it("retries an ambiguous transport failure with the identical caller key", async () => {
    mockApiFetch
      .mockRejectedValueOnce(new TypeError("network response lost"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            sessionId: "s1",
            runId: "u1:stable",
            status: "accepted",
            idempotencyKey: "durable-intent-key",
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      );

    const outcome = await runWorkflow("summarize", {
      sessionId: "s1",
      input: "otters",
      durable: true,
      idempotencyKey: "durable-intent-key",
    });

    expect(outcome.scheduled).toBe(true);
    expect(mockApiFetch).toHaveBeenCalledTimes(2);
    const bodies = mockApiFetch.mock.calls.map(([, init]) =>
      JSON.parse(String(init?.body)),
    );
    expect(bodies[0]).toEqual(bodies[1]);
    expect(bodies[0].idempotencyKey).toBe("durable-intent-key");
  });

  it("does not retry a synchronous workflow after a transport failure", async () => {
    mockApiFetch.mockRejectedValue(new TypeError("network response lost"));
    await expect(
      runWorkflow("summarize", { sessionId: "s1", input: "otters" }),
    ).rejects.toThrow("network response lost");
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });
});
