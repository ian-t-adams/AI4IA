"use client";

import { useId } from "react";

// Shown once a newer web build is serving than the one this tab runs. It never
// reloads by itself; Reload waits while reloading would lose work in progress.
export function StaleBuildBanner({
  blockedReason,
  onReload,
}: {
  // Why reloading now would lose work (a live voice session, an unsaved
  // transcript, a streaming reply, an upload), or null when it is safe.
  blockedReason: string | null;
  onReload: () => void;
}) {
  const detailId = useId();
  return (
    <div className="build-notice">
      <div className="build-notice-text">
        <p role="status">A new version of AI4IA is available.</p>
        <p id={detailId}>
          {blockedReason ?? "Reload to update. New voice sessions start after you reload."}
        </p>
      </div>
      <div className="build-notice-actions">
        <button
          type="button"
          className="btn btn-sm"
          onClick={onReload}
          disabled={blockedReason !== null}
          aria-describedby={detailId}
        >
          Reload
        </button>
      </div>
    </div>
  );
}
