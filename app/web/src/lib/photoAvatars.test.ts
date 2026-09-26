import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  PHOTO_AVATAR_DEFINITE_CREATE_REFUSALS,
  PHOTO_AVATAR_POLL_BUDGET_MS,
  PHOTO_AVATAR_REVERIFYING_TEXT,
  PhotoAvatarApiError,
  createPhotoAvatar,
  deletePhotoAvatar,
  fetchPhotoAvatarPreview,
  getPhotoAvatar,
  isReverifyingPhotoAvatar,
  isUncertainCreateOutcome,
  listPhotoAvatars,
  newerPhotoAvatar,
  parseRetryAfter,
  photoAvatarDisplayStatus,
  photoAvatarErrorMessage,
  photoAvatarPollDelay,
  photoAvatarPreviewPath,
  reportPhotoAvatar,
  retryAfterPhrase,
  safeReportUrl,
  validatePhotoAvatarDraft,
  type PhotoAvatar,
  type PhotoAvatarConfig,
  type PhotoAvatarDraft,
} from "./photoAvatars";

const mocks = vi.hoisted(() => ({ apiFetch: vi.fn() }));
vi.mock("./auth", () => ({ apiFetch: mocks.apiFetch }));

const ID = "0123456789abcdef0123456789abcdef";
const OTHER = "fedcba9876543210fedcba9876543210";
const PREVIEW = `/api/photo-avatars/${ID}/preview`;

function config(overrides: Partial<PhotoAvatarConfig> = {}): PhotoAvatarConfig {
  return {
    enabled: true,
    available: true,
    reason: "available",
    canCreate: true,
    limits: {
      maxAvatars: 5,
      avatarCount: 1,
      maxCreationsPerDay: 5,
      creationsInLastDay: 1,
      nextCreationAt: null,
      promptMaxChars: 20,
      displayNameMaxChars: 60,
    },
    attributes: {
      gender: ["Male", "Female"],
      age: ["YoungAdult", "Senior"],
      ethnicity: ["Asian"],
      style: ["Realistic", "Stylized3D"],
    },
    attestation: {
      version: "fixture-attestation-7",
      statements: [
        { id: "fictional", text: "Fixture: fictional." },
        { id: "adult", text: "Fixture: adult." },
        { id: "notRealPerson", text: "Fixture: not a real person." },
      ],
    },
    disclosure: { label: "AI-generated", text: "Synthetic." },
    pricing: { currency: "USD", estimatedUsdPerAvatar: 2, known: true, priceVersion: "fixture-v1" },
    feedback: {
      reasons: ["impersonation", "minor", "sexual", "hateful", "violent", "other"],
      detailsMaxChars: 1000,
      microsoftReportUrl: "https://aka.ms/reportabuse",
    },
    ...overrides,
  };
}

const ALL_ATTESTED = { fictional: true, adult: true, notRealPerson: true };

function draft(overrides: Partial<PhotoAvatarDraft> = {}): PhotoAvatarDraft {
  return {
    displayName: "Host",
    prompt: "A friendly host",
    attributes: {},
    attested: ALL_ATTESTED,
    ...overrides,
  };
}

function avatar(overrides: Partial<PhotoAvatar> = {}): PhotoAvatar {
  return {
    id: ID,
    displayName: "Host",
    prompt: "A friendly host",
    attributes: { gender: null, age: null, ethnicity: null, style: null },
    status: "ready",
    failure: null,
    preview: { url: PREVIEW, contentType: "image/png", width: 1024, height: 1024, bytes: 3 },
    disclosure: { aiGenerated: true, label: "AI-generated" },
    cost: { currency: "USD", estimatedUsd: 2, known: true, priceVersion: "fixture-v1", basis: "per_avatar" },
    usable: true,
    reported: false,
    createdAt: "2026-09-26T12:00:00Z",
    updatedAt: "2026-09-26T12:00:00Z",
    readyAt: "2026-09-26T12:00:30Z",
    ...overrides,
  };
}

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

beforeEach(() => {
  mocks.apiFetch.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the preview guard", () => {
  it("accepts only the record's own API route", () => {
    expect(photoAvatarPreviewPath(avatar())).toBe(PREVIEW);
  });

  it.each([
    ["another origin", "https://evil.example/avatar.png"],
    ["a protocol-relative host", `//evil.example${PREVIEW}`],
    ["a provider SAS link", "https://acct.blob.core.windows.net/avatars/a.png?sv=2024&sig=abc"],
    ["an absolute URL to the right path", `http://localhost:3000${PREVIEW}`],
    ["another record's route", `/api/photo-avatars/${OTHER}/preview`],
    ["a query", `${PREVIEW}?download=1`],
    ["a fragment", `${PREVIEW}#x`],
    ["a traversal", `/api/photo-avatars/${ID}/../${OTHER}/preview`],
    ["a data URL", "data:image/png;base64,AAAA"],
    ["a blob URL", "blob:https://evil.example/1"],
    ["a script URL", "javascript:alert(1)"],
  ])("refuses %s", (_label, url) => {
    expect(photoAvatarPreviewPath(avatar({ preview: { ...avatar().preview!, url } }))).toBeNull();
  });

  it("refuses a record whose id is not the opaque shape, even with a matching path", () => {
    const upper = ID.toUpperCase();
    expect(
      photoAvatarPreviewPath({ id: upper, preview: { ...avatar().preview!, url: `/api/photo-avatars/${upper}/preview` } }),
    ).toBeNull();
    expect(photoAvatarPreviewPath(avatar({ preview: null }))).toBeNull();
  });

  it("fetches bytes only from a preview route and requires a PNG", async () => {
    mocks.apiFetch.mockResolvedValue(
      new Response(new Uint8Array([137, 80, 78, 71]), {
        headers: { "Content-Type": "image/png", "X-AI4IA-Synthetic-Media": "ai-generated" },
      }),
    );
    const blob = await fetchPhotoAvatarPreview(PREVIEW);
    expect(blob.type).toBe("image/png");
    expect(mocks.apiFetch).toHaveBeenCalledExactlyOnceWith(PREVIEW, { signal: undefined });

    mocks.apiFetch.mockClear();
    await expect(fetchPhotoAvatarPreview("https://evil.example/avatar.png")).rejects.toThrow(
      "outside the photo avatar API",
    );
    await expect(fetchPhotoAvatarPreview(`/api/photo-avatars/${ID}/preview?x=1`)).rejects.toThrow();
    expect(mocks.apiFetch).not.toHaveBeenCalled();

    mocks.apiFetch.mockResolvedValue(
      new Response("<svg/>", { headers: { "Content-Type": "image/svg+xml" } }),
    );
    await expect(fetchPhotoAvatarPreview(PREVIEW)).rejects.toMatchObject({ code: "invalid_preview" });
  });
});

describe("the attestation gate", () => {
  it("builds no request until every server statement is attested", () => {
    for (const attested of [{}, { fictional: true }, { fictional: true, adult: true }, { ...ALL_ATTESTED, adult: false }]) {
      const result = validatePhotoAvatarDraft(config(), draft({ attested }));
      expect(result.request).toBeNull();
      expect(result.issues).toContain("attestation");
    }
    // Control: the identical draft with all three attested is sendable.
    const { request, issues } = validatePhotoAvatarDraft(config(), draft());
    expect(issues).toEqual([]);
    expect(request?.attestation).toEqual({
      version: "fixture-attestation-7",
      fictional: true,
      adult: true,
      notRealPerson: true,
    });
  });

  it("sends the server's published version, not a client constant", () => {
    const next = config({ attestation: { ...config().attestation!, version: "server-v9" } });
    expect(validatePhotoAvatarDraft(next, draft()).request?.attestation.version).toBe("server-v9");
  });

  it("does not count ticks given against an older attestation version", () => {
    const stale = validatePhotoAvatarDraft(config(), draft({ attestedVersion: "fixture-attestation-6" }));
    expect(stale.request).toBeNull();
    expect(stale.issues).toContain("attestation");
    // Controls: ticks for the current version, and a caller that sends no version.
    expect(validatePhotoAvatarDraft(config(), draft({ attestedVersion: "fixture-attestation-7" })).request)
      .not.toBeNull();
    expect(validatePhotoAvatarDraft(config(), draft()).request).not.toBeNull();
  });

  it.each([
    ["an extra statement", [...config().attestation!.statements, { id: "consent", text: "New." }]],
    ["a missing statement", config().attestation!.statements.slice(0, 2)],
  ])("refuses to attest when the server lists %s", (_label, statements) => {
    const next = config({ attestation: { version: "v2", statements } });
    const result = validatePhotoAvatarDraft(next, draft({ attested: { ...ALL_ATTESTED, consent: true } }));
    expect(result.request).toBeNull();
    expect(result.issues).toContain("attestationUnrecognized");
  });

  it("requires a name and a description within the limits, trimming both", () => {
    expect(validatePhotoAvatarDraft(config(), draft({ displayName: "  " })).issues).toContain("displayName");
    expect(validatePhotoAvatarDraft(config(), draft({ prompt: "" })).issues).toContain("prompt");
    expect(validatePhotoAvatarDraft(config(), draft({ prompt: "x".repeat(21) })).issues).toContain("promptTooLong");
    // Control: exactly at the limit, with surrounding whitespace, is accepted.
    const atLimit = validatePhotoAvatarDraft(config(), draft({ displayName: " Host ", prompt: ` ${"x".repeat(20)} ` }));
    expect(atLimit.issues).toEqual([]);
    expect(atLimit.request).toMatchObject({ displayName: "Host", prompt: "x".repeat(20) });
  });

  it("sends only chosen attributes that the server offers", () => {
    const { request } = validatePhotoAvatarDraft(
      config(),
      draft({ attributes: { style: "Stylized3D", age: "", gender: undefined } }),
    );
    expect(request).toEqual({
      displayName: "Host",
      prompt: "A friendly host",
      style: "Stylized3D",
      attestation: expect.any(Object),
    });
    const stale = validatePhotoAvatarDraft(config(), draft({ attributes: { style: "Watercolor" } }));
    expect(stale.request).toBeNull();
    expect(stale.issues).toContain("attribute");
  });

  it("builds nothing from a config without limits or options", () => {
    const off: PhotoAvatarConfig = {
      ...config(), enabled: false, limits: null, attributes: null, attestation: null,
    };
    expect(validatePhotoAvatarDraft(off, draft())).toEqual({ request: null, issues: ["config"] });
  });
});

describe("HTTP helpers", () => {
  it("posts the create body as JSON", async () => {
    mocks.apiFetch.mockResolvedValue(json(avatar({ status: "generating" }), 202));
    const request = validatePhotoAvatarDraft(config(), draft()).request!;
    await expect(createPhotoAvatar(request)).resolves.toMatchObject({ status: "generating" });
    const [path, init] = mocks.apiFetch.mock.calls[0];
    expect(path).toBe("/api/photo-avatars");
    expect(init).toMatchObject({ method: "POST", headers: { "Content-Type": "application/json" } });
    expect(JSON.parse(init.body)).toEqual(request);
  });

  it("reads code, reason, correlation id and Retry-After from a refusal", async () => {
    mocks.apiFetch.mockResolvedValue(
      json(
        { detail: "Daily creation limit reached.", code: "daily_creation_limit", correlation_id: "corr-1" },
        429,
        { "Retry-After": "321" },
      ),
    );
    const error = await createPhotoAvatar(validatePhotoAvatarDraft(config(), draft()).request!).catch((e) => e);
    expect(error).toBeInstanceOf(PhotoAvatarApiError);
    expect(error).toMatchObject({
      status: 429, code: "daily_creation_limit", retryAfterSeconds: 321, correlationId: "corr-1", reason: null,
    });

    mocks.apiFetch.mockResolvedValue(
      json({ detail: "Unavailable.", code: "photo_avatars_unavailable", reason: "capability_unavailable" }, 503),
    );
    await expect(listPhotoAvatars()).rejects.toMatchObject({ reason: "capability_unavailable" });
    mocks.apiFetch.mockResolvedValue(json({ detail: "Unavailable.", reason: "not-a-reason" }, 503));
    await expect(listPhotoAvatars()).rejects.toMatchObject({ reason: null, code: null });
  });

  it("treats a malformed list as unavailable, never as an empty gallery", async () => {
    mocks.apiFetch.mockResolvedValue(json({ items: [] }));
    await expect(listPhotoAvatars()).rejects.toMatchObject({ code: "invalid_response" });
    // Control: a well-formed list keeps its valid records and drops unusable ones.
    mocks.apiFetch.mockResolvedValue(json({ avatars: [avatar(), { ...avatar(), id: "../x" }] }));
    await expect(listPhotoAvatars()).resolves.toEqual([avatar()]);
  });

  it("never builds a request path from an unvalidated id", async () => {
    await expect(getPhotoAvatar("../config")).rejects.toThrow("Invalid photo avatar id");
    await expect(deletePhotoAvatar(ID.toUpperCase())).rejects.toThrow("Invalid photo avatar id");
    await expect(reportPhotoAvatar("x", { reason: "other" })).rejects.toThrow("Invalid photo avatar id");
    expect(mocks.apiFetch).not.toHaveBeenCalled();
    mocks.apiFetch.mockResolvedValue(new Response(null, { status: 204 }));
    await deletePhotoAvatar(ID);
    expect(mocks.apiFetch).toHaveBeenCalledWith(`/api/photo-avatars/${ID}`, { method: "DELETE" });
  });

  it("posts a report to the record's reports route", async () => {
    mocks.apiFetch.mockResolvedValue(
      json({ id: "r1", avatarId: ID, reason: "minor", createdAt: "2026-09-26T12:00:00Z" }, 202),
    );
    await reportPhotoAvatar(ID, { reason: "minor", details: "Looks young" });
    const [path, init] = mocks.apiFetch.mock.calls[0];
    expect(path).toBe(`/api/photo-avatars/${ID}/reports`);
    expect(JSON.parse(init.body)).toEqual({ reason: "minor", details: "Looks young" });
  });
});

describe("Retry-After and messages", () => {
  it("treats only definite refusals as a create that surely did not happen", () => {
    const refusal = (status: number, code: string | null) =>
      new PhotoAvatarApiError({ status, code, detail: "Refused." });
    // Unknown: nothing proves the avatar was not created (and billed).
    for (const error of [
      new TypeError("Failed to fetch"),
      new SyntaxError("Unexpected end of JSON input"),
      refusal(502, null),
      refusal(503, "service_unavailable"),
      refusal(500, "internal_error"),
      refusal(502, "bad_gateway"),
      refusal(504, "gateway_timeout"),
      refusal(599, "server_error"),
      refusal(503, "a_future_code"),
      refusal(302, null),
    ]) {
      expect(isUncertainCreateOutcome(error), String(error)).toBe(true);
    }
    // Definite: refused before anything reached the provider.
    for (const code of PHOTO_AVATAR_DEFINITE_CREATE_REFUSALS) {
      expect(isUncertainCreateOutcome(refusal(503, code)), code).toBe(false);
    }
    for (const [status, code] of [
      [409, "avatar_limit_reached"], [429, "daily_creation_limit"], [422, "validation_error"],
      [403, "policy_denied"], [404, "photo_avatars_disabled"], [429, null],
    ] as const) {
      expect(isUncertainCreateOutcome(refusal(status, code)), `${status} ${code}`).toBe(false);
    }
  });

  it("parses delta-seconds and HTTP dates, bounding both", () => {
    const now = Date.parse("2026-09-26T12:00:00Z");
    expect(parseRetryAfter("7", now)).toBe(7);
    expect(parseRetryAfter(" 0 ", now)).toBe(0);
    expect(parseRetryAfter("Sat, 26 Sep 2026 12:01:30 GMT", now)).toBe(90);
    expect(parseRetryAfter("Sat, 26 Sep 2026 11:00:00 GMT", now)).toBe(0);
    expect(parseRetryAfter("99999999999", now)).toBe(7 * 24 * 60 * 60);
    for (const bad of [null, undefined, "", "soon", "-5", "1.5"]) {
      expect(parseRetryAfter(bad, now)).toBeNull();
    }
  });

  it("names the wait in a refusal that carries Retry-After", () => {
    const confirming = new PhotoAvatarApiError({
      status: 409, code: "avatar_confirming", detail: "Confirming.", retryAfterSeconds: 7,
    });
    expect(photoAvatarErrorMessage(confirming)).toBe(
      "This avatar's creation is still being confirmed. You can delete it in 7 seconds.",
    );
    const daily = new PhotoAvatarApiError({
      status: 429, code: "daily_creation_limit", detail: "Limit.", retryAfterSeconds: 600,
    });
    expect(photoAvatarErrorMessage(daily)).toMatch(/create another in about 10 minutes\.$/);
    const unknownCode = new PhotoAvatarApiError({
      status: 429, code: "rate_limited", detail: "Daily cost budget reached.", retryAfterSeconds: 30,
    });
    expect(photoAvatarErrorMessage(unknownCode)).toBe("Daily cost budget reached. Try again in 30 seconds.");
    expect(photoAvatarErrorMessage(new TypeError("Failed to fetch"))).toMatch(/didn't complete/);
    expect(retryAfterPhrase(1)).toBe("in 1 second");
    expect(retryAfterPhrase(7200, Date.parse("2026-09-26T12:00:00Z"))).toMatch(/^after /);
  });

  it("maps availability reasons, including Limited Access, to explanations", () => {
    const refusal = (reason: PhotoAvatarConfig["reason"]) =>
      photoAvatarErrorMessage(new PhotoAvatarApiError({
        status: 503, code: "photo_avatars_unavailable", detail: "Unavailable.", reason,
      }));
    expect(refusal("capability_unavailable")).toMatch(/Limited Access approval/);
    expect(refusal("policy_denied")).toMatch(/access policy/);
    expect(refusal("storage_unavailable")).toMatch(/storage/);
  });

  it("links only to an https report form", () => {
    expect(safeReportUrl("https://aka.ms/reportabuse")).toBe("https://aka.ms/reportabuse");
    for (const bad of ["http://aka.ms/reportabuse", "javascript:alert(1)", "https://user:pw@example.com", "/relative", ""]) {
      expect(safeReportUrl(bad)).toBeNull();
    }
  });
});

describe("polling and merging", () => {
  it("treats only a ready avatar with the flag as re-verifying", () => {
    expect(isReverifyingPhotoAvatar(avatar({ needsReverification: true, usable: false }))).toBe(true);
    // Controls: the same record without the flag, or with it on a non-ready status.
    expect(isReverifyingPhotoAvatar(avatar({ needsReverification: false }))).toBe(false);
    expect(isReverifyingPhotoAvatar(avatar({ needsReverification: undefined }))).toBe(false);
    expect(isReverifyingPhotoAvatar(avatar({ status: "generating", needsReverification: true }))).toBe(false);
  });

  it("names the displayed status for re-verification and for a failure after ready", () => {
    expect(photoAvatarDisplayStatus(avatar({ needsReverification: true }))).toBe(PHOTO_AVATAR_REVERIFYING_TEXT);
    expect(photoAvatarDisplayStatus(avatar())).toBe("Ready");
    expect(photoAvatarDisplayStatus(avatar({ status: "failed" }))).toBe("No longer available");
    expect(photoAvatarDisplayStatus(avatar({ status: "failed", readyAt: null }))).toBe("Couldn't create");
    expect(photoAvatarDisplayStatus(avatar({ status: "generating", readyAt: null }))).toBe("Generating…");
  });

  it("backs off to a ceiling within a bounded budget", () => {
    const delays = Array.from({ length: 12 }, (_value, attempt) => photoAvatarPollDelay(attempt));
    expect(delays.slice(0, 3)).toEqual([2_000, 3_000, 5_000]);
    expect(Math.max(...delays)).toBe(15_000);
    expect(delays).toEqual([...delays].sort((a, b) => a - b));
    expect(PHOTO_AVATAR_POLL_BUDGET_MS).toBe(180_000);
  });

  it("keeps the newer copy of a record", () => {
    const ready = avatar({ status: "ready", updatedAt: "2026-09-26T12:01:00Z" });
    const stale = avatar({ status: "generating", updatedAt: "2026-09-26T12:00:10Z" });
    expect(newerPhotoAvatar(ready, stale)).toBe(ready);
    expect(newerPhotoAvatar(stale, ready)).toBe(ready);
  });
});
