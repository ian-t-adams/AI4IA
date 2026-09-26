"use client";

import { useEffect, useState } from "react";

import { getPhotoAvatarConfig } from "@/lib/photoAvatars";

/**
 * Whether to offer the photo avatar gallery to the current owner. Read from
 * GET /api/photo-avatars/config, so no web env var or build flag is involved.
 * This decides visibility only: the API refuses every route but /config while
 * the feature is off. A failed read hides the entry rather than guessing.
 */
export function usePhotoAvatarsEnabled(ownerKey: string | null): boolean {
  const [state, setState] = useState<{ owner: string; enabled: boolean } | null>(null);

  useEffect(() => {
    if (ownerKey === null) return;
    const controller = new AbortController();
    getPhotoAvatarConfig(controller.signal)
      .then((config) => {
        if (!controller.signal.aborted) {
          setState({ owner: ownerKey, enabled: config?.enabled === true });
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ owner: ownerKey, enabled: false });
      });
    return () => controller.abort();
  }, [ownerKey]);

  return ownerKey !== null && state?.owner === ownerKey && state.enabled;
}
