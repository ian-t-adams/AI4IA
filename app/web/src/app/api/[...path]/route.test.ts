import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

type RouteModule = typeof import("./route");

async function loadRoute(devUser = ""): Promise<RouteModule> {
  vi.resetModules();
  vi.stubEnv("API_BASE_URL", "http://localhost:8080");
  vi.stubEnv("DEV_USER", devUser);
  return import("./route");
}

function context(path: string[]) {
  return { params: Promise.resolve({ path }) };
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("same-origin API proxy", () => {
  it.each([
    ["parent traversal", ["..", "docs"]],
    ["decoded slash", ["../../openapi.json"]],
    ["query injection", ["admin/usage?days=99999"]],
    ["fragment injection", ["chat#ignored"]],
    ["backslash", ["..\\docs"]],
  ])("rejects %s before issuing an upstream request", async (_name, path) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { GET } = await loadRoute();

    const response = await GET(
      new NextRequest("https://app.example.test/api/test"),
      context(path),
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ detail: "Invalid API path" });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("re-encodes path segments and strips request and response hop headers", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response("ok", {
        headers: {
          connection: "x-upstream-hop",
          "x-upstream-hop": "secret",
          "x-kept": "upstream",
        },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { GET } = await loadRoute();
    const request = new NextRequest(
      "https://app.example.test/api/sessions/a%20b?limit=1",
      {
        headers: {
          connection: "x-client-hop",
          "x-client-hop": "secret",
          "x-dev-user": "attacker",
          "x-kept": "browser",
        },
      },
    );

    const response = await GET(request, context(["sessions", "a b"]));

    expect(fetchMock).toHaveBeenCalledOnce();
    const [target, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(target).toBe("http://localhost:8080/api/sessions/a%20b?limit=1");
    const headers = init.headers as Headers;
    expect(headers.get("x-client-hop")).toBeNull();
    expect(headers.get("x-dev-user")).toBeNull();
    expect(headers.get("x-kept")).toBe("browser");
    expect(response.headers.get("x-upstream-hop")).toBeNull();
    expect(response.headers.get("x-kept")).toBe("upstream");
    await expect(response.text()).resolves.toBe("ok");
  });

  it("overwrites a browser-supplied dev identity with the server value", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null));
    vi.stubGlobal("fetch", fetchMock);
    const { GET } = await loadRoute("trusted@example.test");

    await GET(
      new NextRequest("https://app.example.test/api/models", {
        headers: { "x-dev-user": "attacker@example.test" },
      }),
      context(["models"]),
    );

    const headers = fetchMock.mock.calls[0][1].headers as Headers;
    expect(headers.get("x-dev-user")).toBe("trusted@example.test");
  });

  it("streams request bodies with duplex half and fails closed on upstream errors", async () => {
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const { GET, POST } = await loadRoute();

    const unavailable = await GET(
      new NextRequest("https://app.example.test/api/models"),
      context(["models"]),
    );
    expect(unavailable.status).toBe(502);

    await POST(
      new NextRequest("https://app.example.test/api/chat", {
        method: "POST",
        body: "payload",
      }),
      context(["chat"]),
    );
    const init = fetchMock.mock.calls[1][1] as RequestInit & { duplex?: string };
    expect(init.duplex).toBe("half");
    expect(init.body).toBeInstanceOf(ReadableStream);
  });
});
