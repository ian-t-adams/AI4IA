// Browser client for custom photo avatars (Phase 1). The types mirror the
// published HTTP contract in app/api/src/ai4ia_api/photo_avatars/models.py, and
// every call goes through apiFetch so the Entra bearer token rides along.
//
// The API is authoritative. GET /config is display posture only: the server
// re-checks the feature gate, policy, limits, cost and attestation on every
// request, so nothing here grants or enforces anything. The gallery and later
// consumers (the voice-settings avatar picker) share these helpers.
import { apiFetch } from "./auth";

export const PHOTO_AVATARS_PATH = "/api/photo-avatars";

export const PHOTO_AVATAR_STATUSES = [
  "creating",
  "generating",
  "confirming",
  "ready",
  "failed",
  "deleting",
] as const;
export type PhotoAvatarStatus = (typeof PHOTO_AVATAR_STATUSES)[number];

export type PhotoAvatarFailureCode =
  | "provider_rejected"
  | "provider_failed"
  | "provider_throttled"
  | "provider_forbidden"
  | "not_created"
  | "provider_missing"
  | "preview_rejected"
  | "project_unavailable";

export const PHOTO_AVATAR_AVAILABILITY_REASONS = [
  "available",
  "disabled",
  "storage_unavailable",
  "residency_unsupported",
  "policy_denied",
  "policy_unavailable",
  "capability_unavailable",
  "capability_unknown",
] as const;
export type PhotoAvatarAvailabilityReason =
  (typeof PHOTO_AVATAR_AVAILABILITY_REASONS)[number];

export type PhotoAvatarReportReason =
  | "impersonation"
  | "minor"
  | "sexual"
  | "hateful"
  | "violent"
  | "other";

// The three statements the create request can carry. A server that lists any
// other statement cannot be attested to by this client, so creation stays off.
export const PHOTO_AVATAR_ATTESTATION_IDS = [
  "fictional",
  "adult",
  "notRealPerson",
] as const;
export type PhotoAvatarAttestationStatementId =
  (typeof PHOTO_AVATAR_ATTESTATION_IDS)[number];

// Display order in the create form: the least sensitive attribute first.
export const PHOTO_AVATAR_ATTRIBUTE_KEYS = [
  "style",
  "age",
  "gender",
  "ethnicity",
] as const;
export type PhotoAvatarAttributeKey =
  (typeof PHOTO_AVATAR_ATTRIBUTE_KEYS)[number];

export interface PhotoAvatarAttributes {
  gender: string | null;
  age: string | null;
  ethnicity: string | null;
  style: string | null;
}

export interface PhotoAvatarFailure {
  code: PhotoAvatarFailureCode;
  message: string;
}

export interface PhotoAvatarPreviewInfo {
  url: string;
  contentType: "image/png";
  width: number;
  height: number;
  bytes: number;
}

export interface PhotoAvatarDisclosure {
  aiGenerated: true;
  label: string;
}

export interface PhotoAvatarCost {
  currency: string;
  estimatedUsd: number | null;
  known: boolean;
  priceVersion: string | null;
  basis: "per_avatar";
}

export interface PhotoAvatar {
  id: string;
  displayName: string;
  prompt: string;
  attributes: PhotoAvatarAttributes;
  status: PhotoAvatarStatus;
  failure: PhotoAvatarFailure | null;
  preview: PhotoAvatarPreviewInfo | null;
  disclosure: PhotoAvatarDisclosure;
  cost: PhotoAvatarCost;
  usable: boolean;
  reported: boolean;
  /**
   * A live session reported that this avatar failed verification. While true,
   * `usable` is false; a status read after the server's cooldown re-checks it
   * with the provider. Optional so a payload without it still reads as false.
   */
  needsReverification?: boolean;
  createdAt: string;
  updatedAt: string;
  readyAt: string | null;
}

export interface PhotoAvatarLimits {
  maxAvatars: number;
  avatarCount: number;
  maxCreationsPerDay: number;
  creationsInLastDay: number;
  nextCreationAt: string | null;
  promptMaxChars: number;
  displayNameMaxChars: number;
}

export type PhotoAvatarAttributeOptions = Record<PhotoAvatarAttributeKey, string[]>;

export interface PhotoAvatarAttestationStatement {
  id: string;
  text: string;
}

export interface PhotoAvatarAttestationInfo {
  version: string;
  statements: PhotoAvatarAttestationStatement[];
}

export interface PhotoAvatarDisclosureInfo {
  label: string;
  text: string;
}

export interface PhotoAvatarPricing {
  currency: string;
  estimatedUsdPerAvatar: number | null;
  known: boolean;
  priceVersion: string | null;
}

export interface PhotoAvatarFeedback {
  reasons: PhotoAvatarReportReason[];
  detailsMaxChars: number;
  microsoftReportUrl: string;
}

// While the feature is off only the first four fields are set.
export interface PhotoAvatarConfig {
  enabled: boolean;
  available: boolean;
  reason: PhotoAvatarAvailabilityReason;
  canCreate: boolean;
  limits: PhotoAvatarLimits | null;
  attributes: PhotoAvatarAttributeOptions | null;
  attestation: PhotoAvatarAttestationInfo | null;
  disclosure: PhotoAvatarDisclosureInfo | null;
  pricing: PhotoAvatarPricing | null;
  feedback: PhotoAvatarFeedback | null;
}

export interface CreatePhotoAvatarRequest {
  displayName: string;
  prompt: string;
  gender?: string;
  age?: string;
  ethnicity?: string;
  style?: string;
  attestation: {
    version: string;
    fictional: true;
    adult: true;
    notRealPerson: true;
  };
}

export interface PhotoAvatarReportRequest {
  reason: PhotoAvatarReportReason;
  details?: string;
}

export interface PhotoAvatarReportReceipt {
  id: string;
  avatarId: string;
  reason: PhotoAvatarReportReason;
  createdAt: string;
}

// ---------------------------------------------------------------------------
// Identity and the preview guard
// ---------------------------------------------------------------------------

const RECORD_ID = /^[0-9a-f]{32}$/;
const PREVIEW_PATH = /^\/api\/photo-avatars\/[0-9a-f]{32}\/preview$/;

export function isPhotoAvatarId(value: unknown): value is string {
  return typeof value === "string" && RECORD_ID.test(value);
}

function recordPath(id: string): string {
  // Never build a request path from an unvalidated id: the opaque 32-hex shape
  // is the only form the API accepts, and it cannot carry a separator.
  if (!isPhotoAvatarId(id)) throw new TypeError("Invalid photo avatar id.");
  return `${PHOTO_AVATARS_PATH}/${id}`;
}

/**
 * The only preview source this client loads: the record's own authenticated
 * API route, exactly as the contract publishes it. Anything else a record might
 * carry (another origin, a provider or Blob SAS link, a protocol-relative URL,
 * another record's route, a query or a fragment) yields null, and callers
 * render no image at all.
 */
export function photoAvatarPreviewPath(
  avatar: Pick<PhotoAvatar, "id" | "preview">,
): string | null {
  const url = avatar.preview?.url;
  if (!isPhotoAvatarId(avatar.id) || typeof url !== "string") return null;
  const expected = `${recordPath(avatar.id)}/preview`;
  return url === expected ? expected : null;
}

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

// Seven days bounds a malformed header; real waits are at most a day.
const MAX_RETRY_AFTER_SECONDS = 7 * 24 * 60 * 60;

/** Parses `Retry-After` (delta-seconds or an HTTP date) into whole seconds. */
export function parseRetryAfter(
  value: string | null | undefined,
  now: number = Date.now(),
): number | null {
  const trimmed = value?.trim();
  if (!trimmed) return null;
  let seconds: number;
  if (/^\d+$/.test(trimmed)) {
    seconds = Number(trimmed);
  } else {
    // Only an HTTP date (a day or month name plus a time) is read as a date;
    // Date.parse alone accepts loose strings such as "-5" or "1.5".
    const at = /[A-Za-z]/.test(trimmed) && /\d{1,2}:\d{2}/.test(trimmed) ? Date.parse(trimmed) : NaN;
    if (Number.isNaN(at)) return null;
    seconds = Math.ceil((at - now) / 1000);
  }
  return Math.min(Math.max(seconds, 0), MAX_RETRY_AFTER_SECONDS);
}

function isAvailabilityReason(value: unknown): value is PhotoAvatarAvailabilityReason {
  return (
    typeof value === "string" &&
    (PHOTO_AVATAR_AVAILABILITY_REASONS as readonly string[]).includes(value)
  );
}

/** A refusal carrying the shared `{detail, code, correlation_id}` body. */
export class PhotoAvatarApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly detail: string;
  readonly retryAfterSeconds: number | null;
  readonly reason: PhotoAvatarAvailabilityReason | null;
  readonly correlationId: string | null;

  constructor(init: {
    status: number;
    code?: string | null;
    detail: string;
    retryAfterSeconds?: number | null;
    reason?: PhotoAvatarAvailabilityReason | null;
    correlationId?: string | null;
  }) {
    super(`${init.status}: ${init.detail}`);
    this.name = "PhotoAvatarApiError";
    this.status = init.status;
    this.code = init.code ?? null;
    this.detail = init.detail;
    this.retryAfterSeconds = init.retryAfterSeconds ?? null;
    this.reason = init.reason ?? null;
    this.correlationId = init.correlationId ?? null;
  }
}

async function errorFrom(resp: Response): Promise<PhotoAvatarApiError> {
  let body: Record<string, unknown> = {};
  try {
    const parsed: unknown = await resp.json();
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      body = parsed as Record<string, unknown>;
    }
  } catch {
    // A proxy or gateway may answer with a non-JSON body.
  }
  const detail =
    typeof body.detail === "string" && body.detail.trim()
      ? body.detail
      : resp.statusText || `Request failed (${resp.status}).`;
  return new PhotoAvatarApiError({
    status: resp.status,
    code: typeof body.code === "string" ? body.code : null,
    detail,
    retryAfterSeconds: parseRetryAfter(resp.headers.get("Retry-After")),
    reason: isAvailabilityReason(body.reason) ? body.reason : null,
    correlationId:
      typeof body.correlation_id === "string" ? body.correlation_id : null,
  });
}

async function jsonOrThrow<T>(resp: Response): Promise<T> {
  if (!resp.ok) throw await errorFrom(resp);
  return (await resp.json()) as T;
}

function isPhotoAvatarRecord(value: unknown): value is PhotoAvatar {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<PhotoAvatar>;
  return (
    isPhotoAvatarId(record.id) &&
    typeof record.displayName === "string" &&
    typeof record.status === "string"
  );
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

export async function getPhotoAvatarConfig(
  signal?: AbortSignal,
): Promise<PhotoAvatarConfig> {
  return jsonOrThrow(
    await apiFetch(`${PHOTO_AVATARS_PATH}/config`, { cache: "no-store", signal }),
  );
}

export async function listPhotoAvatars(
  signal?: AbortSignal,
): Promise<PhotoAvatar[]> {
  const body = await jsonOrThrow<{ avatars?: unknown }>(
    await apiFetch(PHOTO_AVATARS_PATH, { cache: "no-store", signal }),
  );
  // A malformed body is an unavailable list, never a successful empty one.
  if (!Array.isArray(body?.avatars)) {
    throw new PhotoAvatarApiError({
      status: 502,
      code: "invalid_response",
      detail: "The avatar list could not be read.",
    });
  }
  return body.avatars.filter(isPhotoAvatarRecord);
}

export async function createPhotoAvatar(
  request: CreatePhotoAvatarRequest,
): Promise<PhotoAvatar> {
  return jsonOrThrow(
    await apiFetch(PHOTO_AVATARS_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    }),
  );
}

export async function getPhotoAvatar(
  id: string,
  signal?: AbortSignal,
): Promise<PhotoAvatar> {
  return jsonOrThrow(await apiFetch(recordPath(id), { cache: "no-store", signal }));
}

export async function deletePhotoAvatar(id: string): Promise<void> {
  const resp = await apiFetch(recordPath(id), { method: "DELETE" });
  if (!resp.ok) throw await errorFrom(resp);
}

export async function reportPhotoAvatar(
  id: string,
  request: PhotoAvatarReportRequest,
): Promise<PhotoAvatarReportReceipt> {
  return jsonOrThrow(
    await apiFetch(`${recordPath(id)}/reports`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(request),
    }),
  );
}

/**
 * Fetches preview bytes from a path returned by `photoAvatarPreviewPath`. An
 * image element cannot carry the bearer token, so callers wrap the Blob in an
 * object URL. Any other path is refused before a request is made.
 */
export async function fetchPhotoAvatarPreview(
  path: string,
  signal?: AbortSignal,
): Promise<Blob> {
  if (!PREVIEW_PATH.test(path)) {
    throw new TypeError("Refusing a preview source outside the photo avatar API.");
  }
  const resp = await apiFetch(path, { signal });
  if (!resp.ok) throw await errorFrom(resp);
  const type = resp.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
  if (type !== "image/png") {
    throw new PhotoAvatarApiError({
      status: 502,
      code: "invalid_preview",
      detail: "The preview was not a PNG image.",
    });
  }
  return resp.blob();
}

// ---------------------------------------------------------------------------
// Create form validation (the attestation gate)
// ---------------------------------------------------------------------------

export interface PhotoAvatarDraft {
  displayName: string;
  prompt: string;
  /** "" (or absent) means unspecified. */
  attributes: Partial<Record<PhotoAvatarAttributeKey, string>>;
  attested: Partial<Record<string, boolean>>;
}

export type PhotoAvatarDraftIssue =
  | "config"
  | "displayName"
  | "displayNameTooLong"
  | "prompt"
  | "promptTooLong"
  | "attribute"
  | "attestationUnrecognized"
  | "attestation";

/** True when the server lists exactly the three statements this client sends. */
export function attestationRecognized(
  attestation: PhotoAvatarAttestationInfo | null | undefined,
): attestation is PhotoAvatarAttestationInfo {
  if (!attestation?.version || !Array.isArray(attestation.statements)) return false;
  const ids = attestation.statements.map((statement) => statement.id);
  return (
    ids.length === PHOTO_AVATAR_ATTESTATION_IDS.length &&
    PHOTO_AVATAR_ATTESTATION_IDS.every((id) => ids.includes(id))
  );
}

/**
 * Builds the create request, or explains why it cannot be sent yet. The
 * request exists only when every server-listed statement is attested; the
 * version sent is the one the server published in /config.
 */
export function validatePhotoAvatarDraft(
  config: PhotoAvatarConfig | null,
  draft: PhotoAvatarDraft,
): { request: CreatePhotoAvatarRequest | null; issues: PhotoAvatarDraftIssue[] } {
  const limits = config?.limits;
  const options = config?.attributes;
  if (!limits || !options) return { request: null, issues: ["config"] };
  const issues: PhotoAvatarDraftIssue[] = [];
  const displayName = draft.displayName.trim();
  const prompt = draft.prompt.trim();
  if (!displayName) issues.push("displayName");
  else if (displayName.length > limits.displayNameMaxChars) issues.push("displayNameTooLong");
  if (!prompt) issues.push("prompt");
  else if (prompt.length > limits.promptMaxChars) issues.push("promptTooLong");

  const attributes: Partial<Record<PhotoAvatarAttributeKey, string>> = {};
  for (const key of PHOTO_AVATAR_ATTRIBUTE_KEYS) {
    const value = draft.attributes[key];
    if (!value) continue;
    if (!options[key]?.includes(value)) {
      if (!issues.includes("attribute")) issues.push("attribute");
      continue;
    }
    attributes[key] = value;
  }

  const attestation = config.attestation;
  if (!attestationRecognized(attestation)) {
    return { request: null, issues: [...issues, "attestationUnrecognized"] };
  }
  if (!attestation.statements.every((statement) => draft.attested[statement.id] === true)) {
    issues.push("attestation");
  }
  if (issues.length > 0) return { request: null, issues };
  return {
    request: {
      displayName,
      prompt,
      ...attributes,
      attestation: {
        version: attestation.version,
        fictional: true,
        adult: true,
        notRealPerson: true,
      },
    },
    issues,
  };
}

// ---------------------------------------------------------------------------
// Status, polling and display helpers
// ---------------------------------------------------------------------------

const PENDING = new Set<string>(["creating", "generating", "confirming"]);

/** Creation is still in flight; the record is worth polling. */
export function isPendingPhotoAvatar(avatar: Pick<PhotoAvatar, "status">): boolean {
  return PENDING.has(avatar.status);
}

/** Ready, but live use is refused until the server re-verifies it. */
export function isReverifyingPhotoAvatar(
  avatar: Pick<PhotoAvatar, "status" | "needsReverification">,
): boolean {
  return avatar.status === "ready" && avatar.needsReverification === true;
}

export function isKnownPhotoAvatarStatus(status: string): status is PhotoAvatarStatus {
  return (PHOTO_AVATAR_STATUSES as readonly string[]).includes(status);
}

// Creation took about 30-45 seconds when verified, so polling starts quickly,
// backs off, and stops for good after three minutes of wall time.
export const PHOTO_AVATAR_POLL_DELAYS_MS = [2_000, 3_000, 5_000, 8_000, 10_000, 15_000];
export const PHOTO_AVATAR_POLL_BUDGET_MS = 180_000;

export function photoAvatarPollDelay(attempt: number): number {
  const index = Math.min(Math.max(0, attempt), PHOTO_AVATAR_POLL_DELAYS_MS.length - 1);
  return PHOTO_AVATAR_POLL_DELAYS_MS[index];
}

// Only a status read re-verifies a flagged avatar, and the server does it at
// most once per five-minute cooldown. So a flagged record is read at once, then
// once a minute, for a little longer than one cooldown.
export const PHOTO_AVATAR_REVERIFY_POLL_MS = 60_000;
export const PHOTO_AVATAR_REVERIFY_BUDGET_MS = 6 * 60_000;

/** Keeps the newer of two copies of a record, so a late read never regresses it. */
export function newerPhotoAvatar(current: PhotoAvatar, incoming: PhotoAvatar): PhotoAvatar {
  const currentAt = Date.parse(current.updatedAt);
  const incomingAt = Date.parse(incoming.updatedAt);
  if (Number.isFinite(currentAt) && Number.isFinite(incomingAt) && incomingAt < currentAt) {
    return current;
  }
  return incoming;
}

export const PHOTO_AVATAR_STATUS_TEXT: Record<PhotoAvatarStatus, string> = {
  creating: "Starting…",
  generating: "Generating…",
  confirming: "Checking status…",
  ready: "Ready",
  failed: "Couldn't create",
  deleting: "Deletion unfinished",
};

export function photoAvatarStatusText(status: string): string {
  return isKnownPhotoAvatarStatus(status) ? PHOTO_AVATAR_STATUS_TEXT[status] : "Status unknown";
}

export const PHOTO_AVATAR_REVERIFYING_TEXT = "Re-verifying…";

/**
 * The status to show for one record: "Re-verifying…" for a ready avatar that is
 * waiting on re-verification, "No longer available" for one that failed after
 * it had been ready, and the plain status text otherwise.
 */
export function photoAvatarDisplayStatus(
  avatar: Pick<PhotoAvatar, "status" | "needsReverification" | "readyAt">,
): string {
  if (isReverifyingPhotoAvatar(avatar)) return PHOTO_AVATAR_REVERIFYING_TEXT;
  if (avatar.status === "failed" && avatar.readyAt) return "No longer available";
  return photoAvatarStatusText(avatar.status);
}

export const PHOTO_AVATAR_UNAVAILABLE_TEXT: Record<
  Exclude<PhotoAvatarAvailabilityReason, "available">,
  string
> = {
  disabled: "Photo avatars are turned off in this deployment.",
  storage_unavailable:
    "Avatar storage is unavailable right now, so new avatars can't be created. Try again later.",
  residency_unsupported:
    "Photo avatars aren't offered under this deployment's data residency settings.",
  policy_denied:
    "Your organization's access policy doesn't allow you to create photo avatars.",
  policy_unavailable:
    "Your access policy couldn't be checked, so creation is paused. Try again later.",
  capability_unavailable:
    "Creating avatars needs Microsoft's Limited Access approval for custom avatars, and that approval isn't in place for this deployment.",
  capability_unknown:
    "The avatar service's approval couldn't be confirmed, so creation is paused. Try again later.",
};

export function photoAvatarUnavailableText(reason: string | null | undefined): string {
  if (isAvailabilityReason(reason) && reason !== "available") {
    return PHOTO_AVATAR_UNAVAILABLE_TEXT[reason];
  }
  return "Creating avatars is unavailable right now.";
}

export const PHOTO_AVATAR_ATTRIBUTE_LABELS: Record<PhotoAvatarAttributeKey, string> = {
  style: "Style",
  age: "Age",
  gender: "Gender",
  ethnicity: "Ethnicity",
};

const ATTRIBUTE_VALUE_LABELS: Record<string, string> = {
  Realistic: "Realistic",
  DigitalIllustration: "Digital illustration",
  Stylized3D: "Stylized 3D",
  YoungAdult: "Young adult",
  MiddleAged: "Middle-aged",
  Senior: "Senior",
  Male: "Male",
  Female: "Female",
  Asian: "Asian",
  White: "White",
  BlackAndAfricanAmerican: "Black and African American",
  SouthAsian: "South Asian",
  MiddleEastern: "Middle Eastern",
  HispanicAndLatinx: "Hispanic and Latinx",
};

/** Human label for a provider enum value such as "YoungAdult". */
export function photoAvatarAttributeLabel(value: string): string {
  const known = ATTRIBUTE_VALUE_LABELS[value];
  if (known) return known;
  const words = value
    .replace(/([a-z])([A-Z0-9])/g, "$1 $2")
    .replace(/([A-Z])([A-Z][a-z])/g, "$1 $2")
    .trim()
    .toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : value;
}

export const PHOTO_AVATAR_REPORT_REASON_LABELS: Record<PhotoAvatarReportReason, string> = {
  impersonation: "Looks like a real or identifiable person",
  minor: "Looks like a minor",
  sexual: "Sexual content",
  hateful: "Hateful content",
  violent: "Violent content",
  other: "Something else",
};

export function photoAvatarReportReasonLabel(reason: string): string {
  return (
    PHOTO_AVATAR_REPORT_REASON_LABELS[reason as PhotoAvatarReportReason] ??
    photoAvatarAttributeLabel(reason)
  );
}

/** A localized amount, or null when the price is unknown (never a zero). */
export function formatPhotoAvatarPrice(
  amount: number | null | undefined,
  currency: string | null | undefined,
  known: boolean,
): string | null {
  if (!known || typeof amount !== "number" || !Number.isFinite(amount) || amount < 0) {
    return null;
  }
  const code = currency || "USD";
  try {
    return new Intl.NumberFormat(undefined, { style: "currency", currency: code }).format(amount);
  } catch {
    return `${code} ${amount.toFixed(2)}`;
  }
}

export function formatPhotoAvatarTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return null;
  return at.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

/** "in 7 seconds", "in about 12 minutes" or "after Sep 26, 2026, 3:45 PM". */
export function retryAfterPhrase(seconds: number, now: number = Date.now()): string {
  const whole = Math.max(1, Math.ceil(seconds));
  if (whole < 90) return `in ${whole} second${whole === 1 ? "" : "s"}`;
  if (whole < 90 * 60) return `in about ${Math.round(whole / 60)} minutes`;
  return `after ${formatPhotoAvatarTime(new Date(now + whole * 1000).toISOString())}`;
}

/** Only an https link to the provider's report form is ever rendered. */
export function safeReportUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
  } catch {
    return null;
  }
}

/** User-facing text for a failed photo avatar call, naming the recovery. */
export function photoAvatarErrorMessage(error: unknown, now: number = Date.now()): string {
  if (!(error instanceof PhotoAvatarApiError)) {
    return "The request didn't complete. Check your connection, then refresh to see the current state before trying again.";
  }
  const wait = error.retryAfterSeconds;
  const later = (text: string) => (wait !== null ? `${text} ${retryAfterPhrase(wait, now)}.` : `${text} later.`);
  switch (error.code) {
    case "avatar_limit_reached":
      return "You've reached your avatar limit. Delete an avatar to create another.";
    case "daily_creation_limit":
      return later("You've reached the creation limit for the last 24 hours. You can create another");
    case "photo_avatars_unavailable":
      return photoAvatarUnavailableText(error.reason);
    case "photo_avatars_disabled":
      return PHOTO_AVATAR_UNAVAILABLE_TEXT.disabled;
    case "cost_unknown_under_cap":
      return "Creation is paused because the cost of an avatar is unknown and your usage has a spending cap.";
    case "policy_denied":
      return "Your organization's access policy doesn't allow this.";
    case "policy_unavailable":
      return "Your access policy couldn't be checked. Try again later.";
    case "hard_quota_refused":
      return later("Your usage quota doesn't allow this right now. Try again");
    case "attestation_outdated":
      return "The confirmation statements changed. Review them and confirm again.";
    case "avatar_confirming":
      return later("This avatar's creation is still being confirmed. You can delete it");
    case "provider_delete_failed":
      return "The avatar service didn't confirm the deletion. Delete it again to finish.";
    case "report_limit":
      return later("You've sent several reports recently. You can send another");
    case "not_found":
      return "This avatar no longer exists.";
    case "validation_error":
    case "invalid_photo_avatar_request":
      return "Check the name, description and options, then try again.";
    default:
      return wait !== null ? `${error.detail} Try again ${retryAfterPhrase(wait, now)}.` : error.detail;
  }
}
