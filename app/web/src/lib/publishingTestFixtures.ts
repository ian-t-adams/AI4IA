import type { AssetVersionRef, ModelEntry, UserAgent } from "./types";
import type { OwnerPublicationState, PublicationReviewDetail } from "./publishing";

export const publicationAgent: UserAgent = {
  id: "helper", userId: "owner", name: "helper", displayName: "Helper", description: "Synthetic agent",
  systemPrompt: "Be helpful.", defaultModel: "fixture-text", tools: [], links: [], enabled: true,
  revision: 7, incarnation: "a".repeat(32), createdAt: "2026-09-01T00:00:00Z", updatedAt: "2026-09-10T00:00:00Z",
};

export const publicationModels: ModelEntry[] = [{
  id: "fixture-text", displayName: "Text model", category: "chat", format: "OpenAI", api: "responses",
  conversational: true, contextWindow: 32000, maxOutputTokens: 4000, supportsSampling: true, reasoningEffortOptions: [],
  options: [{ region: "test-region", dataZone: null, sku: "Standard", deploymentName: "fixture-text", modelVersion: "2026-01-01", residency: "global" }],
}, {
  id: "fixture-embedding", displayName: "Embedding model", category: "embedding", format: "OpenAI", api: "embeddings",
  conversational: false, contextWindow: null, maxOutputTokens: null, supportsSampling: false, reasoningEffortOptions: [],
  options: [{ region: "test-region", dataZone: null, sku: "Standard", deploymentName: "fixture-embedding", modelVersion: "1", residency: "global" }],
}];

export const publicationRef: AssetVersionRef = {
  kind: "agent", ownerId: "owner", assetId: "b".repeat(32), version: 2, digest: "c".repeat(64),
};

export const publicationHead: OwnerPublicationState = {
  id: "publication-head", userId: "owner", tenantId: "tenant", kind: "agent", sourceName: "helper",
  sourceIncarnation: publicationAgent.incarnation, assetId: publicationRef.assetId, handle: "published-helper",
  revision: 11, versionCount: 2, activeVersion: null, pendingVersion: 2, visibility: "private", acl: [], groupAcl: [],
  deleted: false, reviewConsent: true, operatorReviewConsent: false, reviewerUserId: null,
  pendingSource: publicationRef, pendingDraftRevision: 7, activeSource: null, reviewDecision: null, reviewerId: null,
};

export const publicationReview: PublicationReviewDetail = {
  headRevision: 13,
  reviewDecision: null, reviewerId: null,
  version: {
    id: "immutable-version", userId: "owner", tenantId: "tenant", kind: "agent", assetId: publicationRef.assetId,
    version: publicationRef.version, digest: publicationRef.digest, source: publicationAgent, sourceDigest: "d".repeat(64),
    audience: { visibility: "public", acl: [], groupAcl: [] },
    modelBindings: [{
      modelId: "fixture-text", api: "responses", category: "chat",
      option: { ...publicationModels[0].options[0], modelVersion: "2026-01-01" },
      requiredRealtimeProtocol: null, runtimeEnabled: true,
    }],
    profiles: {
      chat: {
        mode: "chat", digest: "e".repeat(64), environmentDigest: "f".repeat(64), exclusions: [],
        tools: [{
          name: "fetch_document", alias: "document", description: "Read scoped documents.", descriptionTruncated: false,
          contractDigest: "1".repeat(64), parametersDigest: "2".repeat(64), risk: "read",
          scopes: ["documents.read"], egress: ["https://documents.example.com"], resources: [{ kind: "document", version: "3" }],
        }],
        requirements: { root: { required: ["document"], optional: { search: ["empty_document_scope"] } } },
      },
    },
    dependencies: [], reviewConsent: true, operatorReviewConsent: false, reviewerUserId: "reviewer",
    submittedAt: "2026-09-10T00:00:00Z", policyDigest: "3".repeat(64), skillMode: "versioned",
  },
};

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((done, fail) => { resolve = done; reject = fail; });
  return { promise, resolve, reject };
}
