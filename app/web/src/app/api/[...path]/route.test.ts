import { afterEach, describe, expect, it, vi } from "vitest";
import { NextRequest } from "next/server";

type RouteModule = typeof import("./route");

async function loadRoute(
  devUser = "",
  pilotEnabled = false,
  apiBaseUrl = pilotEnabled
    ? "https://api.internal.synthetic.eastus2.azurecontainerapps.io"
    : "http://localhost:8080",
  apiAllowedHost = new URL(apiBaseUrl).hostname,
): Promise<RouteModule> {
  vi.resetModules();
  vi.stubEnv("API_BASE_URL", apiBaseUrl);
  vi.stubEnv("CHAT_PILOT_API_ALLOWED_HOST", apiAllowedHost);
  vi.stubEnv("DEV_USER", devUser);
  vi.stubEnv("CHAT_PILOT_UI_ENABLED", pilotEnabled ? "true" : "");
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
  it.each([false, true])("preserves chat throttling without resubmitting (pilot=%s)", async (pilotEnabled) => {
    const body = JSON.stringify({ detail: "Model gateway throttled this request. Try again in 17 seconds." });
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, {
      status: 429, headers: { "retry-after": "17", "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { POST } = await loadRoute("", pilotEnabled);
    const response = await POST(new NextRequest("https://app.example.test/api/chat", {
      method: "POST", body: "{}",
    }), context(["chat"]));

    expect(response.status).toBe(429);
    expect(response.headers.get("retry-after")).toBe("17");
    await expect(response.text()).resolves.toBe(body);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it("uses a configured ACA internal API hostname without following redirects", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const apiBaseUrl = "https://api.internal.synthetic.eastus2.azurecontainerapps.io";
    const { GET } = await loadRoute("", true, apiBaseUrl, new URL(apiBaseUrl).hostname);

    const response = await GET(
      new NextRequest("https://app.example.test/api/models"),
      context(["models"]),
    );

    expect(response.status).toBe(204);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(`${apiBaseUrl}/api/models`);
    expect(fetchMock.mock.calls[0][1].redirect).toBe("manual");
  });

  it.each([
    ["missing host pin", "https://api.internal.synthetic.eastus2.azurecontainerapps.io", ""],
    ["host mismatch", "https://api.internal.synthetic.eastus2.azurecontainerapps.io", "other.internal.synthetic.eastus2.azurecontainerapps.io"],
    ["public host", "https://api.example.test", "api.example.test"],
    ["non-HTTPS URL", "http://api.internal.synthetic.eastus2.azurecontainerapps.io", "api.internal.synthetic.eastus2.azurecontainerapps.io"],
    ["URL path", "https://api.internal.synthetic.eastus2.azurecontainerapps.io/prefix", "api.internal.synthetic.eastus2.azurecontainerapps.io"],
    ["URL credentials", "https://user:pass@api.internal.synthetic.eastus2.azurecontainerapps.io", "api.internal.synthetic.eastus2.azurecontainerapps.io"],
  ])("fails closed before fetch for pilot API target %s", async (_name, apiBaseUrl, apiAllowedHost) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { GET } = await loadRoute("", true, apiBaseUrl, apiAllowedHost);

    const response = await GET(
      new NextRequest("https://app.example.test/api/models"),
      context(["models"]),
    );

    expect(response.status).toBe(503);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({
      detail: "Pilot API target is not configured.",
      code: "chat_pilot_api_target_unavailable",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([false, true])("passes metadata and unchanged throttling SSE (pilot=%s)", async (pilotEnabled) => {
    const metadata = 'data: {"conversationId":"synthetic","userMessageId":"user"}\n\n';
    const error = 'data: {"error":"Model gateway throttled this request. Try again in 17 seconds.","status":429,"retryAfterSeconds":17}\n\n';
    let upstream: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(controller) { upstream = controller; controller.enqueue(new TextEncoder().encode(metadata)); },
    });
    const fetchMock = vi.fn().mockResolvedValue(new Response(body, {
      headers: { "content-type": "text/event-stream" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const { POST } = await loadRoute("", pilotEnabled);
    const response = await POST(new NextRequest("https://app.example.test/api/chat", {
      method: "POST", body: "{}",
    }), context(["chat"]));
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    const reader = response.body!.getReader();
    expect(new TextDecoder().decode((await reader.read()).value)).toBe(metadata);
    upstream!.enqueue(new TextEncoder().encode(error));
    upstream!.close();
    expect(new TextDecoder().decode((await reader.read()).value)).toBe(error);
    expect((await reader.read()).done).toBe(true);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each([300, 301, 302, 303, 304, 307, 308, 399])(
    "refuses pilot upstream status %s without exposing redirect headers or content",
    async (status) => {
      const upstream = new Response(status === 304 ? null : "synthetic redirect content", {
        status,
        headers: {
          location: "https://redirect.example.test/synthetic",
          refresh: "0; url=https://redirect.example.test/synthetic",
          "set-cookie": "synthetic-redirect-cookie=blocked",
          "x-upstream-marker": "synthetic redirect header",
        },
      });
      const cancel = upstream.body ? vi.spyOn(upstream.body, "cancel") : null;
      const fetchMock = vi.fn().mockResolvedValue(upstream);
      vi.stubGlobal("fetch", fetchMock);
      const { POST } = await loadRoute("", true);
      const response = await POST(new NextRequest("https://app.example.test/api/chat", {
        method: "POST", body: '{"content":"synthetic question"}',
      }), context(["chat"]));

      expect(response.status).toBe(502);
      expect([...response.headers.keys()].sort()).toEqual(["cache-control", "content-type"]);
      expect(response.headers.get("cache-control")).toBe("no-store");
      await expect(response.json()).resolves.toEqual({
        detail: "API upstream redirect refused in isolated chat-pilot mode.",
        code: "chat_pilot_upstream_redirect",
      });
      if (cancel) expect(cancel).toHaveBeenCalledOnce();
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(fetchMock.mock.calls[0][1].redirect).toBe("manual");
    },
  );

  it.each([
    ["models", "GET", ["models"]],
    ["session list", "GET", ["sessions"]],
    ["session create", "POST", ["sessions"]],
    ["session read", "GET", ["sessions", "session-1"]],
    ["message history", "GET", ["sessions", "session-1", "messages"]],
  ])("also refuses redirects on the allowed pilot %s route", async (_name, method, path) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("synthetic redirect content", {
      status: 307, headers: { location: "https://redirect.example.test/synthetic" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const route = await loadRoute("", true);
    const request = new NextRequest(`https://app.example.test/api/${path.join("/")}`, {
      method, ...(method === "POST" ? { body: "{}" } : {}),
    });
    const response = await route[method as "GET" | "POST"](request, context(path));

    expect(response.status).toBe(502);
    expect(response.headers.get("location")).toBeNull();
    await expect(response.json()).resolves.toEqual({
      detail: "API upstream redirect refused in isolated chat-pilot mode.",
      code: "chat_pilot_upstream_redirect",
    });
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][1].redirect).toBe("manual");
  });

  it("sanitizes pilot redirect-body cancellation failure without replay", async () => {
    const cancel = vi.fn().mockRejectedValue(new Error("synthetic redirect cleanup content"));
    const upstream = new Response(new ReadableStream<Uint8Array>({ cancel }), {
      status: 308, headers: { location: "https://redirect.example.test/synthetic" },
    });
    const fetchMock = vi.fn().mockResolvedValue(upstream);
    vi.stubGlobal("fetch", fetchMock);
    const { POST } = await loadRoute("", true);
    const response = await POST(new NextRequest("https://app.example.test/api/chat", {
      method: "POST", body: "{}",
    }), context(["chat"]));

    expect(response.status).toBe(502);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("location")).toBeNull();
    await expect(response.json()).resolves.toEqual({ detail: "API upstream unavailable" });
    expect(cancel).toHaveBeenCalledOnce();
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each([301, 302, 303, 307, 308])(
    "preserves ordinary-mode upstream redirect status, Location and body (%s)",
    async (status) => {
      const location = "https://redirect.example.test/synthetic";
      const upstream = new Response("synthetic ordinary redirect content", {
        status, headers: { location },
      });
      const cancel = vi.spyOn(upstream.body!, "cancel");
      const fetchMock = vi.fn().mockResolvedValue(upstream);
      vi.stubGlobal("fetch", fetchMock);
      const { POST } = await loadRoute();
      const response = await POST(new NextRequest("https://app.example.test/api/chat", {
        method: "POST", body: "{}",
      }), context(["chat"]));

      expect(response.status).toBe(status);
      expect(response.headers.get("location")).toBe(location);
      await expect(response.text()).resolves.toBe("synthetic ordinary redirect content");
      expect(cancel).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(fetchMock.mock.calls[0][1].redirect).toBe("manual");
    },
  );

  it.each(["decisions/options", "models"])("forwards cancelled availability reads only on Decision (%s)", async (path) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const { GET } = await loadRoute();
    const controller = new AbortController();
    const request = new NextRequest(`https://app.example.test/api/${path}`, { signal: controller.signal });
    await GET(request, context(path.split("/")));
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    if (path === "decisions/options") {
      expect(init.signal).toBe(request.signal);
      controller.abort();
      expect(init.signal?.aborted).toBe(true);
    } else {
      expect(init.signal).toBeUndefined();
    }
  });

  it.each(["decisions/run", "chat"])("scopes disconnect propagation to Decision (%s)", async (path) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}"));
    vi.stubGlobal("fetch", fetchMock);
    const { POST } = await loadRoute();
    const controller = new AbortController();
    const request = new NextRequest(`https://app.example.test/api/${path}`, {
      method: "POST", body: "{}", signal: controller.signal,
    });
    await POST(request, context(path.split("/")));
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    if (path === "decisions/run") {
      expect(init.signal).toBe(request.signal);
      controller.abort();
      expect(init.signal?.aborted).toBe(true);
    } else {
      expect(init.signal).toBeUndefined();
    }
  });

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

  it("keeps unrestricted proxy forwarding unchanged when pilot mode is off", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const { PATCH } = await loadRoute();

    const response = await PATCH(
      new NextRequest("https://app.example.test/api/sessions/session-1", {
        method: "PATCH",
        body: JSON.stringify({ toolsEnabled: true }),
      }),
      context(["sessions", "session-1"]),
    );

    expect(response.status).toBe(204);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe("http://localhost:8080/api/sessions/session-1");
  });

  it.each([
    ["models", "GET", ["models"]],
    ["session list", "GET", ["sessions"]],
    ["session create", "POST", ["sessions"]],
    ["session read", "GET", ["sessions", "session-1"]],
    ["message history", "GET", ["sessions", "session-1", "messages"]],
    ["text chat", "POST", ["chat"]],
  ])("allows the pilot route for %s", async (_name, method, path) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const route = await loadRoute("", true);
    const request = new NextRequest(`https://app.example.test/api/${path.join("/")}`, {
      method,
      ...(method === "POST" ? { body: "{}" } : {}),
    });

    const response = await route[method as "GET" | "POST"](request, context(path));

    expect(response.status).toBe(204);
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it.each([
    ["session update", "PATCH", ["sessions", "session-1"]],
    ["session delete", "DELETE", ["sessions", "session-1"]],
    ["reserved initialization session", "GET", ["sessions", "initializations"]],
    ["reserved deletion session", "GET", ["sessions", "deletions"]],
    ["initialization", "POST", ["sessions", "initializations"]],
    ["deletion workflow", "POST", ["sessions", "deletions"]],
    ["reserved initialization messages", "GET", ["sessions", "initializations", "messages"]],
    ["reserved deletion messages", "GET", ["sessions", "deletions", "messages"]],
    ["agents", "GET", ["agents"]],
    ["tools", "GET", ["tools"]],
    ["attachment capabilities", "GET", ["attachments", "capabilities"]],
    ["image options", "GET", ["images", "options"]],
    ["library", "GET", ["library", "documents"]],
    ["voice", "GET", ["voice", "live", "config"]],
    ["chat continuation", "POST", ["chat", "continue"]],
    ["workflows", "GET", ["workflows"]],
  ])("blocks the pilot-incompatible %s route without contacting the API", async (_name, method, path) => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const route = await loadRoute("", true);
    const request = new NextRequest(`https://app.example.test/api/${path.join("/")}`, {
      method,
      ...(method === "POST" || method === "PATCH" ? { body: "{}" } : {}),
    });

    const response = await route[method as "GET" | "POST" | "PATCH" | "DELETE"](request, context(path));

    expect(response.status).toBe(404);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toMatchObject({
      code: "chat_pilot_route_disabled",
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([
    ["reserved initialization session", ["sessions", "initializations"]],
    ["reserved deletion session", ["sessions", "deletions"]],
    ["reserved initialization messages", ["sessions", "initializations", "messages"]],
    ["reserved deletion messages", ["sessions", "deletions", "messages"]],
  ])("keeps unrestricted forwarding for %s when pilot mode is off", async (_name, path) => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);
    const { GET } = await loadRoute();

    const response = await GET(
      new NextRequest(`https://app.example.test/api/${path.join("/")}`),
      context(path),
    );

    expect(response.status).toBe(204);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls[0][0]).toBe(
      `http://localhost:8080/api/${path.join("/")}`,
    );
  });

  it.each([true, false])(
    "forwards synthetic Authorization unchanged on an allowed route (pilot=%s)",
    async (pilotEnabled) => {
      const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
      vi.stubGlobal("fetch", fetchMock);
      const apiBaseUrl = pilotEnabled
        ? "https://api.internal.synthetic.eastus2.azurecontainerapps.io"
        : "http://localhost:8080";
      const { GET } = await loadRoute("", pilotEnabled, apiBaseUrl);
      const authorization = "Bearer synthetic-test-only-not-a-credential";

      const response = await GET(
        new NextRequest("https://app.example.test/api/sessions/session-1", {
          headers: { authorization, "x-dev-user": "untrusted@example.test" },
        }),
        context(["sessions", "session-1"]),
      );

      expect(response.status).toBe(204);
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(fetchMock.mock.calls[0][0]).toBe(
        `${apiBaseUrl}/api/sessions/session-1`,
      );
      const headers = fetchMock.mock.calls[0][1].headers as Headers;
      expect(headers.get("authorization")).toBe(authorization);
      expect(headers.get("x-dev-user")).toBeNull();
    },
  );

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
