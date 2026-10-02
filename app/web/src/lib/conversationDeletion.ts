// What a conversation deletion means for the person who asked for it. The API
// owns every outcome; this turns its public responses into plain words and the
// recovery that is actually safe:
// - `retryable`: offer Try again. It re-sends the same DELETE, which resumes the
//   existing owner-scoped request (the API returns the current status for a
//   conversation it already accepted) rather than starting a second one.
// - `blocksRemoval`: retrying can't succeed in this page session, so the row's
//   delete action stays unavailable with this explanation.
// A refusal never implies anything was erased. "Deleted" is said only for a
// verified status or the API's 204 (its best-effort delete of an older
// conversation); neither covers backups.
import { ApiError } from "./api";
import type { DeletionStatus } from "./types";

export type DeletionFeedbackKind =
  | "migration_required"
  | "paused"
  | "unavailable"
  | "not_found"
  | "not_allowed"
  | "refused"
  | "unknown";

export interface DeletionFeedback {
  kind: DeletionFeedbackKind;
  message: string;
  retryable: boolean;
  blocksRemoval: boolean;
  /** A dismissed message stays as the row's accessible explanation. */
  dismissed?: boolean;
}

const UNKNOWN: DeletionFeedback = {
  kind: "unknown",
  message:
    "We couldn't confirm whether this conversation was deleted. Try again to check; it won't start a second deletion.",
  retryable: true,
  blocksRemoval: false,
};

export function deletionFeedbackFor(reason: unknown): DeletionFeedback {
  if (!(reason instanceof ApiError)) return UNKNOWN;
  if (reason.code === "migration_required") {
    // Only a server that predates deleting older conversations says this.
    return {
      kind: "migration_required",
      message: "This older conversation couldn't be deleted yet. Nothing was removed.",
      retryable: true,
      blocksRemoval: false,
    };
  }
  if (reason.code === "deletion_disabled") {
    return {
      kind: "paused",
      message: "Deleting conversations is paused by your administrator. Nothing was removed.",
      retryable: false,
      blocksRemoval: false,
    };
  }
  if (reason.code === "deletion_unavailable") {
    return {
      kind: "unavailable",
      message:
        "Deletion couldn't be recorded right now, so nothing is assumed removed. Try again in a moment.",
      retryable: true,
      blocksRemoval: false,
    };
  }
  if (reason.status === 404) {
    return {
      kind: "not_found",
      message:
        "This conversation couldn't be found. It may already have been deleted elsewhere; reload to refresh the list.",
      retryable: false,
      blocksRemoval: true,
    };
  }
  if (reason.status === 401 || reason.status === 403) {
    return {
      kind: "not_allowed",
      message:
        "You can't delete this conversation right now. Your sign-in may have expired; sign in again, then retry.",
      retryable: false,
      blocksRemoval: false,
    };
  }
  if (reason.status >= 400 && reason.status < 500) {
    return {
      kind: "refused",
      message: `${reason.detail.trim() || "The server refused the request."} Nothing was removed.`,
      retryable: false,
      blocksRemoval: false,
    };
  }
  return UNKNOWN;
}

// After an accepted deletion, the owner's Delete also asks the server to clean
// the conversation up: a few bounded passes of the existing owner-scoped
// reconcile, for that one conversation, in that one page session. Nothing runs
// it later on its own, after a reload, or for any other conversation.
export const CLEANUP_PASSES = 6;

export type DeletionNoticePhase = "cleaning" | "deleted" | "incomplete";

export function isDeletionStatusFor(value: unknown, sessionId: string): value is DeletionStatus {
  return typeof value === "object" && value !== null
    && "sessionId" in value && value.sessionId === sessionId
    && "state" in value
    && (value.state === "pending" || value.state === "retryable" || value.state === "cleanup_verified")
    && "attempts" in value && typeof value.attempts === "number";
}

/** Whether another pass can make progress without anything changing first. */
export function shouldContinueCleanup(status: DeletionStatus): boolean {
  return status.state === "pending" && status.retryReason === null;
}

/** What the notice says right after the server accepts a deletion. */
export function acceptedNoticePhase(status: DeletionStatus | undefined): DeletionNoticePhase {
  // No status (204) is the best-effort delete of an older conversation.
  return !status || status.state === "cleanup_verified" ? "deleted" : "incomplete";
}

const CLEANUP_REASONS: Record<NonNullable<DeletionStatus["retryReason"]>, string> = {
  storage_unavailable: "Storage was unavailable.",
  cleanup_timeout: "It ran out of time.",
  concurrent_change: "Something changed while it ran.",
  integrity_mismatch:
    "Its records didn't match, so it stopped safely. Contact your administrator if this keeps happening.",
  uploads_unresolved: "A file upload to it hasn't finished yet.",
  artifact_store_required: "File storage isn't set up for it. Contact your administrator.",
};

export const CLEANUP_UNCONFIRMED = "We couldn't confirm how far it got.";

export function cleanupProblem(status: DeletionStatus | undefined, failure?: string): string {
  if (failure) return failure;
  if (status?.retryReason) return CLEANUP_REASONS[status.retryReason];
  return "There's more to clean up.";
}
