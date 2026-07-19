import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the same-origin proxy so streamChat can be driven without a network.
vi.mock("./auth", () => ({ apiFetch: vi.fn() }));

import { streamChat } from "./api";
import { apiFetch } from "./auth";

const mockApiFetch = vi.mocked(apiFetch);

function sseResponse(chunks: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(encoder.encode(c));
      controller.close();
    },
  });
  return { ok: true, status: 200, statusText: "OK", body: stream } as unknown as Response;
}

afterEach(() => mockApiFetch.mockReset());

describe("streamChat", () => {
  it("routes step events to onStep, content to onDelta, and finishes on [DONE]", async () => {
    mockApiFetch.mockResolvedValue(
      sseResponse([
        `data: ${JSON.stringify({ step: { kind: "tool_start", label: "Searching the web", tool: "web_search" } })}\n\n`,
        `data: ${JSON.stringify({ step: { kind: "tool_result", label: "Searched the web", tool: "web_search" } })}\n\n`,
        `data: ${JSON.stringify({ choices: [{ delta: { content: "Here is the answer." } }] })}\n\n`,
        "data: [DONE]\n\n",
      ]),
    );
    const steps: string[] = [];
    let text = "";
    let done = false;
    let error: string | null = null;
    await new Promise<void>((resolve) => {
      streamChat(
        {
          sessionId: "s1",
          content: "hi",
          clientTurnId: "123e4567-e89b-42d3-a456-426614174000",
        },
        {
          onStep: (s) => steps.push(s.label),
          onDelta: (t) => {
            text += t;
          },
          onDone: () => {
            done = true;
            resolve();
          },
          onError: (m) => {
            error = m;
            resolve();
          },
        },
      );
    });
    expect(error).toBeNull();
    expect(done).toBe(true);
    expect(steps).toEqual(["Searching the web", "Searched the web"]);
    expect(text).toBe("Here is the answer.");
  });

  it("ignores step events for callers that don't handle them", async () => {
    mockApiFetch.mockResolvedValue(
      sseResponse([
        `data: ${JSON.stringify({ step: { kind: "tool_start", label: "x" } })}\n\n`,
        `data: ${JSON.stringify({ choices: [{ delta: { content: "ok" } }] })}\n\n`,
        "data: [DONE]\n\n",
      ]),
    );
    let text = "";
    await new Promise<void>((resolve) => {
      streamChat(
        {
          sessionId: "s1",
          content: "hi",
          clientTurnId: "123e4567-e89b-42d3-a456-426614174000",
        },
        {
          onDelta: (t) => {
            text += t;
          },
          onDone: () => resolve(),
          onError: () => resolve(),
        },
      );
    });
    expect(text).toBe("ok");
  });

  it("retries an accepted response that loses every SSE byte with the same clientTurnId", async () => {
    mockApiFetch
      .mockResolvedValueOnce(sseResponse([]))
      .mockResolvedValueOnce(
        sseResponse([
          `data: ${JSON.stringify({
            messageId: "a1",
            userMessageId: "u1",
            clientTurnId: "123e4567-e89b-42d3-a456-426614174000",
            status: "complete",
          })}\n\n`,
          `data: ${JSON.stringify({ choices: [{ delta: { content: "replayed" } }] })}\n\n`,
          "data: [DONE]\n\n",
        ]),
      );
    let text = "";
    await new Promise<void>((resolve) => {
      streamChat(
        {
          sessionId: "s1",
          content: "hi",
          clientTurnId: "123e4567-e89b-42d3-a456-426614174000",
        },
        {
          onDelta: (value) => {
            text += value;
          },
          onDone: resolve,
          onError: () => resolve(),
        },
      );
    });

    expect(mockApiFetch).toHaveBeenCalledTimes(2);
    const bodies = mockApiFetch.mock.calls.map(([, init]) =>
      JSON.parse(String(init?.body)),
    );
    expect(bodies[0].clientTurnId).toBe(bodies[1].clientTurnId);
    expect(text).toBe("replayed");
  });

  it("reports definitive pre-SSE HTTP rejection separately", async () => {
    mockApiFetch.mockResolvedValue({
      ok: false,
      status: 429,
      statusText: "Too Many Requests",
      text: async () => "rate limited",
      body: null,
    } as unknown as Response);
    const rejected = vi.fn();
    await new Promise<void>((resolve) => {
      streamChat(
        {
          sessionId: "s1",
          content: "hi",
          clientTurnId: "123e4567-e89b-42d3-a456-426614174000",
        },
        {
          onDelta: vi.fn(),
          onDone: resolve,
          onError: () => resolve(),
          onRejected: rejected,
        },
      );
    });
    expect(rejected).toHaveBeenCalledWith(429, "rate limited");
    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });
});
