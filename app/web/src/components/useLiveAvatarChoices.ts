"use client";

import { useEffect, useState } from "react";

import {
  getPhotoAvatarConfig,
  listPhotoAvatars,
  photoAvatarErrorMessage,
  photoAvatarUnavailableText,
  type PhotoAvatar,
} from "@/lib/photoAvatars";

export interface LiveAvatarChoices {
  // The owner's avatars the server marks usable right now, in server order.
  avatars: PhotoAvatar[];
  // The server's disclosure label, shown on every live avatar frame.
  disclosureLabel: string;
  loading: boolean;
  error: string | null;
}

const DEFAULT_DISCLOSURE_LABEL = "AI-generated";
const NONE: LiveAvatarChoices = {
  avatars: [], disclosureLabel: DEFAULT_DISCLOSURE_LABEL, loading: false, error: null,
};
const LOADING: LiveAvatarChoices = { ...NONE, loading: true };

/**
 * The photo avatars live voice may offer: only while GET /api/photo-avatars/config
 * reports the feature enabled and available, and only records the server marks
 * `usable` (ready, available, and not waiting on re-verification). This decides
 * visibility only; the relay re-checks ownership, readiness, policy and access
 * on every connection. Refreshes invalidate the previous snapshot immediately;
 * a failed read offers nothing and explains the failure rather than guessing.
 */
export function useLiveAvatarChoices(
  ownerKey: string | null,
  enabled: boolean,
  refreshKey = 0,
): LiveAvatarChoices {
  const [state, setState] = useState<{
    owner: string; refresh: number; value: LiveAvatarChoices;
  } | null>(null);

  useEffect(() => {
    if (!enabled || ownerKey === null) return;
    const controller = new AbortController();
    const settle = (value: LiveAvatarChoices) => {
      if (!controller.signal.aborted) setState({ owner: ownerKey, refresh: refreshKey, value });
    };
    (async () => {
      try {
        const config = await getPhotoAvatarConfig(controller.signal);
        if (config?.enabled !== true || config.available !== true) {
          settle({ ...NONE, error: photoAvatarUnavailableText(config?.reason ?? "disabled") });
          return;
        }
        const avatars = (await listPhotoAvatars(controller.signal)).filter(
          (avatar) => avatar.usable === true && avatar.status === "ready",
        );
        settle({
          avatars,
          disclosureLabel: config.disclosure?.label?.trim() || DEFAULT_DISCLOSURE_LABEL,
          loading: false,
          error: null,
        });
      } catch (error) {
        settle({ ...NONE, error: `Your avatars couldn't be loaded. ${photoAvatarErrorMessage(error)}` });
      }
    })();
    return () => controller.abort();
  }, [enabled, ownerKey, refreshKey]);

  if (!enabled || ownerKey === null) return NONE;
  if (state?.owner !== ownerKey || state.refresh !== refreshKey) return LOADING;
  return state.value;
}
