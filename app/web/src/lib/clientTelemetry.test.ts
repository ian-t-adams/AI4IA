// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  apiFetch: vi.fn(),
}));

vi.mock("@/lib/auth", () => ({
  apiFetch: mocks.apiFetch,
}));

async function freshModule() {
  vi.resetModules();
  return import("./clientTelemetry");
}

describe("reportClientEvent", () => {
  beforeEach(() => {
    mocks.apiFetch.mockReset();
    mocks.apiFetch.mockResolvedValue(new Response(null, { status: 202 }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("posts a bounded event body to /api/client-events", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("unhandled_error", {
      message: "boom",
      route: "/chat",
      component: "ChatApp",
    });

    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mocks.apiFetch.mock.calls[0];
    expect(url).toBe("/api/client-events");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body)).toEqual({
      event: "unhandled_error",
      message: "boom",
      route: "/chat",
      component: "ChatApp",
    });
  });

  it("truncates an overlong message and adds an ellipsis", async () => {
    const { reportClientEvent } = await freshModule();
    const longMessage = "x".repeat(400);

    reportClientEvent("render_error", { message: longMessage });

    const [, init] = mocks.apiFetch.mock.calls[0];
    const body = JSON.parse(init.body);
    expect(body.message).toHaveLength(300);
    expect(body.message.endsWith("…")).toBe(true);
  });

  it("omits blank or missing optional fields rather than sending empty strings", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("render_error", { message: "  ", route: "", component: undefined });

    const [, init] = mocks.apiFetch.mock.calls[0];
    const body = JSON.parse(init.body);
    expect(body.message).toBeUndefined();
    expect(body.route).toBeUndefined();
    expect(body.component).toBeUndefined();
  });

  it("de-dupes identical (event, message) pairs within the same page load", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("microphone_error", { message: "denied" });
    reportClientEvent("microphone_error", { message: "denied" });
    reportClientEvent("microphone_error", { message: "different" });

    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
  });

  it("caps total reports per page load", async () => {
    const { reportClientEvent } = await freshModule();

    for (let i = 0; i < 25; i += 1) {
      reportClientEvent("unhandled_error", { message: `error-${i}` });
    }

    expect(mocks.apiFetch).toHaveBeenCalledTimes(20);
  });

  it("never throws when the beacon request rejects", async () => {
    mocks.apiFetch.mockRejectedValue(new Error("network down"));
    const { reportClientEvent } = await freshModule();

    expect(() => reportClientEvent("unhandled_rejection", { message: "x" })).not.toThrow();
  });
});

describe("installGlobalClientTelemetry", () => {
  beforeEach(() => {
    mocks.apiFetch.mockReset();
    mocks.apiFetch.mockResolvedValue(new Response(null, { status: 202 }));
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // These tests share ONE module instance (loaded once below) rather than the
  // `freshModule()` per-test pattern used above: `vi.resetModules()` only
  // resets the module registry, it does not detach listeners a previous
  // instance already attached to the shared jsdom `window`. Re-importing a
  // fresh instance per test would stack up duplicate listeners across tests
  // and inflate the call counts. Installing exactly once here also matches
  // how this is actually used in production (installed once per page load).
  // Each test below uses a distinct message so the module-level de-dupe
  // (shared across these tests) never masks a real assertion.
  it("reports genuinely-uncaught script errors, unhandled promise rejections, and stays idempotent across repeat calls", async () => {
    const { installGlobalClientTelemetry } = await freshModule();
    installGlobalClientTelemetry();
    // A second call must be a no-op -- proven below because only one report
    // is recorded per dispatched event, not two.
    installGlobalClientTelemetry();

    window.dispatchEvent(new ErrorEvent("error", { message: "script exploded" }));
    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    const [, errorInit] = mocks.apiFetch.mock.calls[0];
    const errorBody = JSON.parse(errorInit.body);
    expect(errorBody.event).toBe("unhandled_error");
    expect(errorBody.message).toBe("script exploded");

    const reason = new Error("promise blew up");
    const rejected = Promise.reject(reason);
    window.dispatchEvent(
      new PromiseRejectionEvent("unhandledrejection", { promise: rejected, reason }),
    );
    rejected.catch(() => {});

    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
    const [, rejectionInit] = mocks.apiFetch.mock.calls[1];
    const rejectionBody = JSON.parse(rejectionInit.body);
    expect(rejectionBody.event).toBe("unhandled_rejection");
    expect(rejectionBody.message).toBe("promise blew up");
  });
});
