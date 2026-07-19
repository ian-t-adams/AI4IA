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
      code: "unknown",
      message: "boom",
      route: "/chat",
      component: "ChatApp",
    });
  });

  it("passes through a known error code but normalizes an unrecognized one to 'unknown'", async () => {
    const { reportClientEvent } = await freshModule();

    reportClientEvent("unhandled_error", { message: "a", code: "TypeError" });
    reportClientEvent("unhandled_error", { message: "b", code: "made_up_code" });
    reportClientEvent("unhandled_error", { message: "c", code: undefined });

    const bodies = mocks.apiFetch.mock.calls.map(([, init]) => JSON.parse(init.body));
    expect(bodies[0].code).toBe("TypeError");
    expect(bodies[1].code).toBe("unknown");
    expect(bodies[2].code).toBe("unknown");
  });

  it("truncates an overlong message and adds an ellipsis", async () => {
    const { reportClientEvent } = await freshModule();
    // Repeated short words (not one long run) so this exercises the length
    // cap without also tripping the long-opaque-token redaction below.
    const longMessage = "failed ".repeat(60);

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
    expect(body.code).toBe("unknown");
  });

  describe("redaction of hostile message content", () => {
    // These directly cover HIGH-2: raw Error.message/rejection text may
    // contain credentials, URLs/query tokens, PII, or response bodies, so
    // this must never reach Application Insights unredacted. Fixtures below
    // are intentionally low-entropy, repeated-character placeholders (never
    // realistic-looking secrets) so they read clearly as synthetic test data.
    // The backend (client_events.py's _sanitize) independently re-applies
    // the same patterns -- see the matching test there.
    const longOpaqueRun = "x".repeat(30);
    const shortKeyValue = "y".repeat(20);
    const email = "jane.doe@example.test";
    const guid = "11111111-2222-3333-4444-555555555555";

    it("redacts an authorization-style key/value pair", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", {
        message: `Authorization: ${shortKeyValue} rejected`,
      });

      const [, init] = mocks.apiFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.message).not.toContain(shortKeyValue);
      expect(body.message).toBe("Authorization=[redacted] rejected");
    });

    // Regression coverage for the HIGH finding: the auth-header pattern used
    // to match only the scheme word ("Basic"/"Bearer") when it preceded a
    // credential, because `[^\s"&,]+` stops at the first whitespace -- so
    // "Authorization: Basic <credential>" redacted "Basic" but left the
    // credential itself sitting in plain text right after it. The fix wraps
    // an optional scheme-word group around the value so scheme + credential
    // are consumed as a single match. Covered for both schemes, and both a
    // short and a long (24+ char) credential since the long case would also
    // be eligible for the separate generic long-opaque-token catch-all --
    // proving the auth-header pattern (which runs first) fully owns it.
    const basicShortCred = "b".repeat(12);
    const basicLongCred = "c".repeat(40);
    const bearerShortCred = "d".repeat(12);
    const bearerLongCred = "e".repeat(40);

    it("redacts 'Authorization: Basic <credential>' for a short credential", async () => {
      const { reportClientEvent } = await freshModule();

      reportClientEvent("unhandled_error", {
        message: `Authorization: Basic ${basicShortCred} failed`,
      });
      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(basicShortCred);
      expect(body.message).not.toContain("Basic");
      expect(body.message).toBe("Authorization=[redacted] failed");
    });

    it("redacts 'Authorization: Basic <credential>' for a long (24+ char) credential", async () => {
      const { reportClientEvent } = await freshModule();

      reportClientEvent("unhandled_error", {
        message: `Authorization: Basic ${basicLongCred} failed`,
      });
      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(basicLongCred);
      expect(body.message).not.toContain("Basic");
      expect(body.message).toBe("Authorization=[redacted] failed");
    });

    it("redacts 'Authorization: Bearer <credential>' for a short credential", async () => {
      const { reportClientEvent } = await freshModule();

      reportClientEvent("unhandled_error", {
        message: `Authorization: Bearer ${bearerShortCred} failed`,
      });
      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(bearerShortCred);
      expect(body.message).not.toContain("Bearer");
      expect(body.message).toBe("Authorization=[redacted] failed");
    });

    it("redacts 'Authorization: Bearer <credential>' for a long (24+ char) credential", async () => {
      const { reportClientEvent } = await freshModule();

      reportClientEvent("unhandled_error", {
        message: `Authorization: Bearer ${bearerLongCred} failed`,
      });
      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(bearerLongCred);
      expect(body.message).not.toContain("Bearer");
      expect(body.message).toBe("Authorization=[redacted] failed");
    });

    // Regression coverage for a follow-up acceptance round: the fix above
    // still missed a JSON-serialized key (`{"Authorization":"Basic <cred>"}`,
    // where a closing key-quote sits between the label and the colon), a
    // quoted credential paired with an unquoted scheme word
    // (`Authorization: Basic "<cred>"`), and standalone scheme+credential
    // with no "Authorization"/"token" label at all (bare `Bearer <cred>`,
    // `Basic: <cred>`, or nested in surrounding punctuation/quotes). Each is
    // tested for both schemes and both credential lengths.
    const jsonCred = "f".repeat(16);
    const quotedCred = "g".repeat(16);
    const standaloneBasicCred = "h".repeat(12);
    const standaloneBearerCred = "i".repeat(40);

    it("redacts a JSON-serialized Authorization key/value pair", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", {
        message: `{"Authorization":"Basic ${jsonCred}"} rejected`,
      });

      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(jsonCred);
      expect(body.message).not.toContain("Basic");
      expect(body.message).toContain("Authorization=[redacted]");
    });

    it("redacts a quoted credential following an unquoted scheme word", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", {
        message: `Authorization: Basic "${quotedCred}" failed`,
      });

      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(quotedCred);
      expect(body.message).not.toContain("Basic");
      expect(body.message).toBe("Authorization=[redacted] failed");
    });

    it("redacts a standalone 'Basic <credential>' with no Authorization label", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", {
        message: `Basic: ${standaloneBasicCred} was rejected`,
      });

      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(standaloneBasicCred);
      expect(body.message).not.toContain("Basic:");
    });

    it("redacts a standalone 'Bearer <credential>' embedded in punctuation, with no label", async () => {
      const { reportClientEvent } = await freshModule();
      const scheme = "Bearer";
      reportClientEvent("unhandled_error", {
        message: `request failed (${scheme} ${standaloneBearerCred})`,
      });

      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).not.toContain(standaloneBearerCred);
      // Unlike the label-prefixed pattern (which drops the scheme word
      // entirely), the standalone pattern's replacement preserves the
      // matched scheme word -- only the credential value after it is
      // opaque -- so the correct assertion is that the scheme word is
      // redacted together with its credential, not that it vanishes. The
      // value class also doesn't exclude ")", so (by accepted design) a
      // single trailing paren is absorbed into the match and dropped --
      // no credential material is exposed either way.
      expect(body.message).toBe(`request failed (${scheme}=[redacted]`);
    });

    it("leaves a trailing scheme word alone when nothing follows it", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", { message: "I love Bearer." });

      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      // "Bearer" here has no colon/equals and no following whitespace+token
      // (just a trailing period with nothing after it), so there is no
      // credential-shaped value for the standalone pattern to consume.
      expect(body.message).toBe("I love Bearer.");
    });

    it("may over-redact prose pairing a scheme word with a following word (accepted tradeoff)", async () => {
      // Documents a deliberate, reviewed tradeoff: catching a genuine
      // standalone credential with no colon/label (e.g. "Bearer <token>"
      // floating mid-sentence) requires treating "scheme word + whitespace +
      // token" as a signal on its own, which also matches ordinary prose
      // like this. For a security-redaction sanitizer, over-redacting rare
      // English phrasing is judged far cheaper than under-redacting a real
      // credential, so this is intentional rather than a bug to fix.
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", { message: "Basic training completed." });

      const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
      expect(body.message).toBe("Basic=[redacted] completed.");
    });

    it("redacts an entire URL, including its query string", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", {
        message: "Failed to fetch https://example.test/path?a=1&b=2",
      });

      const [, init] = mocks.apiFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.message).not.toContain("example.test");
      expect(body.message).toBe("Failed to fetch [redacted-url]");
    });

    it("redacts an email address but keeps surrounding text useful", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", { message: `User ${email} not found` });

      const [, init] = mocks.apiFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.message).toBe("User [redacted-email] not found");
    });

    it("redacts a GUID-shaped session/request id", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", { message: `session ${guid} crashed` });

      const [, init] = mocks.apiFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.message).not.toContain(guid);
      expect(body.message).toBe("session [redacted-id] crashed");
    });

    it("redacts a generic long opaque token with no other recognizable shape", async () => {
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", { message: `token ${longOpaqueRun} invalid` });

      const [, init] = mocks.apiFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.message).not.toContain(longOpaqueRun);
      expect(body.message).toBe("token [redacted-token] invalid");
    });

    it("does not let authenticated user correlation preserve message content", async () => {
      // The frontend never sends a userId itself (the backend derives it
      // from the authenticated request), so proving the message is scrubbed
      // here is what keeps the backend's later userId-tagged record from
      // preserving this content against that user.
      const { reportClientEvent } = await freshModule();
      reportClientEvent("unhandled_error", {
        message: `for ${email}, token=${longOpaqueRun}`,
      });

      const [, init] = mocks.apiFetch.mock.calls[0];
      const body = JSON.parse(init.body);
      expect(body.message).not.toContain(email);
      expect(body.message).not.toContain(longOpaqueRun);
    });

    describe("URL-encoded, nested, and malformed-quote bypass resistance", () => {
      // Regression coverage for a follow-up acceptance round: naive
      // delimiter-matching is defeated by percent-encoding the delimiter
      // away, by a credential value that is itself a small nested JSON
      // object, or by an unterminated quote -- each previously let the real
      // credential straight through. `sanitize` now runs a bounded
      // decode-then-redact loop (MAX_SANITIZE_PASSES) specifically to close
      // these gaps; the backend (client_events.py's `_sanitize`) mirrors
      // this exactly -- see the matching tests there.
      const cred = "z".repeat(20);

      it("redacts a standalone scheme+credential joined by a URL-encoded space", async () => {
        const { reportClientEvent } = await freshModule();
        reportClientEvent("unhandled_error", { message: `Basic%20${cred} rejected` });

        const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
        expect(body.message).not.toContain(cred);
        expect(body.message).not.toContain("%20");
        expect(body.message).toBe("Basic=[redacted] rejected");
      });

      it("redacts a labeled key/value pair joined by a URL-encoded equals sign", async () => {
        const { reportClientEvent } = await freshModule();
        reportClientEvent("unhandled_error", { message: `token%3D${cred} invalid` });

        const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
        expect(body.message).not.toContain(cred);
        expect(body.message).not.toContain("%3D");
        expect(body.message).toBe("token=[redacted] invalid");
      });

      it("redacts a double-percent-encoded delimiter that only resolves after two decode passes", async () => {
        // "%2520" decodes to "%20" on the first pass, which itself only
        // becomes a literal space on a second pass -- proving the loop
        // actually iterates rather than decoding once and giving up.
        const { reportClientEvent } = await freshModule();
        reportClientEvent("unhandled_error", { message: `Basic%2520${cred} rejected` });

        const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
        expect(body.message).not.toContain(cred);
        expect(body.message).not.toContain("%20");
        expect(body.message).not.toContain("%2520");
        expect(body.message).toBe("Basic=[redacted] rejected");
      });

      it("redacts a credential value that is itself a nested JSON object", async () => {
        const { reportClientEvent } = await freshModule();
        reportClientEvent("unhandled_error", {
          message: `Authorization: {"scheme":"Basic","credential":"${cred}"} rejected`,
        });

        const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
        expect(body.message).not.toContain(cred);
        expect(body.message).toBe("Authorization=[redacted] rejected");
      });

      it("redacts an unterminated (missing closing quote) credential value", async () => {
        const { reportClientEvent } = await freshModule();
        reportClientEvent("unhandled_error", { message: `Authorization: "${cred}` });

        const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
        expect(body.message).not.toContain(cred);
        expect(body.message).toBe("Authorization=[redacted]");
      });

      it("never leaves a decoded-but-unredacted intermediate in the reported message", async () => {
        // A message combining several bypass shapes at once; the assertion
        // is strong -- no raw credential material and no residual
        // percent-encoding survive, which is only possible if every pass's
        // decode step is followed by a redaction step before anything is
        // returned.
        const { reportClientEvent } = await freshModule();
        reportClientEvent("unhandled_error", {
          message: `Basic%20${cred} and token%3D${cred}2 both failed`,
        });

        const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
        expect(body.message).not.toContain(cred);
        expect(body.message).not.toContain("%20");
        expect(body.message).not.toContain("%3D");
      });

      it("does not corrupt an already-redacted quoted credential on a later loop pass", async () => {
        // Regression for a bug introduced while adding the iterative loop
        // above: once the standalone pattern redacts `("Bearer <cred>")`
        // down to `("Bearer=[redacted]")` on pass one, pass two must not
        // let the label-prefixed pattern re-match its own `Bearer=[redacted]`
        // output and swallow the legitimately-preserved leading quote (it
        // optionally consumes one, for the unrelated JSON-key-quoting case).
        // Uses "Bearer" specifically -- the one word that is both a
        // standalone scheme AND a label -- since that dual role is exactly
        // what let the label-prefixed pattern misfire on the second pass.
        const { reportClientEvent } = await freshModule();
        reportClientEvent("unhandled_error", { message: `request failed ("Bearer ${cred}")` });

        const body = JSON.parse(mocks.apiFetch.mock.calls[0][1].body);
        expect(body.message).toBe('request failed ("Bearer=[redacted]")');
      });
    });
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
