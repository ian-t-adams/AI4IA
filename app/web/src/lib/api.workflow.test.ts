import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("./auth", () => ({ apiFetch: vi.fn() }));

import { runWorkflow } from "./api";
import { apiFetch } from "./auth";

const mockApiFetch = vi.mocked(apiFetch);

afterEach(() => {
  mockApiFetch.mockReset();
  vi.useRealTimers();
});

describe("runWorkflow durable idempotency", () => {
  it("recovers transport failure through pending with the byte-identical request", async () => {
    vi.useFakeTimers();
    mockApiFetch
      .mockRejectedValueOnce(new TypeError("network response lost"))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            sessionId: "s1",
            runId: "u1:stable",
            status: "pending",
            idempotencyKey: "durable-intent-key",
            retryAfterSeconds: 30,
            leaseExpiresAt: "2026-08-10T05:00:30Z",
          }),
          {
            status: 202,
            headers: {
              "Content-Type": "application/json",
              "Retry-After": "30",
            },
          },
        ),
      )
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

    const outcomePromise = runWorkflow("summarize", {
      sessionId: "s1",
      input: "otters",
      durable: true,
      idempotencyKey: "durable-intent-key",
      autoApproveTools: true,
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(mockApiFetch).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(29_999);
    expect(mockApiFetch).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(1);
    const outcome = await outcomePromise;

    expect(outcome.scheduled).toBe(true);
    expect(mockApiFetch).toHaveBeenCalledTimes(3);
    const bodies = mockApiFetch.mock.calls.map(([, init]) =>
      String(init?.body),
    );
    expect(new Set(bodies)).toEqual(new Set([bodies[0]]));
    expect(JSON.parse(bodies[0]).idempotencyKey).toBe("durable-intent-key");
    expect(JSON.parse(bodies[0]).autoApproveTools).toBe(true);
  });

  it("returns an accepted durable run immediately without creating a timer", async () => {
    vi.useFakeTimers();
    mockApiFetch.mockResolvedValueOnce(
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
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("cancels a pending recovery timer without another request", async () => {
    vi.useFakeTimers();
    mockApiFetch.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          sessionId: "s1",
          runId: "u1:stable",
          status: "pending",
          idempotencyKey: "durable-intent-key",
          retryAfterSeconds: 30,
        }),
        {
          status: 202,
          headers: {
            "Content-Type": "application/json",
            "Retry-After": "30",
          },
        },
      ),
    );
    const controller = new AbortController();

    const outcome = runWorkflow(
      "summarize",
      {
        sessionId: "s1",
        input: "otters",
        durable: true,
        idempotencyKey: "durable-intent-key",
      },
      controller.signal,
    );
    await vi.advanceTimersByTimeAsync(0);
    controller.abort();

    await expect(outcome).rejects.toMatchObject({ name: "AbortError" });
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not retry a synchronous workflow after a transport failure", async () => {
    mockApiFetch.mockRejectedValue(new TypeError("network response lost"));
    await expect(
      runWorkflow("summarize", { sessionId: "s1", input: "otters" }),
    ).rejects.toThrow("network response lost");
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });
});
