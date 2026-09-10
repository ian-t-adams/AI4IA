import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, updateAgent, updateWorkflow } from "./api";
import { apiFetch } from "./auth";
import * as publishing from "./publishing";
import { publicationAgent, publicationHead, publicationRef, publicationReview } from "./publishingTestFixtures";

vi.mock("./auth", () => ({ apiFetch: vi.fn() }));
const fetch = vi.mocked(apiFetch);
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const submissionState = {
  ownerId: publicationAgent.userId, sourceIncarnation: publicationAgent.incarnation ?? null,
  previousHead: publicationHead,
};
const submission: publishing.PublicationSubmit = {
  expectedRevision: 7, audience: { visibility: "public", acl: [], groupAcl: [] },
  modelIds: ["fixture-text"], modes: ["chat"], reviewConsent: true,
  operatorReviewConsent: false, skillMode: "versioned",
};
const submitted = {
  ...publicationHead, revision: 12, versionCount: 3, pendingVersion: 3,
  pendingSource: { ...publicationRef, version: 3 },
};
const activated: publishing.OwnerPublicationState = {
  ...publicationHead, revision: 12, activeVersion: 2, activeSource: publicationRef, visibility: "public",
  pendingVersion: null, pendingSource: null, pendingDraftRevision: null,
};
const withdrawn: publishing.OwnerPublicationState = {
  ...activated, revision: 13, activeVersion: null, activeSource: null, visibility: "private",
};

beforeEach(() => { fetch.mockReset(); });

describe("publication client", () => {
  it.each([false, true])("requires strict server enablement (%s)", async (enabled) => {
    fetch.mockResolvedValue(json({ enabled, actions: ["submit", "review", "consume"], operatorReviewAvailable: true }));
    const signal = new AbortController().signal;
    expect(await publishing.getPublicationCapabilities(signal)).toEqual({
      enabled, actions: enabled ? ["submit", "review", "consume"] : [], operatorReviewAvailable: enabled,
    });
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/publications/capabilities", { cache: "no-store", signal });
  });

  it.each([
    { enabled: "true", actions: ["submit"], operatorReviewAvailable: false },
    { enabled: 1, actions: ["submit"], operatorReviewAvailable: false },
    { enabled: true, actions: ["review"], operatorReviewAvailable: "true" },
    { enabled: true, actions: ["admin"], operatorReviewAvailable: false },
    { enabled: true }, null,
  ])("keeps malformed capability evidence unavailable (%j)", async (capabilities) => {
    fetch.mockResolvedValue(json(capabilities));
    await expect(publishing.getPublicationCapabilities()).rejects.toThrow(/unknown.*invalid capability/i);
  });

  it("reads the flat owner state, encodes the source name, and preserves exact refs", async () => {
    const name = "helper/encoded";
    fetch.mockResolvedValue(json({ ...publicationHead, sourceName: name }));
    const signal = new AbortController().signal;
    const result = await publishing.getOwnerPublication("agent", name, signal);
    expect(result?.pendingSource).toEqual(publicationRef);
    expect(result?.revision).toBe(11);
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/publications/agent/helper%2Fencoded", { cache: "no-store", signal });
  });

  it("distinguishes no owner submission from an unavailable owner read", async () => {
    fetch.mockResolvedValueOnce(json(null)).mockResolvedValueOnce(json({ detail: "publication_not_authorized" }, 403));
    await expect(publishing.getOwnerPublication("agent", "helper")).resolves.toBeNull();
    await expect(publishing.getOwnerPublication("agent", "helper")).rejects.toMatchObject({ status: 403, detail: "publication_not_authorized" });
  });

  it.each([
    { ...publicationHead, sourceName: "another-draft" },
    { ...publicationHead, revision: "11" },
    { ...publicationHead, pendingSource: { ...publicationRef, assetId: "0".repeat(32) } },
    { ...publicationHead, pendingVersion: 1 },
    { ...publicationHead, pendingSource: null },
  ])("rejects a stale or malformed owner state", async (head) => {
    fetch.mockResolvedValue(json(head));
    await expect(publishing.getOwnerPublication("agent", "helper")).rejects.toThrow(/stale or invalid/i);
  });

  it("posts only the explicit submission DTO and its saved draft revision", async () => {
    fetch.mockResolvedValue(json(submitted));
    const input: publishing.PublicationSubmit = {
      expectedRevision: 7, audience: { visibility: "shared", acl: ["reader@example.com"], groupAcl: [] },
      modelIds: ["fixture-text", "fixture-embedding"], modes: ["chat"], reviewConsent: true,
      operatorReviewConsent: false, skillMode: "versioned",
    };
    const signal = new AbortController().signal;
    await publishing.submitPublication("agent", "helper", input, submissionState, signal);
    expect(fetch).toHaveBeenCalledExactlyOnceWith("/api/publications/agent/helper/submit", {
      cache: "no-store", signal, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input),
    });
  });

  it("uses the exact pending reference and head revision for owner activation, not the draft revision", async () => {
    fetch.mockResolvedValueOnce(json(activated)).mockResolvedValueOnce(json(withdrawn));
    await expect(publishing.activatePublication("agent", "helper", {
      source: publicationRef, expectedHeadRevision: 11,
    })).resolves.toEqual(activated);
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ source: publicationRef, expectedHeadRevision: 11 });
    expect(fetch.mock.calls[0][0]).toBe("/api/publications/agent/helper/activate");
    await expect(publishing.withdrawPublication("agent", "helper", activated)).resolves.toEqual(withdrawn);
    expect(JSON.parse(String(fetch.mock.calls[1][1]?.body))).toEqual({ expectedHeadRevision: 12 });
    expect(fetch.mock.calls[1][0]).toBe("/api/publications/agent/helper/withdraw");
  });

  it.each([
    null, {}, publicationHead,
    { ...publicationHead, revision: 12, reviewDecision: "approved", reviewerId: "reviewer" },
    { ...submitted, versionCount: 4, pendingVersion: 4, pendingSource: { ...publicationRef, version: 4 } },
    { ...submitted, assetId: "0".repeat(32), pendingSource: { ...submitted.pendingSource, assetId: "0".repeat(32) } },
    { ...submitted, kind: "workflow" },
    { ...submitted, sourceName: "other" },
    { ...submitted, userId: "other", pendingSource: { ...submitted.pendingSource, ownerId: "other" } },
    { ...submitted, sourceIncarnation: "0".repeat(32) },
    { ...submitted, pendingDraftRevision: 6 },
    { ...submitted, deleted: true },
    { ...submitted, reviewConsent: false },
    { ...submitted, operatorReviewConsent: true },
    { ...submitted, reviewerUserId: "unrequested-reviewer" },
    { ...submitted, visibility: ["public"] },
  ])("rejects missing, stale or mismatched submission acknowledgements", async (value) => {
    fetch.mockResolvedValue(json(value));
    await expect(publishing.submitPublication("agent", "helper", submission, submissionState))
      .rejects.toThrow(/acknowledgement.*Refresh/i);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each(["first", "recreated", "deleted"] as const)("requires a new asset's first version for %s submission", async (scenario) => {
    const first = { ...submitted, revision: scenario === "first" ? 1 : 12, assetId: "0".repeat(32), versionCount: 1, pendingVersion: 1,
      pendingSource: { ...publicationRef, assetId: "0".repeat(32), version: 1 } };
    const expected: publishing.PublicationSubmissionState = {
      ...submissionState, previousHead: scenario === "first" ? null :
        { ...publicationHead, ...(scenario === "deleted" ? { deleted: true } : { sourceIncarnation: "1".repeat(32) }) },
    };
    fetch.mockResolvedValueOnce(json(first)).mockResolvedValueOnce(json({
      ...first, assetId: publicationHead.assetId,
      pendingSource: { ...first.pendingSource, assetId: publicationHead.assetId },
      ...(scenario === "first" ? { versionCount: 2, pendingVersion: 2, pendingSource: publicationRef } : {}),
    }));
    await expect(publishing.submitPublication("agent", "helper", submission, expected)).resolves.toEqual(first);
    await expect(publishing.submitPublication("agent", "helper", submission, expected)).rejects.toThrow(/newly submitted version/i);
  });

  it.each([
    null, {}, { ...activated, revision: 11 },
    { ...activated, pendingVersion: 2, pendingSource: publicationRef, pendingDraftRevision: 7 },
    { ...activated, activeSource: { ...publicationRef, digest: "0".repeat(64) } },
    { ...activated, visibility: "private" },
    { ...activated, deleted: true },
  ])("never announces activation without its exact source and transition acknowledgement", async (value) => {
    fetch.mockResolvedValue(json(value));
    await expect(publishing.activatePublication("agent", "helper", {
      source: publicationRef, expectedHeadRevision: 11,
    })).rejects.toThrow(/acknowledgement.*Refresh/i);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each([
    null, {}, activated, { ...withdrawn, assetId: "0".repeat(32) },
    { ...withdrawn, activeVersion: 2, activeSource: publicationRef },
    { ...withdrawn, visibility: "public" },
    { ...withdrawn, acl: ["still-shared@example.com"] },
    { ...withdrawn, groupAcl: ["still-shared-group"] },
  ])("never announces withdrawal with an unconfirmed or still-shared head", async (value) => {
    fetch.mockResolvedValue(json(value));
    await expect(publishing.withdrawPublication("agent", "helper", activated))
      .rejects.toThrow(/acknowledgement.*Refresh/i);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it("reads reviews by the exact escaped owner, asset, version, and digest", async () => {
    const source = { ...publicationRef, ownerId: "owner/with:reserved?chars" };
    fetch.mockResolvedValue(json({ ...publicationReview, version: { ...publicationReview.version, userId: source.ownerId } }));
    const signal = new AbortController().signal;
    const detail = await publishing.getPublicationReview(source, signal);
    expect(detail.headRevision).toBe(13);
    expect(fetch).toHaveBeenCalledExactlyOnceWith(
      `/api/publication-reviews/agent/owner%2Fwith%3Areserved%3Fchars/${source.assetId}/2?digest=${source.digest}`,
      { cache: "no-store", signal },
    );
  });

  it.each([
    { ...publicationRef, assetId: "not-an-asset" },
    { ...publicationRef, version: 0 },
    { ...publicationRef, digest: "C".repeat(64) },
  ])("rejects invalid version references before any request", async (source) => {
    await expect(publishing.getPublicationReview(source)).rejects.toThrow(/invalid publication version/i);
    expect(fetch).not.toHaveBeenCalled();
  });

  it.each([
    { ...publicationReview, version: { ...publicationReview.version, digest: "0".repeat(64) } },
    { ...publicationReview, version: { ...publicationReview.version, userId: "other-owner" } },
    { ...publicationReview, headRevision: 0 },
  ])("rejects a mismatched review snapshot rather than enabling a decision", async (detail) => {
    fetch.mockResolvedValue(json(detail));
    await expect(publishing.getPublicationReview(publicationRef)).rejects.toThrow(/review response is stale/i);
  });

  it.each(["approved", "rejected"] as const)("sends the explicit %s decision without author or reviewer grants", async (decision) => {
    const summary = { source: publicationRef, displayName: "Helper", headRevision: 14, reviewDecision: decision, reviewerId: "reviewer" };
    fetch.mockResolvedValue(json(summary));
    const input: publishing.PublicationReviewRequest = { source: publicationRef, expectedHeadRevision: 13, decision, note: "Inspected this version." };
    await expect(publishing.decidePublicationReview(input)).resolves.toEqual(summary);
    expect(fetch.mock.calls[0][0]).toBe("/api/publication-reviews/decision");
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual(input);
  });

  it.each([
    null, {},
    { source: publicationRef, displayName: "Helper", headRevision: 14 },
    { source: publicationRef, displayName: "Helper", headRevision: 13, reviewDecision: "approved", reviewerId: "reviewer" },
    { source: publicationRef, displayName: "Helper", headRevision: 14, reviewDecision: "rejected", reviewerId: "reviewer" },
    { source: publicationRef, displayName: "Helper", headRevision: 14, reviewDecision: "approved", reviewerId: publicationRef.ownerId },
    { source: { ...publicationRef, digest: "0".repeat(64) }, displayName: "Helper", headRevision: 14, reviewDecision: "approved", reviewerId: "reviewer" },
  ])("does not accept a missing or mismatched immutable review acknowledgement", async (value) => {
    fetch.mockResolvedValue(json(value));
    await expect(publishing.decidePublicationReview({
      source: publicationRef, expectedHeadRevision: 13, decision: "approved", note: "",
    })).rejects.toThrow(/acknowledgement.*Reload/i);
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it.each(["approved", "rejected"] as const)("retains authoritative %s review status on list and detail reads", async (decision) => {
    const status = { reviewDecision: decision, reviewerId: "reviewer" };
    const item = { source: publicationRef, displayName: "Helper", headRevision: 14, ...status };
    fetch.mockResolvedValueOnce(json({ items: [item], truncated: false }))
      .mockResolvedValueOnce(json({ ...publicationReview, headRevision: 14, ...status }));
    expect((await publishing.listPublicationReviews("agent")).items).toEqual([item]);
    expect(await publishing.getPublicationReview(publicationRef)).toMatchObject(status);
  });

  it.each(["agent", "workflow"] as const)("keeps %s inbox/catalog reads separate and preserves truncation", async (kind) => {
    fetch.mockImplementation(async () => json({ items: [], truncated: true }));
    const signal = new AbortController().signal;
    await expect(publishing.listPublicationReviews(kind, signal)).resolves.toEqual({ items: [], truncated: true });
    await expect(publishing.listPublications(kind, signal)).resolves.toEqual({ items: [], truncated: true });
    expect(fetch.mock.calls.map(([path]) => path)).toEqual([`/api/publication-reviews?kind=${kind}`, `/api/publications?kind=${kind}`]);
    expect(fetch.mock.calls.every(([, init]) => init?.cache === "no-store" && init?.signal === signal && !init?.method)).toBe(true);
  });

  it.each([409, 412, 422, 503])("preserves HTTP %s reasons and never retries or changes the skill mode", async (status) => {
    fetch.mockResolvedValue(json({ detail: "publication_unversioned_skill" }, status));
    await expect(publishing.submitPublication("agent", "helper", {
      expectedRevision: 7, audience: { visibility: "public", acl: [], groupAcl: [] }, modelIds: ["fixture-text"],
      modes: ["chat"], reviewConsent: true, operatorReviewConsent: false, skillMode: "versioned",
    }, submissionState)).rejects.toMatchObject({ status, detail: "publication_unversioned_skill" });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(String(fetch.mock.calls[0][1]?.body)).skillMode).toBe("versioned");
  });

  it("keeps structured validation errors readable instead of flattening them to object strings", async () => {
    const detail = [{ loc: ["body", "audience"], msg: "Invalid publication grantee." }];
    fetch.mockResolvedValue(json({ detail }, 422));
    await expect(publishing.listPublications("agent")).rejects.toEqual(new ApiError(422, JSON.stringify(detail)));
  });

  it.each([{ items: [] }, { items: null, truncated: false }, { items: [{ source: publicationRef }], truncated: false }])(
    "keeps malformed list coverage unavailable rather than rendering it as empty", async (value) => {
      fetch.mockImplementation(async () => json(value));
      await expect(publishing.listPublications("agent")).rejects.toThrow(/list is unavailable/);
      await expect(publishing.listPublicationReviews("agent")).rejects.toThrow(/list is unavailable/);
    },
  );

  it("never turns malformed success JSON or transport failure into an empty catalog", async () => {
    fetch.mockResolvedValueOnce(new Response("not JSON")).mockRejectedValueOnce(new TypeError("offline"));
    await expect(publishing.listPublications("agent")).rejects.toBeInstanceOf(SyntaxError);
    await expect(publishing.listPublications("agent")).rejects.toThrow("offline");
  });

  it("normalizes emails but preserves exact group GUIDs, and makes tenant visibility explicit", () => {
    const group = "ABCD1234-1234-1234-1234-1234567890AB";
    expect(publishing.publicationAudience("shared", " Reader@Example.com,reader@example.com\n Second@Example.com ", group)).toEqual({
      visibility: "shared", acl: ["reader@example.com", "second@example.com"], groupAcl: [group],
    });
    expect(publishing.publicationAudience("public", "reader@example.com", group)).toEqual({ visibility: "public", acl: [], groupAcl: [] });
    expect(publishing.publicationVisibilityLabel("public")).toBe("Tenant-visible");
    expect(() => publishing.publicationAudience("shared", "", "")).toThrow(/at least one/i);
    expect(() => publishing.publicationAudience("shared", "", `${group},${group}`)).toThrow(/exact, unique/i);
    expect(() => publishing.publicationAudience("shared", "", "Engineering")).toThrow(/exact, unique/i);
  });

  it("passes real draft revisions through the existing update clients, while legacy omission stays omitted", async () => {
    fetch.mockImplementation(async () => json(publicationAgent));
    const agent = { displayName: "Helper", description: "", systemPrompt: "Be helpful.", tools: [], links: [], enabled: true };
    const workflow = { displayName: "Summary", description: "", steps: [{ agent: "helper", instruction: "Summarize {input}" }], enabled: true };
    await updateAgent("helper", { ...agent, expectedRevision: 7 });
    await updateWorkflow("summary", { ...workflow, expectedRevision: 9 });
    await updateAgent("helper", agent);
    await updateWorkflow("summary", workflow);
    expect(fetch.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))).toEqual([
      { ...agent, expectedRevision: 7 }, { ...workflow, expectedRevision: 9 }, agent, workflow,
    ]);
  });
});
