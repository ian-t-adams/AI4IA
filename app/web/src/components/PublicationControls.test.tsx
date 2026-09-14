// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ComponentProps } from "react";
import { apiFetch } from "@/lib/auth";
import type {
  AssetVersionRef, OwnerPublicationState, PublicationList, PublicationReviewDetail,
  PublicationReviewRequest, PublicationReviewSummary, PublicationSubmit, PublicationSummary,
} from "@/lib/publishing";
import { sameAssetVersionRef } from "@/lib/publishing";
import { deferred, publicationAgent, publicationHead, publicationModels, publicationRef, publicationReview } from "@/lib/publishingTestFixtures";
import { PublicationControls } from "./PublicationControls";

vi.mock("@/lib/auth", () => ({ apiFetch: vi.fn() }));
const fetch = vi.mocked(apiFetch);
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const ownerPath = "/api/publications/agent/helper";
const detailPath = (source: AssetVersionRef) => `/api/publication-reviews/${source.kind}/${encodeURIComponent(source.ownerId)}/${source.assetId}/${source.version}?digest=${source.digest}`;
let capabilities: unknown;
let heads: Map<string, OwnerPublicationState | null>;
let reviews: PublicationList<PublicationReviewSummary>;
let details: Map<string, PublicationReviewDetail>;
let catalog: PublicationList<PublicationSummary>;

async function reply(input: Parameters<typeof apiFetch>[0], init?: RequestInit): Promise<Response> {
  const path = String(input);
  if (init?.method === "POST") {
    if (path === "/api/publication-reviews/decision") {
      const body: PublicationReviewRequest = JSON.parse(String(init.body));
      const detail = details.get(detailPath(body.source));
      if (!detail) throw new Error("Unexpected publication review");
      if (detail.reviewDecision !== null) return json({ detail: "publication_version_changed" }, 409);
      const summary: PublicationReviewSummary = {
        source: body.source, displayName: detail.version.source.displayName, headRevision: body.expectedHeadRevision + 1,
        reviewDecision: body.decision, reviewerId: "reviewer",
      };
      details.set(detailPath(body.source), { ...detail, headRevision: summary.headRevision,
        reviewDecision: summary.reviewDecision, reviewerId: summary.reviewerId });
      reviews = { ...reviews, items: reviews.items.map((item) => sameAssetVersionRef(item.source, body.source) ? summary : item) };
      return json(summary);
    }
    const action = path.slice(path.lastIndexOf("/") + 1);
    const target = path.slice(0, path.lastIndexOf("/"));
    if (!heads.has(target)) throw new Error(`Unexpected publication write: ${path}`);
    const head = heads.get(target) ?? publicationHead;
    if (action === "submit") {
      const body: PublicationSubmit = JSON.parse(String(init.body));
      const previous = heads.get(target);
      const version = (previous?.versionCount ?? 0) + 1;
      const submitted: OwnerPublicationState = {
        ...head, revision: (previous?.revision ?? 0) + 1, versionCount: version, pendingVersion: version,
        pendingSource: { ...publicationRef, version }, pendingDraftRevision: body.expectedRevision,
        reviewDecision: null, reviewerId: null, reviewConsent: true,
        operatorReviewConsent: body.operatorReviewConsent, reviewerUserId: body.reviewerUserId ?? null,
      };
      heads.set(target, submitted);
      return json(submitted);
    }
    if (action === "activate") {
      const active = { ...head, revision: head.revision + 1, activeVersion: head.pendingVersion, activeSource: head.pendingSource,
        pendingVersion: null, pendingSource: null, pendingDraftRevision: null, reviewDecision: null, reviewerId: null, visibility: "public" as const };
      heads.set(target, active);
      return json(active);
    }
    if (action === "withdraw") {
      const withdrawn = { ...head, revision: head.revision + 1, activeVersion: null, activeSource: null, pendingVersion: null, pendingSource: null,
        pendingDraftRevision: null, reviewDecision: null, reviewerId: null, visibility: "private" as const, acl: [], groupAcl: [] };
      heads.set(target, withdrawn);
      return json(withdrawn);
    }
    throw new Error(`Unexpected publication action: ${path}`);
  }
  if (path === "/api/publications/capabilities") return json(capabilities);
  if (path === "/api/models") return json({ models: publicationModels, residencyPolicy: "global" });
  if (heads.has(path)) return json(heads.get(path));
  if (details.has(path)) return json(details.get(path));
  if (path.startsWith("/api/publication-reviews?kind=")) return json(reviews);
  if (path.startsWith("/api/publications?kind=")) return json(catalog);
  throw new Error(`Unexpected publication read: ${path}`);
}

function controls(props: Partial<ComponentProps<typeof PublicationControls>> = {}) {
  return <PublicationControls kind="agent" saved={publicationAgent} dirty={false} busy={false}
    models={publicationModels} defaultModelId={publicationAgent.defaultModel} {...props} />;
}

const writes = () => fetch.mock.calls.filter(([, init]) => init?.method === "POST");
const callsTo = (path: string) => fetch.mock.calls.filter(([input]) => input === path);
const posted = (suffix: string) => JSON.parse(String(writes().find(([path]) => String(path).endsWith(suffix))?.[1]?.body));

async function readySubmission(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Submit a saved version for review"));
  await user.type(await screen.findByLabelText("Recipient emails"), " READER@Example.com ");
  await user.click(screen.getByRole("checkbox", { name: "I consent to independent review of this submitted source" }));
  return screen.getByRole("button", { name: "Submit for independent review" });
}

async function openReview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByText("Independent review inbox"));
  await user.click(await screen.findByRole("button", { name: "Review Helper (version 2)" }));
  return screen.findByRole("region", { name: "Review submitted version" });
}

beforeEach(() => {
  capabilities = { enabled: true, actions: ["submit"], operatorReviewAvailable: false };
  heads = new Map([[ownerPath, { ...publicationHead }]]);
  reviews = { items: [{ source: publicationRef, displayName: "Helper", headRevision: 11, reviewDecision: null, reviewerId: null }], truncated: false };
  details = new Map([[detailPath(publicationRef), publicationReview]]);
  catalog = { items: [], truncated: false };
  fetch.mockReset();
  fetch.mockImplementation(reply);
});

afterEach(() => { cleanup(); });

describe("PublicationControls", () => {
  it.each([false, true])("preserves private editing and gates all publication reads when enabled=%s", async (enabled) => {
    capabilities = { enabled, actions: ["submit", "review", "consume"], operatorReviewAvailable: true };
    render(controls());
    await waitFor(() => expect(screen.queryByText("Loading publication availability...")).not.toBeInTheDocument());
    if (enabled) {
      expect(await screen.findByRole("region", { name: "Publication" })).toBeInTheDocument();
      await waitFor(() => expect(callsTo(ownerPath)).toHaveLength(1));
      expect(screen.getByText("Independent review inbox")).toBeInTheDocument();
    } else {
      expect(screen.queryByRole("region", { name: "Publication" })).not.toBeInTheDocument();
      expect(callsTo(ownerPath)).toHaveLength(0);
      expect(fetch).toHaveBeenCalledTimes(1);
    }
    expect(writes()).toHaveLength(0);
  });

  it.each(["true", 1, null])("does not treat truthy or unknown enabled evidence (%j) as availability", async (enabled) => {
    capabilities = { enabled, actions: ["submit", "review"], operatorReviewAvailable: false };
    render(controls());
    expect(await screen.findByRole("alert")).toHaveTextContent(/availability.*unavailable.*unknown/i);
    expect(screen.getByText("Private editing is still available.")).toBeInTheDocument();
    expect(callsTo(ownerPath)).toHaveLength(0);
    expect(writes()).toHaveLength(0);
  });

  it.each([
    { actions: ["submit"], operatorReviewAvailable: false, review: false },
    { actions: ["review"], operatorReviewAvailable: false, review: true },
    { actions: [], operatorReviewAvailable: true, review: true },
    { actions: [], operatorReviewAvailable: false, review: false },
  ])("separates author, reviewer, and explicitly advertised operator actions (%j)", async (posture) => {
    capabilities = { enabled: true, ...posture };
    render(controls());
    await screen.findByRole("region", { name: "Publication" });
    expect(screen.queryByText("Independent review inbox") !== null).toBe(posture.review);
    expect(screen.queryByRole("button", { name: "Approve reviewed version" })).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Withdraw publication" })).toBeEnabled();
    expect(callsTo("/api/publication-reviews?kind=agent")).toHaveLength(0);
  });

  it("submits only saved content with exact revision, chosen capability models, and separate explicit consents", async () => {
    heads.set(ownerPath, null);
    const user = userEvent.setup();
    render(controls());
    expect(await screen.findByText(/No publication submitted/)).toBeInTheDocument();
    await user.click(screen.getByText("Submit a saved version for review"));
    const submit = await screen.findByRole("button", { name: "Submit for independent review" });
    expect(submit).toBeDisabled();
    expect(screen.getByLabelText("Skill profile")).toHaveValue("versioned");
    expect(screen.getByLabelText("Also allow operator review of this submission")).not.toBeChecked();
    await user.type(screen.getByLabelText("Recipient emails"), "Reader@Example.com, reader@example.com");
    await user.type(screen.getByLabelText("Group object IDs"), "ABCD1234-1234-1234-1234-1234567890AB");
    await user.selectOptions(screen.getByLabelText("Publication models"), "fixture-embedding");
    await user.click(screen.getByLabelText("I consent to independent review of this submitted source"));
    expect(writes()).toHaveLength(0);
    await user.click(submit);
    await screen.findByText(/Submitted for independent review\. Nothing/);
    expect(posted("/submit")).toEqual({
      expectedRevision: 7, audience: { visibility: "shared", acl: ["reader@example.com"], groupAcl: ["ABCD1234-1234-1234-1234-1234567890AB"] },
      modelIds: ["fixture-text", "fixture-embedding"], modes: ["chat"], reviewConsent: true, operatorReviewConsent: false, skillMode: "versioned",
    });
    await waitFor(() => expect(callsTo(ownerPath)).toHaveLength(2));
    expect(writes()).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Approve reviewed version" })).not.toBeInTheDocument();
  });

  it.each(["dirty", "busy", "unknown-revision"] as const)("blocks submission for %s while keeping owner withdrawal independent", async (condition) => {
    const user = userEvent.setup();
    const view = render(controls());
    const submit = await readySubmission(user);
    expect(submit).toBeEnabled();
    view.rerender(controls(condition === "unknown-revision" ? { saved: { ...publicationAgent, revision: undefined } } : condition === "dirty" ? { dirty: true } : { busy: true }));
    if (condition === "unknown-revision") {
      expect(await screen.findByText(/Saved revision unavailable/)).toBeInTheDocument();
      expect(await readySubmission(user)).toBeDisabled();
    } else {
      expect(submit).toBeDisabled();
    }
    expect(writes()).toHaveLength(0);
    if (condition !== "busy") expect(screen.getByRole("button", { name: "Withdraw publication" })).toBeEnabled();
  });

  it("requires a shared recipient and an independent reviewer ID, without a directory lookup", async () => {
    const user = userEvent.setup();
    render(controls());
    const submit = await readySubmission(user);
    await user.clear(screen.getByLabelText("Recipient emails"));
    await user.click(submit);
    expect(await screen.findByRole("alert")).toHaveTextContent(/at least one email or group/);
    await user.selectOptions(screen.getByLabelText("Publication audience"), "public");
    expect(screen.getByRole("option", { name: "Tenant-visible" })).toBeInTheDocument();
    await user.type(screen.getByLabelText("Reviewer internal user ID (optional)"), publicationAgent.userId);
    await user.click(submit);
    expect(await screen.findByRole("alert")).toHaveTextContent(/not your own ID/);
    expect(writes()).toHaveLength(0);
    await user.clear(screen.getByLabelText("Reviewer internal user ID (optional)"));
    await user.type(screen.getByLabelText("Reviewer internal user ID (optional)"), "reviewer-exact-ID");
    await user.click(screen.getByLabelText("Also allow operator review of this submission"));
    await user.click(submit);
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(posted("/submit")).toMatchObject({ audience: { visibility: "public", acl: [], groupAcl: [] }, reviewerUserId: "reviewer-exact-ID", operatorReviewConsent: true });
  });

  it.each([false, true])("prevents duplicate writes and ignores completed submissions after unmount=%s", async (unmount) => {
    const pending = deferred<Response>();
    fetch.mockImplementation((input, init) => String(input).endsWith("/submit") ? pending.promise : reply(input, init));
    const user = userEvent.setup();
    const view = render(controls());
    const submit = await readySubmission(user);
    await user.dblClick(submit);
    expect(writes()).toHaveLength(1);
    expect(screen.getByRole("button", { name: "Submitting for review..." })).toBeDisabled();
    const signal = writes()[0][1]?.signal;
    if (unmount) view.unmount();
    await act(async () => { pending.resolve(await reply(...writes()[0])); });
    expect(signal?.aborted).toBe(unmount);
    await waitFor(() => expect(callsTo(ownerPath)).toHaveLength(unmount ? 1 : 2));
  });

  it.each([409, 412])("makes %s conflicts stale until an explicit refresh, without replaying", async (status) => {
    fetch.mockImplementation((input, init) => String(input).endsWith("/submit") ? Promise.resolve(json({ detail: "publication_source_changed" }, status)) : reply(input, init));
    const user = userEvent.setup();
    render(controls());
    await user.click(await readySubmission(user));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Conflict:.*publication_source_changed/);
    expect(screen.getByRole("button", { name: "Submit for independent review" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Withdraw publication" })).toBeDisabled();
    fetch.mockImplementation(reply);
    await user.click(screen.getByRole("button", { name: "Refresh publication status" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Submit for independent review" })).toBeEnabled());
    expect(writes()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Submit for independent review" }));
    await waitFor(() => expect(writes()).toHaveLength(2));
  });

  it("reports a lost write acknowledgement as unknown and blocks blind retry", async () => {
    fetch.mockImplementation((input, init) => String(input).endsWith("/submit") ? Promise.reject(new TypeError("connection lost")) : reply(input, init));
    const user = userEvent.setup();
    render(controls());
    await user.click(await readySubmission(user));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Outcome unknown.*connection lost/);
    expect(screen.getByRole("button", { name: "Submit for independent review" })).toBeDisabled();
    expect(writes()).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Refresh publication status" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Submit for independent review" })).toBeEnabled());
    expect(writes()).toHaveLength(1);
  });

  it.each(["submit", "activate", "withdraw", "decision"] as const)(
    "keeps malformed successful %s acknowledgements unknown without announcing or replaying a transition", async (action) => {
      const user = userEvent.setup();
      fetch.mockImplementation((input, init) => init?.method === "POST" ? Promise.resolve(json(null)) : reply(input, init));
      let button: HTMLElement;
      if (action === "decision") {
        capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
        render(controls({ saved: null, ownerId: "reviewer" }));
        await openReview(user);
        button = screen.getByRole("button", { name: "Approve reviewed version" });
      } else {
        if (action === "activate") heads.set(ownerPath, { ...publicationHead, reviewDecision: "approved", reviewerId: "reviewer" });
        render(controls());
        button = action === "submit" ? await readySubmission(user) :
          await screen.findByRole("button", { name: action === "activate" ? "Activate reviewed version" : "Withdraw publication" });
      }
      expect(button).toBeEnabled();
      await user.click(button);
      expect(await screen.findByRole("alert")).toHaveTextContent(/Outcome unknown.*acknowledgement/i);
      expect(button).toBeDisabled();
      expect(writes()).toHaveLength(1);
      expect(screen.queryByText(/Submitted for independent review\.|Reviewed version activated\.|Publication withdrawn\.|Review recorded\./)).not.toBeInTheDocument();
      if (action === "decision") expect(screen.getByRole("region", { name: "Review submitted version" })).toBeInTheDocument();
      else expect(callsTo(ownerPath)).toHaveLength(1);
    },
  );

  it("keeps unversioned-skill rejection visible and only excludes skills after an explicit choice", async () => {
    fetch.mockImplementation((input, init) => String(input).endsWith("/submit") ? Promise.resolve(json({ detail: "publication_unversioned_skill" }, 422)) : reply(input, init));
    const user = userEvent.setup();
    render(controls());
    await user.click(await readySubmission(user));
    expect(await screen.findByRole("alert")).toHaveTextContent("publication_unversioned_skill");
    expect(screen.getByLabelText("Skill profile")).toHaveValue("versioned");
    expect(writes()).toHaveLength(1);
    expect(posted("/submit").skillMode).toBe("versioned");
    fetch.mockImplementation(reply);
    await user.selectOptions(screen.getByLabelText("Skill profile"), "excluded");
    expect(screen.getByText(/explicitly requesting a reviewed profile without skills/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Submit for independent review" }));
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(JSON.parse(String(writes()[1][1]?.body)).skillMode).toBe("excluded");
  });

  it("activates only after independent approval with the freshly read reference and head revision", async () => {
    const user = userEvent.setup();
    render(controls());
    await screen.findByText(/Awaiting independent review/);
    expect(screen.queryByRole("button", { name: "Activate reviewed version" })).not.toBeInTheDocument();
    heads.set(ownerPath, { ...publicationHead, revision: 14, reviewDecision: "approved", reviewerId: "reviewer" });
    await user.click(screen.getByRole("button", { name: "Refresh publication status" }));
    const activate = await screen.findByRole("button", { name: "Activate reviewed version" });
    expect(writes()).toHaveLength(0);
    await user.click(activate);
    expect(await screen.findByText(/Reviewed version activated/)).toBeInTheDocument();
    expect(posted("/activate")).toEqual({ source: publicationRef, expectedHeadRevision: 14 });
    await waitFor(() => expect(screen.getByText(/Tenant-visible: active version 2/)).toBeInTheDocument());
  });

  it("does not activate approval from an earlier draft incarnation, but still allows withdrawal", async () => {
    heads.set(ownerPath, { ...publicationHead, reviewDecision: "approved", reviewerId: "reviewer", sourceIncarnation: "0".repeat(32) });
    const user = userEvent.setup();
    render(controls());
    expect(await screen.findByRole("button", { name: "Activate reviewed version" })).toBeDisabled();
    expect(screen.getByText(/earlier draft incarnation/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Withdraw publication" })).toBeEnabled();
    heads.set(ownerPath, { ...publicationHead, reviewDecision: "approved", reviewerId: "reviewer" });
    await user.click(screen.getByRole("button", { name: "Refresh publication status" }));
    expect(await screen.findByRole("button", { name: "Activate reviewed version" })).toBeEnabled();
    expect(writes()).toHaveLength(0);
  });

  it("lets an owner withdraw after losing submit access, using only the owned head revision", async () => {
    capabilities = { enabled: true, actions: [], operatorReviewAvailable: false };
    const user = userEvent.setup();
    render(controls());
    await user.click(await screen.findByRole("button", { name: "Withdraw publication" }));
    expect(await screen.findByText(/Immutable review history is retained/)).toBeInTheDocument();
    expect(posted("/withdraw")).toEqual({ expectedHeadRevision: 11 });
    expect(screen.queryByText("Submit a saved version for review")).not.toBeInTheDocument();
  });

  it("retires owner responses when switching assets, even if the old request resolves last", async () => {
    const pending = deferred<Response>();
    fetch.mockImplementation((input, init) => input === ownerPath ? pending.promise : reply(input, init));
    heads.set("/api/publications/agent/other", { ...publicationHead, sourceName: "other", handle: "published-other" });
    const view = render(controls());
    await waitFor(() => expect(callsTo(ownerPath)).toHaveLength(1));
    view.rerender(controls({ saved: { ...publicationAgent, name: "other" } }));
    expect(await screen.findByText("published-other")).toBeInTheDocument();
    await act(async () => { pending.resolve(json(publicationHead)); });
    expect(screen.queryByText("published-helper")).not.toBeInTheDocument();
    expect(callsTo(ownerPath)[0][1]?.signal?.aborted).toBe(true);
    expect(writes()).toHaveLength(0);
  });

  it("shows owned submissions without self-review controls", async () => {
    capabilities = { enabled: true, actions: ["submit", "review"], operatorReviewAvailable: false };
    const user = userEvent.setup();
    render(controls());
    await user.click(await screen.findByText("Independent review inbox"));
    expect(await screen.findByText(/Your submission: Helper/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review Helper (version 2)" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve reviewed version" })).not.toBeInTheDocument();
    expect(callsTo(detailPath(publicationRef))).toHaveLength(0);
  });

  it("discards an old review snapshot when the reviewer selects another submitted version", async () => {
    capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
    const second = { ...publicationRef, assetId: "4".repeat(32), digest: "5".repeat(64) };
    reviews = { items: [...reviews.items, { source: second, displayName: "Second helper", headRevision: 21, reviewDecision: null, reviewerId: null }], truncated: true };
    details.set(detailPath(second), { ...publicationReview, headRevision: 22, version: { ...publicationReview.version,
      assetId: second.assetId, digest: second.digest, source: { ...publicationAgent, displayName: "Second helper" } } });
    const pending = deferred<Response>();
    fetch.mockImplementation((input, init) => input === detailPath(publicationRef) ? pending.promise : reply(input, init));
    const user = userEvent.setup();
    render(controls({ saved: null, ownerId: "reviewer" }));
    await user.click(await screen.findByText("Independent review inbox"));
    await user.click(await screen.findByRole("button", { name: "Review Helper (version 2)" }));
    expect(await screen.findByText("Loading review snapshot...")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Review Second helper (version 2)" }));
    expect(await screen.findByRole("heading", { name: "Review Second helper, version 2" })).toBeInTheDocument();
    await act(async () => { pending.resolve(json(publicationReview)); });
    expect(screen.queryByRole("heading", { name: "Review Helper, version 2" })).not.toBeInTheDocument();
    expect(callsTo(detailPath(publicationRef))[0][1]?.signal?.aborted).toBe(true);
    await user.click(screen.getByRole("button", { name: "Approve reviewed version" }));
    await waitFor(() => expect(writes()).toHaveLength(1));
    expect(posted("/decision")).toMatchObject({ source: second, expectedHeadRevision: 22 });
  });

  it.each(["approved", "rejected"] as const)("lets an independent reviewer explicitly record %s against the inspected revision", async (decision) => {
    capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
    const user = userEvent.setup();
    render(controls({ saved: null, ownerId: "reviewer" }));
    const region = await openReview(user);
    expect(within(region).getByText(/Publication revision 13/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Activate reviewed version" })).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);
    await user.type(screen.getByLabelText("Review note (optional, up to 1000 characters)"), "Inspected this snapshot.");
    await user.click(screen.getByRole("button", { name: decision === "approved" ? "Approve reviewed version" : "Reject version" }));
    expect(await screen.findByText(/Review recorded\. Owner activation is still required/)).toBeInTheDocument();
    expect(posted("/decision")).toEqual({ source: publicationRef, expectedHeadRevision: 13, decision, note: "Inspected this snapshot." });
    expect(writes()).toHaveLength(1);
  });

  it.each([false, true])("keeps an acknowledged review read-only with stale reads=%s, but not a new exact version", async (staleReads) => {
    capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
    const originalInbox = reviews;
    let recorded = false;
    fetch.mockImplementation(async (input, init) => {
      if (staleReads && recorded) {
        if (input === "/api/publication-reviews?kind=agent") return json(originalInbox);
        if (input === detailPath(publicationRef)) return json(publicationReview);
      }
      const response = await reply(input, init);
      if (input === "/api/publication-reviews/decision") recorded = true;
      return response;
    });
    const user = userEvent.setup();
    render(controls({ saved: null, ownerId: "reviewer" }));
    await openReview(user);
    expect(screen.getByRole("button", { name: "Approve reviewed version" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Approve reviewed version" }));
    await screen.findByText(/Review recorded\. Owner activation/);
    await user.click(await screen.findByRole("button", { name: "View reviewed Helper (version 2)" }));
    expect(await screen.findByText(/This version was approved/)).toHaveTextContent(/decision is immutable/);
    expect(screen.queryByRole("button", { name: "Approve reviewed version" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject version" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reload review snapshot" }));
    expect(await screen.findByText(/This version was approved/)).toBeInTheDocument();
    expect(writes()).toHaveLength(1);

    fetch.mockImplementation(reply);
    const nextSource = { ...publicationRef, version: 3, digest: "9".repeat(64) };
    reviews = { items: [{ source: nextSource, displayName: "Helper", headRevision: 15, reviewDecision: null, reviewerId: null }], truncated: false };
    details.set(detailPath(nextSource), { ...publicationReview, headRevision: 15,
      version: { ...publicationReview.version, version: 3, digest: nextSource.digest } });
    await user.click(screen.getByRole("button", { name: "Refresh review inbox" }));
    await user.click(await screen.findByRole("button", { name: "Review Helper (version 3)" }));
    expect(await screen.findByRole("button", { name: "Approve reviewed version" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject version" })).toBeEnabled();
    expect(writes()).toHaveLength(1);
  });

  it.each(["approved", "rejected"] as const)("reopens persisted %s decisions read-only without a local acknowledgement", async (decision) => {
    capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
    const status = { reviewDecision: decision, reviewerId: "reviewer" };
    reviews = { items: [{ ...reviews.items[0], ...status }], truncated: false };
    details.set(detailPath(publicationRef), { ...publicationReview, ...status });
    const user = userEvent.setup();
    render(controls({ saved: null, ownerId: "reviewer" }));
    for (let round = 0; round < 2; round += 1) {
      await user.click(await screen.findByText("Independent review inbox"));
      await user.click(await screen.findByRole("button", { name: "View reviewed Helper (version 2)" }));
      expect(await screen.findByText(new RegExp(`This version was ${decision}`))).toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Approve reviewed version" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Reject version" })).not.toBeInTheDocument();
      await user.click(screen.getByText("Independent review inbox"));
    }
    expect(writes()).toHaveLength(0);
  });

  it.each([
    ["inbox", "approved"], ["inbox", "rejected"], ["detail", "approved"], ["detail", "rejected"],
  ] as const)("retains a completed decision learned from %s (%s) across stale reads", async (from, decision) => {
    capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
    const undecidedInbox = reviews;
    const status = { reviewDecision: decision, reviewerId: "reviewer" };
    if (from === "inbox") reviews = { ...reviews, items: [{ ...reviews.items[0], ...status }] };
    else details.set(detailPath(publicationRef), { ...publicationReview, ...status });
    const user = userEvent.setup();
    render(controls({ saved: null, ownerId: "reviewer" }));
    await user.click(await screen.findByText("Independent review inbox"));
    await user.click(await screen.findByRole("button", { name: `${from === "inbox" ? "View reviewed" : "Review"} Helper (version 2)` }));
    expect(await screen.findByText(new RegExp(`This version was ${decision}`))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve reviewed version" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject version" })).not.toBeInTheDocument();

    reviews = undecidedInbox;
    details.set(detailPath(publicationRef), publicationReview);
    await user.click(screen.getByRole("button", { name: "Reload review snapshot" }));
    expect(await screen.findByText(new RegExp(`This version was ${decision}`))).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh review inbox" }));
    await user.click(await screen.findByRole("button", { name: "View reviewed Helper (version 2)" }));
    expect(await screen.findByText(new RegExp(`This version was ${decision}`))).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve reviewed version" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reject version" })).not.toBeInTheDocument();
    expect(writes()).toHaveLength(0);

    const nextSource = { ...publicationRef, version: 3, digest: "9".repeat(64) };
    reviews = { items: [{ source: nextSource, displayName: "Helper", headRevision: 15, reviewDecision: null, reviewerId: null }], truncated: false };
    details.set(detailPath(nextSource), { ...publicationReview, headRevision: 15,
      version: { ...publicationReview.version, version: 3, digest: nextSource.digest } });
    await user.click(screen.getByRole("button", { name: "Refresh review inbox" }));
    await user.click(await screen.findByRole("button", { name: "Review Helper (version 3)" }));
    await user.click(await screen.findByRole("button", { name: "Reject version" }));
    await screen.findByText(/Review recorded\. Owner activation/);
    expect(posted("/decision")).toMatchObject({ source: nextSource, expectedHeadRevision: 15, decision: "rejected" });
    expect(writes()).toHaveLength(1);
  });

  it("renders escaped source and material audience, tool, model and skill evidence", async () => {
    capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
    const sourceText = '<img src="x" onerror="alert(1)">';
    details.set(detailPath(publicationRef), { ...publicationReview, version: { ...publicationReview.version,
      source: { ...publicationAgent, systemPrompt: sourceText }, skillMode: "excluded" } });
    const user = userEvent.setup();
    render(controls({ saved: null, ownerId: "reviewer" }));
    await openReview(user);
    for (const title of ["Submitted source", "Audience and reviewer consent", "Model bindings and declared versions", "Tool contracts, requirements and exclusions"]) {
      await user.click(screen.getByText(title));
    }
    expect(screen.getByLabelText("Submitted source")).toHaveTextContent("<img");
    expect(document.querySelector("img")).toBeNull();
    expect(screen.getByLabelText("Audience and reviewer consent")).toHaveTextContent("Tenant-visible");
    expect(screen.getByLabelText("Model bindings and declared versions")).toHaveTextContent('"runtimeEnabled": true');
    expect(screen.getByLabelText("Model bindings and declared versions")).toHaveTextContent("2026-01-01");
    const tools = screen.getByLabelText("Tool contracts, requirements and exclusions");
    for (const field of ["contractDigest", "parametersDigest", "descriptionTruncated", "risk", "scopes", "egress", "resources", "requirements", "optional", "empty_document_scope", "environmentDigest"]) {
      expect(tools).toHaveTextContent(field);
    }
    expect(screen.getByText(/Explicit no-skill profile: skill loading is excluded/)).toBeInTheDocument();
    expect(writes()).toHaveLength(0);
  });

  it("locks stale review decisions until the reviewer reloads the exact snapshot", async () => {
    capabilities = { enabled: true, actions: ["review"], operatorReviewAvailable: false };
    fetch.mockImplementation((input, init) => input === "/api/publication-reviews/decision" ? Promise.resolve(json({ detail: "publication_version_changed" }, 409)) : reply(input, init));
    const user = userEvent.setup();
    render(controls({ saved: null, ownerId: "reviewer" }));
    await openReview(user);
    await user.click(screen.getByRole("button", { name: "Approve reviewed version" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/Conflict:.*publication_version_changed/);
    expect(screen.getByRole("button", { name: "Reject version" })).toBeDisabled();
    details.set(detailPath(publicationRef), { ...publicationReview, headRevision: 20 });
    fetch.mockImplementation(reply);
    await user.click(screen.getByRole("button", { name: "Reload review snapshot" }));
    await user.click(await screen.findByRole("button", { name: "Reject version" }));
    await waitFor(() => expect(writes()).toHaveLength(2));
    expect(JSON.parse(String(writes()[1][1]?.body))).toMatchObject({ source: publicationRef, expectedHeadRevision: 20, decision: "rejected" });
  });

  it("keeps catalog loading, empty, error and partial read-only results distinct", async () => {
    capabilities = { enabled: true, actions: ["consume"], operatorReviewAvailable: false };
    const pending = deferred<Response>();
    fetch.mockImplementation((input, init) => input === "/api/publications?kind=agent" ? pending.promise : reply(input, init));
    const user = userEvent.setup();
    render(controls({ saved: null }));
    await user.click(await screen.findByText("Available publications"));
    expect(await screen.findByText("Loading publication catalog...")).toBeInTheDocument();
    expect(screen.queryByText("No published agents available to you.")).not.toBeInTheDocument();
    await act(async () => { pending.resolve(json({ items: [], truncated: false })); });
    expect(await screen.findByText("No published agents available to you.")).toBeInTheDocument();
    fetch.mockImplementation((input, init) => input === "/api/publications?kind=agent" ? Promise.reject(new TypeError("offline")) : reply(input, init));
    await user.click(screen.getByRole("button", { name: "Refresh publication catalog" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/catalog unavailable: offline/);
    expect(screen.queryByText("No published agents available to you.")).not.toBeInTheDocument();
    catalog = { items: [{ source: publicationRef, handle: "published-helper", displayName: "Shared helper", description: "Reviewed helper",
      visibility: "public", modes: ["chat"], modelIds: ["fixture-text"], skillMode: "versioned" }], truncated: true };
    fetch.mockImplementation(reply);
    await user.click(screen.getByRole("button", { name: "Retry publication catalog" }));
    expect(await screen.findByText("@published-helper")).toBeInTheDocument();
    expect(screen.getByText(/Tenant-visible\. Version 2/)).toBeInTheDocument();
    expect(screen.getByText(/Partial catalog/)).toBeInTheDocument();
    expect(callsTo(ownerPath)).toHaveLength(0);
    expect(callsTo(detailPath(publicationRef))).toHaveLength(0);
    expect(writes()).toHaveLength(0);
  });
});
