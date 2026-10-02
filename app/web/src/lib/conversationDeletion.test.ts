import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import {
  CLEANUP_UNCONFIRMED,
  acceptedNoticePhase,
  cleanupProblem,
  deletionFeedbackFor,
  isDeletionStatusFor,
  shouldContinueCleanup,
} from "./conversationDeletion";
import { PENDING_DELETION, VERIFIED_DELETION } from "./deletionTestFixtures";

describe("deletionFeedbackFor", () => {
  it("explains an older server's refusal plainly and lets the owner try again", () => {
    const feedback = deletionFeedbackFor(
      new ApiError(409, "This conversation requires an approved deletion migration.", "migration_required"),
    );
    expect(feedback).toMatchObject({ kind: "migration_required", retryable: true, blocksRemoval: false });
    expect(feedback.message).toBe("This older conversation couldn't be deleted yet. Nothing was removed.");
    expect(feedback.message).not.toMatch(/migration|resumable|administrator/i);
  });

  it("tells a paused deployment apart from a refusal of this conversation", () => {
    const paused = deletionFeedbackFor(new ApiError(409, "Resumable deletion is disabled.", "deletion_disabled"));
    expect(paused).toMatchObject({ kind: "paused", retryable: false, blocksRemoval: false });
    // Control: the same status without the code is the server's own refusal.
    const refused = deletionFeedbackFor(new ApiError(409, "Conversation changed concurrently."));
    expect(refused).toMatchObject({ kind: "refused", retryable: false, blocksRemoval: false });
    expect(refused.message).toBe("Conversation changed concurrently. Nothing was removed.");
  });

  it("offers Try again only where re-sending is a safe resume", () => {
    const unavailable = deletionFeedbackFor(
      new ApiError(503, "Conversation deletion state is unavailable.", "deletion_unavailable"),
    );
    expect(unavailable).toMatchObject({ kind: "unavailable", retryable: true, blocksRemoval: false });
    expect(unavailable.message).toMatch(/nothing is assumed removed/);
    for (const reason of [
      new TypeError("Failed to fetch"),
      new Error("The server did not confirm deletion status for this conversation."),
      new ApiError(502, "Bad Gateway"),
      "lost",
    ]) {
      const unknown = deletionFeedbackFor(reason);
      expect(unknown).toMatchObject({ kind: "unknown", retryable: true, blocksRemoval: false });
      // An unconfirmed outcome must claim neither removal nor its absence.
      expect(unknown.message).toMatch(/couldn't confirm whether/);
      expect(unknown.message).not.toMatch(/Nothing was removed|has been deleted|was removed/);
    }
  });

  it("explains a missing conversation and an authorization refusal", () => {
    expect(deletionFeedbackFor(new ApiError(404, "Session not found"))).toMatchObject({
      kind: "not_found", retryable: false, blocksRemoval: true,
    });
    for (const status of [401, 403]) {
      expect(deletionFeedbackFor(new ApiError(status, "Forbidden"))).toMatchObject({
        kind: "not_allowed", retryable: false, blocksRemoval: false,
      });
    }
  });

  it("keeps a sensible message when the server sends no detail", () => {
    expect(deletionFeedbackFor(new ApiError(400, "  ")).message).toBe(
      "The server refused the request. Nothing was removed.",
    );
  });
});

describe("cleanup after an accepted deletion", () => {
  it("recognizes only a well-formed status for the exact conversation", () => {
    expect(isDeletionStatusFor({ ...PENDING_DELETION }, PENDING_DELETION.sessionId)).toBe(true);
    expect(isDeletionStatusFor({ ...PENDING_DELETION }, "another")).toBe(false);
    expect(isDeletionStatusFor({ ...PENDING_DELETION, state: "done" }, PENDING_DELETION.sessionId)).toBe(false);
    expect(isDeletionStatusFor({ ...PENDING_DELETION, attempts: "1" }, PENDING_DELETION.sessionId)).toBe(false);
    for (const value of [undefined, null, "pending", 7]) {
      expect(isDeletionStatusFor(value, PENDING_DELETION.sessionId)).toBe(false);
    }
  });

  it("continues only while more work can run without anything changing first", () => {
    expect(shouldContinueCleanup(PENDING_DELETION)).toBe(true);
    expect(shouldContinueCleanup({ ...PENDING_DELETION, retryReason: "uploads_unresolved" })).toBe(false);
    expect(shouldContinueCleanup({ ...PENDING_DELETION, state: "retryable", retryReason: "cleanup_timeout" })).toBe(false);
    expect(shouldContinueCleanup(VERIFIED_DELETION)).toBe(false);
  });

  it("calls a best-effort or verified deletion done, and anything else unfinished", () => {
    expect(acceptedNoticePhase(undefined)).toBe("deleted");
    expect(acceptedNoticePhase(VERIFIED_DELETION)).toBe("deleted");
    expect(acceptedNoticePhase(PENDING_DELETION)).toBe("incomplete");
    expect(acceptedNoticePhase({ ...PENDING_DELETION, state: "retryable" })).toBe("incomplete");
  });

  it("explains an unfinished cleanup in plain words", () => {
    expect(cleanupProblem(PENDING_DELETION)).toBe("There's more to clean up.");
    expect(cleanupProblem({ ...PENDING_DELETION, state: "retryable", retryReason: "storage_unavailable" }))
      .toBe("Storage was unavailable.");
    expect(cleanupProblem(PENDING_DELETION, CLEANUP_UNCONFIRMED)).toBe(CLEANUP_UNCONFIRMED);
    expect(cleanupProblem(undefined)).toBe("There's more to clean up.");
  });
});

