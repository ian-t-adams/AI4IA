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
        `data: ${JSON.stringify({ metadata: { userMessageId: "u1", assistantMessageId: "a1" } })}\n\n`,
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
    let assistantMessageId: string | null = null;
    await new Promise<void>((resolve) => {
      streamChat(
        { sessionId: "s1", content: "hi" },
        {
          onMetadata: (metadata) => {
            assistantMessageId = metadata.assistantMessageId;
          },
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
    expect(assistantMessageId).toBe("a1");
    expect(steps).toEqual(["Searching the web", "Searched the web"]);
    expect(text).toBe("Here is the answer.");
  });

  it("ignores step events for callers that don't handle them", async () => {
    mockApiFetch.mockResolvedValue(
      sseResponse([
        `data: ${JSON.stringify({ metadata: { userMessageId: "u1", assistantMessageId: "a1" } })}\n\n`,
        `data: ${JSON.stringify({ step: { kind: "tool_start", label: "x" } })}\n\n`,
        `data: ${JSON.stringify({ choices: [{ delta: { content: "ok" } }] })}\n\n`,
        "data: [DONE]\n\n",
      ]),
    );
    let text = "";
    await new Promise<void>((resolve) => {
      streamChat(
        { sessionId: "s1", content: "hi" },
        {
          onMetadata: () => {},
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

  it("surfaces a missing-metadata stream error without discarding buffered text", async () => {
    mockApiFetch.mockResolvedValue(
      sseResponse([
        `data: ${JSON.stringify({ choices: [{ delta: { content: "partial" } }] })}\n\n`,
        "data: [DONE]\n\n",
      ]),
    );
    let text = "";
    let error = "";
    await new Promise<void>((resolve) => {
      streamChat(
        { sessionId: "s1", content: "hi" },
        {
          onMetadata: () => {},
          onDelta: (delta) => {
            text += delta;
          },
          onDone: resolve,
          onError: (message) => {
            error = message;
            resolve();
          },
        },
      );
    });
    expect(text).toBe("partial");
    expect(error).toContain("without message metadata");
  });

  it("marks only a 4xx HTTP rejection as definite pre-acceptance", async () => {
    mockApiFetch.mockResolvedValue({
      ok: false,
      status: 429,
      statusText: "Too Many Requests",
      text: async () => "rate limited",
    } as Response);
    let accepted: boolean | null = null;
    let definitePreAcceptance: boolean | undefined;
    await new Promise<void>((resolve) => {
      streamChat(
        { sessionId: "s1", content: "hi" },
        {
          onMetadata: () => {},
          onDelta: () => {},
          onDone: resolve,
          onError: (_message, info) => {
            accepted = info.accepted;
            definitePreAcceptance = info.definitePreAcceptance;
            resolve();
          },
        },
      );
    });
    expect(accepted).toBe(false);
    expect(definitePreAcceptance).toBe(true);
  });

  it("treats a 5xx response as ambiguous because the turn may be durable", async () => {
    mockApiFetch.mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Unavailable",
      text: async () => "upstream reset",
    } as Response);
    let definitePreAcceptance: boolean | undefined;
    await new Promise<void>((resolve) => {
      streamChat(
        { sessionId: "s1", content: "hi" },
        {
          onMetadata: () => {},
          onDelta: () => {},
          onDone: resolve,
          onError: (_message, info) => {
            definitePreAcceptance = info.definitePreAcceptance;
            resolve();
          },
        },
      );
    });
    expect(definitePreAcceptance).toBe(false);
  });

  it("reports a no-metadata abort as ambiguous", async () => {
    mockApiFetch.mockImplementation(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("aborted", "AbortError"));
          });
        }),
    );
    let definitePreAcceptance: boolean | undefined;
    const completed = new Promise<void>((resolve) => {
      const abort = streamChat(
        { sessionId: "s1", content: "hi" },
        {
          onMetadata: () => {},
          onDelta: () => {},
          onDone: resolve,
          onError: () => resolve(),
          onAbort: (info) => {
            definitePreAcceptance = info?.definitePreAcceptance;
            resolve();
          },
        },
      );
      abort();
    });
    await completed;
    expect(definitePreAcceptance).toBe(false);
  });
});
