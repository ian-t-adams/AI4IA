import type { DeletionStatus, SessionInitialization } from "./types";

export const INITIALIZING_SESSION: SessionInitialization = {
  sessionId: "reserved/creation",
  createdAt: "2026-09-09T11:59:00Z",
  state: "initializing",
};

export const PENDING_DELETION: DeletionStatus = {
  sessionId: "removed/session",
  state: "pending",
  phase: "messages",
  requestedAt: "2026-09-09T12:00:00Z",
  updatedAt: "2026-09-09T12:01:00Z",
  lastVerifiedAt: null,
  messagesVerified: false,
  documentsVerified: false,
  attachmentsVerified: false,
  pendingUploads: [],
  pendingUploadsTruncated: false,
  retryReason: null,
  attempts: 1,
  scope: "conversation_content_and_inline_originals",
  backupsErased: false,
  coordinationRetained: true,
  autonomousCleanup: false,
};

export const VERIFIED_DELETION: DeletionStatus = {
  ...PENDING_DELETION,
  state: "cleanup_verified",
  phase: "complete",
  updatedAt: "2026-09-09T12:02:00Z",
  lastVerifiedAt: "2026-09-09T12:02:00Z",
  messagesVerified: true,
  documentsVerified: true,
  attachmentsVerified: true,
  attempts: 2,
};
