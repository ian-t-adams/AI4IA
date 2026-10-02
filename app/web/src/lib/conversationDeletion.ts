// What a failed conversation deletion means for the person who asked for it.
// The API owns the outcome; this only turns its public error into an
// explanation and the recovery that is actually safe:
// - `retryable`: offer Try again. It re-sends the same DELETE, which resumes the
//   existing owner-scoped request (the API returns the current status for a
//   conversation it already accepted) rather than starting a second one.
// - `blocksRemoval`: retrying can't succeed in this page session, so the row's
//   delete action stays unavailable with this explanation.
// Nothing here implies that a conversation was erased, or that it wasn't.
import { ApiError } from "./api";

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
    return {
      kind: "migration_required",
      message:
        "This conversation is older than resumable deletion, so it can't be deleted until an administrator approves its migration. Nothing was removed.",
      retryable: false,
      blocksRemoval: true,
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
