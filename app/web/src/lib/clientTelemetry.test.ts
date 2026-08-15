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

  it("posts a content-free, bounded event body to /api/client-events", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("voice_playback_rebuffer", {
      severity: "warning",
    });

    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mocks.apiFetch.mock.calls[0];
    expect(url).toBe("/api/client-events");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    const body = JSON.parse(init.body);
    expect(body).toEqual({
      event: "voice_playback_rebuffer",
      code: "unknown",
      severity: "warning",
      hasDigest: false,
    });
    // Exactly these four keys, always -- there is no fifth field a caller
    // (or a compromised copy of this module) could smuggle free text into.
    expect(Object.keys(body).sort()).toEqual(["code", "event", "hasDigest", "severity"]);
  });

  it("defaults severity to 'error' and hasDigest to false when omitted", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("window_error", { code: "TypeError" });

    const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
    expect(body.severity).toBe("error");
    expect(body.hasDigest).toBe(false);
  });

  it("passes through a known error code but normalizes an unrecognized one to 'unknown'", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("window_error", { code: "TypeError" });
    reportClientEvent("window_error", { code: "made_up_code" });
    reportClientEvent("render_error", { code: undefined });

    const bodies = mocks.apiFetch.mock.calls.map(([, init]) => JSON.parse(init.body));
    expect(bodies[0].code).toBe("TypeError");
    expect(bodies[1].code).toBe("unknown");
    expect(bodies[2].code).toBe("unknown");
  });

  describe("hostile `code` values always normalize to 'unknown'", () => {
    // The old free-text message/route/component fields are gone entirely, so
    // there is no longer any string field wide enough to carry a credential,
    // URL, or PII. `code` is the only string field left, and it is a closed
    // allowlist (KNOWN_CODES): anything that is not an exact match becomes
    // "unknown" regardless of encoding, nesting, or control characters. This
    // is what "arbitrary strings never enter logs" means under the new
    // design -- guaranteed by construction, not by pattern-matching a
    // blocklist that a previous version of this file kept losing to.
    // A repeated-character placeholder standing in for an opaque bearer/
    // basic-auth token below: near-zero Shannon entropy, so these fixtures
    // read as obvious test data (never a realistic-looking secret) to both
    // humans and entropy-based secret scanners. Only the *shape* (scheme +
    // opaque token, in various encodings/nestings) matters for this test.
    const placeholderToken = "z".repeat(12);
    const hostileCodes = [
      `Authorization: Basic ${placeholderToken}`,
      `Authorization%3A%20Basic%20${placeholderToken}`,
      `Basic%2520${placeholderToken}%253D`, // double percent-encoded
      `{"Authorization":"Basic ${placeholderToken}"}`,
      `Authorization: Basic "${placeholderToken}`, // unterminated quote
      "TypeError\u0000\u0001 with control chars",
      "TypeError'; DROP TABLE users;--",
      "a".repeat(5000),
      "https://example.test/reset?token=abc123",
      "user@example.test leaked here",
      "Bearer " + "z".repeat(40),
    ];

    it.each(hostileCodes)("normalizes %j to 'unknown'", async (hostileCode) => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("window_error", { code: hostileCode });
      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.code).toBe("unknown");
    });
  });

  it("de-dupes identical (event, code, severity, hasDigest) tuples within the same page load", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("microphone_error", { code: "NotAllowedError" });
    reportClientEvent("microphone_error", { code: "NotAllowedError" });
    reportClientEvent("microphone_error", { code: "NotFoundError" });

    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
  });

  it("caps total reports per page load", async () => {
    const { reportClientEvent, KNOWN_EVENT_CODES } = await freshModule();
    const severities = ["error", "warning", "info"] as const;

    // Each iteration uses a distinct (code, severity) pair (guaranteed by
    // construction below) so the cap -- not the de-dupe above -- is what
    // stops reporting after 20.
    for (let i = 0; i < 25; i += 1) {
      reportClientEvent("window_error", {
        code: KNOWN_EVENT_CODES[i % KNOWN_EVENT_CODES.length],
        severity: severities[Math.floor(i / KNOWN_EVENT_CODES.length) % severities.length],
      });
    }

    expect(mocks.apiFetch).toHaveBeenCalledTimes(20);
  });

  it("never throws when the beacon request rejects", async () => {
    mocks.apiFetch.mockRejectedValue(new Error("network down"));
    const { reportClientEvent } = await freshModule();

    expect(() => reportClientEvent("unhandled_rejection", { code: "TypeError" })).not.toThrow();
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

  // This single test shares ONE module instance (loaded once below) rather
  // than the `freshModule()` per-test pattern used above: `vi.resetModules()`
  // only resets the module registry, it does not detach listeners a previous
  // instance already attached to the shared jsdom `window`. Re-importing a
  // fresh instance per test would stack up duplicate listeners across tests
  // and inflate the call counts. Installing exactly once here also matches
  // how this is actually used in production (installed once per page load).
  it("reports window errors and unhandled rejections with a content-free body, staying idempotent across repeat install calls", async () => {
    const { installGlobalClientTelemetry } = await freshModule();
    installGlobalClientTelemetry();
    // A second call must be a no-op -- proven below because only one report
    // is recorded per dispatched event, not two.
    installGlobalClientTelemetry();

    // This stands in for a real error whose message/stack might carry a
    // credential, URL, or other sensitive text -- the assertions below prove
    // none of it can reach the reported body no matter what the underlying
    // error says, because there is no field left that could carry it. The
    // repeated-character token is a low-entropy placeholder, not a
    // realistic-looking secret.
    const hostileText =
      `Authorization: Basic ${"z".repeat(12)} while loading https://internal.example.test/admin?token=abc123`;

    window.dispatchEvent(
      new ErrorEvent("error", { error: new TypeError(hostileText), message: hostileText }),
    );
    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    const errorBody = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
    expect(errorBody).toEqual({
      event: "window_error",
      code: "TypeError",
      severity: "error",
      hasDigest: false,
    });
    expect(JSON.stringify(errorBody)).not.toContain("Authorization");
    expect(JSON.stringify(errorBody)).not.toContain("z".repeat(12));
    expect(JSON.stringify(errorBody)).not.toContain("token=abc123");

    const reason = new RangeError(hostileText);
    const rejected = Promise.reject(reason);
    window.dispatchEvent(
      new PromiseRejectionEvent("unhandledrejection", { promise: rejected, reason }),
    );
    rejected.catch(() => {});

    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
    const rejectionBody = JSON.parse(mocks.apiFetch.mock.calls[1][1].body);
    expect(rejectionBody).toEqual({
      event: "unhandled_rejection",
      code: "RangeError",
      severity: "error",
      hasDigest: false,
    });
    expect(JSON.stringify(rejectionBody)).not.toContain("Authorization");

    // A rejection whose reason is a plain string (not an Error) is also
    // fully covered: its code is a fixed literal, never the string itself.
    const stringRejected = Promise.reject(hostileText);
    window.dispatchEvent(
      new PromiseRejectionEvent("unhandledrejection", {
        promise: stringRejected,
        reason: hostileText,
      }),
    );
    stringRejected.catch(() => {});

    expect(mocks.apiFetch).toHaveBeenCalledTimes(3);
    const stringRejectionBody = JSON.parse(mocks.apiFetch.mock.calls[2][1].body);
    expect(stringRejectionBody.code).toBe("string_rejection");
    expect(JSON.stringify(stringRejectionBody)).not.toContain("Authorization");
  });
});
