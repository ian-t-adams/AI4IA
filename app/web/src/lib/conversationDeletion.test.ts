import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { deletionFeedbackFor } from "./conversationDeletion";

describe("deletionFeedbackFor", () => {
  it("holds a conversation that needs an approved migration, without a retry", () => {
    const feedback = deletionFeedbackFor(
      new ApiError(409, "This conversation requires an approved deletion migration.", "migration_required"),
    );
    expect(feedback).toMatchObject({ kind: "migration_required", retryable: false, blocksRemoval: true });
    expect(feedback.message).toMatch(/administrator approves its migration\. Nothing was removed\.$/);
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
