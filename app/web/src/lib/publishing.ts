import { ApiError } from "./api";
import { apiFetch } from "./auth";
import type { AssetVersionRef, DeploymentOption, UserAgent, Workflow } from "./types";

export type { AssetVersionRef } from "./types";
export type PublicationKind = AssetVersionRef["kind"];
export type PublicationAction = "submit" | "review" | "consume";
export type PublicationMode = "chat" | "workflow" | "delegation" | "voice" | "workflow_tool";
export type PublicationSkillMode = "versioned" | "excluded";
export type PublicationVisibility = "private" | "shared" | "public";
export type PublicationDecision = "approved" | "rejected";

export interface PublicationCapabilities {
  enabled: boolean;
  actions: PublicationAction[];
  operatorReviewAvailable: boolean;
}

export interface PublicationAudience {
  visibility: "shared" | "public";
  acl: string[];
  groupAcl: string[];
}

export interface PublicationHead {
  id: string;
  userId: string;
  tenantId: string;
  kind: PublicationKind;
  sourceName: string;
  sourceIncarnation?: string | null;
  assetId: string;
  handle: string;
  revision: number;
  versionCount: number;
  activeVersion?: number | null;
  pendingVersion?: number | null;
  visibility: PublicationVisibility;
  acl: string[];
  groupAcl: string[];
  deleted: boolean;
  reviewConsent: boolean;
  operatorReviewConsent: boolean;
  reviewerUserId?: string | null;
}

export interface PublicationReviewStatus {
  reviewDecision: PublicationDecision | null;
  reviewerId: string | null;
}

export interface OwnerPublicationState extends PublicationHead, PublicationReviewStatus {
  pendingSource: AssetVersionRef | null;
  pendingDraftRevision: number | null;
  activeSource: AssetVersionRef | null;
}

export interface PublicationSubmissionState {
  ownerId: string;
  sourceIncarnation: string | null;
  headRevision: number;
}

export interface PublicationSubmit {
  expectedRevision: number;
  audience: PublicationAudience;
  modelIds: string[];
  modes: PublicationMode[];
  reviewConsent: true;
  operatorReviewConsent: boolean;
  reviewerUserId?: string;
  skillMode: PublicationSkillMode;
}

export interface PublishedTool {
  name: string;
  alias: string;
  contractDigest: string;
  parametersDigest: string;
  description: string;
  descriptionTruncated: boolean;
  risk: string;
  scopes: string[];
  egress: string[];
  resources: Record<string, unknown>[];
}

export interface PublicationToolProfile {
  mode: PublicationMode;
  tools: PublishedTool[];
  digest: string;
  environmentDigest: string;
  requirements: Record<string, {
    required: string[];
    optional: Record<string, ("empty_document_scope" | "request_tools_disabled")[]>;
  }>;
  exclusions: "skills_excluded_by_author"[];
}

export interface PublicationVersion {
  id: string;
  userId: string;
  tenantId: string;
  kind: PublicationKind;
  assetId: string;
  version: number;
  digest: string;
  source: UserAgent | Workflow;
  sourceDigest: string;
  audience: PublicationAudience;
  modelBindings: {
    modelId: string;
    api: string;
    category: string;
    option: DeploymentOption & { modelVersion: string };
    requiredRealtimeProtocol?: "ga" | null;
    runtimeEnabled: boolean;
  }[];
  profiles: Partial<Record<PublicationMode, PublicationToolProfile>>;
  dependencies: {
    name: string;
    published: AssetVersionRef | null;
    curatedDigest: string | null;
  }[];
  reviewConsent: boolean;
  operatorReviewConsent: boolean;
  reviewerUserId?: string | null;
  submittedAt: string;
  policyDigest: string;
  skillMode: PublicationSkillMode;
}

export interface PublicationReviewSummary extends PublicationReviewStatus {
  source: AssetVersionRef;
  displayName: string;
  headRevision: number;
}

export interface PublicationReviewDetail extends PublicationReviewStatus {
  version: PublicationVersion;
  headRevision: number;
}

export interface PublicationReviewRequest {
  source: AssetVersionRef;
  expectedHeadRevision: number;
  decision: PublicationDecision;
  note: string;
}

export interface PublicationSummary {
  source: AssetVersionRef;
  handle: string;
  displayName: string;
  description: string;
  visibility: PublicationVisibility;
  modes: PublicationMode[];
  modelIds: string[];
  skillMode: PublicationSkillMode;
}

export interface PublicationList<T> {
  items: T[];
  truncated: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAction(value: unknown): value is PublicationAction {
  return value === "submit" || value === "review" || value === "consume";
}

export function isAssetVersionRef(value: unknown): value is AssetVersionRef {
  return isRecord(value) && (value.kind === "agent" || value.kind === "workflow") &&
    typeof value.ownerId === "string" && value.ownerId.length > 0 && value.ownerId.length <= 256 &&
    typeof value.assetId === "string" && /^[0-9a-f]{32}$/.test(value.assetId) &&
    typeof value.version === "number" && Number.isSafeInteger(value.version) && value.version >= 1 && value.version <= 20 &&
    typeof value.digest === "string" && /^[0-9a-f]{64}$/.test(value.digest);
}

export function sameAssetVersionRef(left: AssetVersionRef, right: AssetVersionRef): boolean {
  return left.kind === right.kind && left.ownerId === right.ownerId && left.assetId === right.assetId &&
    left.version === right.version && left.digest === right.digest;
}

function isIdentifier(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 256;
}

function isInteger(value: unknown, minimum: number, maximum = Number.MAX_SAFE_INTEGER): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= minimum && value <= maximum;
}

function isReviewStatus(value: Record<string, unknown>): value is Record<string, unknown> & PublicationReviewStatus {
  return value.reviewDecision === null ? value.reviewerId === null :
    (value.reviewDecision === "approved" || value.reviewDecision === "rejected") && isIdentifier(value.reviewerId);
}

function isOwnerState(value: unknown): value is OwnerPublicationState {
  if (!isRecord(value) || (value.kind !== "agent" && value.kind !== "workflow") ||
    !isIdentifier(value.id) || !isIdentifier(value.userId) || !isIdentifier(value.tenantId) ||
    !isIdentifier(value.sourceName) || !isIdentifier(value.handle) ||
    (value.sourceIncarnation !== null && (typeof value.sourceIncarnation !== "string" || !/^[0-9a-f]{32}$/.test(value.sourceIncarnation))) ||
    typeof value.assetId !== "string" || !/^[0-9a-f]{32}$/.test(value.assetId) ||
    !isInteger(value.revision, 1) || !isInteger(value.versionCount, 0, 20) ||
    (value.visibility !== "private" && value.visibility !== "shared" && value.visibility !== "public") ||
    !isStringList(value.acl) || !isStringList(value.groupAcl) || typeof value.deleted !== "boolean" ||
    typeof value.reviewConsent !== "boolean" || typeof value.operatorReviewConsent !== "boolean" ||
    (value.reviewerUserId !== null && !isIdentifier(value.reviewerUserId)) || !isReviewStatus(value) ||
    value.reviewerId === value.userId) return false;
  const versionCount = value.versionCount;
  const matches = (ref: unknown, version: unknown) => ref === null ? version === null :
    isAssetVersionRef(ref) && ref.kind === value.kind && ref.ownerId === value.userId &&
    ref.assetId === value.assetId && ref.version === version && isInteger(version, 1, versionCount);
  return matches(value.activeSource, value.activeVersion) && matches(value.pendingSource, value.pendingVersion) &&
    (value.pendingSource === null
      ? value.pendingDraftRevision === null && value.reviewDecision === null
      : value.pendingVersion === value.versionCount && isInteger(value.pendingDraftRevision, 0));
}

function ownerAcknowledgement(
  value: unknown, kind: PublicationKind, name: string, ownerId: string, revision: number,
): OwnerPublicationState {
  if (!isOwnerState(value) || value.kind !== kind || value.sourceName !== name ||
    value.userId !== ownerId || value.revision !== revision) {
    throw new Error("Publication acknowledgement does not match the requested owner or revision. Refresh before retrying.");
  }
  return value;
}

async function request<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  const response = await apiFetch(path, {
    cache: "no-store",
    signal,
    ...(body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  });
  if (!response.ok) {
    const payload: unknown = await response.json().catch((error: unknown) => {
      if (error instanceof SyntaxError) return null;
      throw error;
    });
    const detail = isRecord(payload) ? payload.detail : null;
    throw new ApiError(response.status, typeof detail === "string" ? detail :
      detail != null ? JSON.stringify(detail) : response.statusText || "Publication request failed.");
  }
  return response.json();
}

export async function getPublicationCapabilities(signal?: AbortSignal): Promise<PublicationCapabilities> {
  const value: unknown = await request("/api/publications/capabilities", signal);
  if (!isRecord(value) || typeof value.enabled !== "boolean" ||
    typeof value.operatorReviewAvailable !== "boolean" || !Array.isArray(value.actions) ||
    !value.actions.every(isAction)) {
    throw new Error("Publication availability is unknown: invalid capability response.");
  }
  return {
    enabled: value.enabled === true,
    actions: value.enabled === true ? value.actions : [],
    operatorReviewAvailable: value.enabled === true && value.operatorReviewAvailable === true,
  };
}

function ownerPath(kind: PublicationKind, name: string): string {
  return `/api/publications/${kind}/${encodeURIComponent(name)}`;
}

export async function getOwnerPublication(
  kind: PublicationKind, name: string, signal?: AbortSignal,
): Promise<OwnerPublicationState | null> {
  const head: unknown = await request(ownerPath(kind, name), signal);
  if (head === null) return null;
  if (!isOwnerState(head) || head.kind !== kind || head.sourceName !== name) {
    throw new Error("Publication state is stale or invalid. Refresh before making changes.");
  }
  return head;
}

export async function submitPublication(
  kind: PublicationKind, name: string, input: PublicationSubmit,
  expected: PublicationSubmissionState, signal?: AbortSignal,
): Promise<OwnerPublicationState> {
  const value: unknown = await request(`${ownerPath(kind, name)}/submit`, signal, input);
  const head = ownerAcknowledgement(value, kind, name, expected.ownerId, expected.headRevision + 1);
  if (head.deleted || head.sourceIncarnation !== expected.sourceIncarnation || head.pendingSource === null ||
    head.pendingDraftRevision !== input.expectedRevision || head.reviewConsent !== true ||
    head.operatorReviewConsent !== input.operatorReviewConsent || head.reviewerUserId !== (input.reviewerUserId ?? null)) {
    throw new Error("Publication acknowledgement does not match the submitted draft or review consent. Refresh before retrying.");
  }
  return head;
}

export async function activatePublication(
  kind: PublicationKind, name: string,
  input: { source: AssetVersionRef; expectedHeadRevision: number }, signal?: AbortSignal,
): Promise<OwnerPublicationState> {
  const value: unknown = await request(`${ownerPath(kind, name)}/activate`, signal, input);
  const head = ownerAcknowledgement(value, kind, name, input.source.ownerId, input.expectedHeadRevision + 1);
  if (head.deleted || head.activeSource === null || !sameAssetVersionRef(head.activeSource, input.source) ||
    head.pendingSource !== null || head.visibility === "private") {
    throw new Error("Publication acknowledgement does not activate the exact reviewed version. Refresh before retrying.");
  }
  return head;
}

export async function withdrawPublication(
  kind: PublicationKind, name: string, expected: Pick<PublicationHead, "revision" | "userId" | "assetId">,
  signal?: AbortSignal,
): Promise<OwnerPublicationState> {
  const value: unknown = await request(`${ownerPath(kind, name)}/withdraw`, signal, { expectedHeadRevision: expected.revision });
  const head = ownerAcknowledgement(value, kind, name, expected.userId, expected.revision + 1);
  if (head.assetId !== expected.assetId || head.activeSource !== null || head.pendingSource !== null ||
    head.visibility !== "private" || head.acl.length !== 0 || head.groupAcl.length !== 0) {
    throw new Error("Publication acknowledgement does not withdraw the requested asset. Refresh before retrying.");
  }
  return head;
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

async function requestList<T>(path: string, valid: (item: unknown) => item is T, signal?: AbortSignal): Promise<PublicationList<T>> {
  const value: unknown = await request(path, signal);
  if (!isRecord(value) || !Array.isArray(value.items) || value.items.length > 100 ||
    !value.items.every(valid) || typeof value.truncated !== "boolean") {
    throw new Error("Publication list is unavailable: incomplete or invalid response.");
  }
  return { items: value.items, truncated: value.truncated };
}

function isReviewSummary(value: unknown): value is PublicationReviewSummary {
  return isRecord(value) && isAssetVersionRef(value.source) && typeof value.displayName === "string" &&
    isInteger(value.headRevision, 1) && isReviewStatus(value) && value.reviewerId !== value.source.ownerId;
}

export function listPublicationReviews(
  kind: PublicationKind, signal?: AbortSignal,
): Promise<PublicationList<PublicationReviewSummary>> {
  return requestList(`/api/publication-reviews?kind=${kind}`, isReviewSummary, signal);
}

export async function getPublicationReview(
  source: AssetVersionRef, signal?: AbortSignal,
): Promise<PublicationReviewDetail> {
  if (!isAssetVersionRef(source)) throw new Error("Invalid publication version reference.");
  const detail = await request<PublicationReviewDetail>(
    `/api/publication-reviews/${source.kind}/${encodeURIComponent(source.ownerId)}/${source.assetId}/${source.version}?digest=${source.digest}`,
    signal,
  );
  if (!isRecord(detail) || !isReviewStatus(detail)) {
    throw new Error("Review response is stale or incomplete. Reload the exact submitted version.");
  }
  const version = detail.version;
  if (detail.reviewerId === source.ownerId ||
    !version || version.kind !== source.kind || version.userId !== source.ownerId ||
    version.assetId !== source.assetId || version.version !== source.version || version.digest !== source.digest ||
    !Number.isSafeInteger(detail.headRevision) || detail.headRevision < 1 ||
    !version.source || typeof version.source.displayName !== "string" || version.reviewConsent !== true ||
    !version.audience || !isStringList(version.audience.acl) || !isStringList(version.audience.groupAcl) ||
    !["shared", "public"].includes(version.audience.visibility) || !Array.isArray(version.modelBindings) ||
    !isRecord(version.profiles) || !Array.isArray(version.dependencies) ||
    (version.skillMode !== "versioned" && version.skillMode !== "excluded")) {
    throw new Error("Review response is stale or incomplete. Reload the exact submitted version.");
  }
  return detail;
}

export async function decidePublicationReview(
  input: PublicationReviewRequest, signal?: AbortSignal,
): Promise<PublicationReviewSummary> {
  const value: unknown = await request("/api/publication-reviews/decision", signal, input);
  if (!isReviewSummary(value) || !sameAssetVersionRef(value.source, input.source) ||
    value.headRevision !== input.expectedHeadRevision + 1 || value.reviewDecision !== input.decision) {
    throw new Error("Review acknowledgement does not match the exact version and decision. Reload before retrying.");
  }
  return value;
}

function isSummary(value: unknown): value is PublicationSummary {
  return isRecord(value) && isAssetVersionRef(value.source) && typeof value.handle === "string" &&
    typeof value.displayName === "string" && typeof value.description === "string" &&
    (value.visibility === "private" || value.visibility === "shared" || value.visibility === "public") &&
    isStringList(value.modelIds) && isStringList(value.modes) &&
    value.modes.every((mode) => ["chat", "workflow", "delegation", "voice", "workflow_tool"].includes(mode)) &&
    (value.skillMode === "versioned" || value.skillMode === "excluded");
}

export function listPublications(
  kind: PublicationKind, signal?: AbortSignal,
): Promise<PublicationList<PublicationSummary>> {
  return requestList(`/api/publications?kind=${kind}`, isSummary, signal);
}

export function publicationVisibilityLabel(visibility: PublicationVisibility): string {
  return visibility === "public" ? "Tenant-visible" : visibility === "shared" ? "Shared" : visibility === "private" ? "Private" : "Unknown";
}

export function publicationAudience(
  visibility: PublicationAudience["visibility"], emails: string, groups: string,
): PublicationAudience {
  if (visibility === "public") return { visibility, acl: [], groupAcl: [] };
  const split = (value: string) => value.split(/[,;\n]/).map((item) => item.trim()).filter(Boolean);
  const acl = [...new Set(split(emails).map((email) => email.toLowerCase()))];
  const groupAcl = split(groups);
  if (acl.length + groupAcl.length === 0) throw new Error("Shared publication needs at least one email or group recipient.");
  if (acl.length > 100 || groupAcl.length > 100) throw new Error("Use at most 100 emails and 100 group IDs.");
  if (new Set(groupAcl).size !== groupAcl.length ||
    groupAcl.some((group) => !/^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/.test(group))) {
    throw new Error("Group recipients must be exact, unique group object GUIDs. No directory lookup is performed.");
  }
  return { visibility, acl, groupAcl };
}
