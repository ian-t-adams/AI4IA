"use client";

// One photo avatar preview with its persistent AI-generated label. Reusable by
// any surface that shows an avatar (the gallery, and later the voice-settings
// picker), so the disclosure travels with every image rather than depending
// on each caller to add it.
//
// The bytes come only from the record's own authenticated API route, fetched
// through apiFetch (an image element cannot carry the bearer token) and shown
// through a local object URL. A record whose preview points anywhere else gets
// no image and no request.
import { useEffect, useState } from "react";

import {
  fetchPhotoAvatarPreview,
  photoAvatarPreviewPath,
  type PhotoAvatar,
} from "@/lib/photoAvatars";

export function PhotoAvatarPreview({
  avatar,
}: {
  avatar: Pick<PhotoAvatar, "id" | "displayName" | "preview" | "disclosure">;
}) {
  const path = photoAvatarPreviewPath(avatar);
  const [loaded, setLoaded] = useState<{ path: string; url: string } | null>(null);
  const [failedPath, setFailedPath] = useState<string | null>(null);

  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    let objectUrl: string | null = null;
    fetchPhotoAvatarPreview(path, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setLoaded({ path, url: objectUrl });
      })
      .catch(() => {
        if (!controller.signal.aborted) setFailedPath(path);
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  const url = path && loaded?.path === path ? loaded.url : null;
  const label = avatar.disclosure?.label?.trim() || "AI-generated";
  const unavailable = !path || failedPath === path;

  return (
    <figure className="photo-avatar-preview">
      {url ? (
        // eslint-disable-next-line @next/next/no-img-element -- authenticated blob object URL; next/image adds no value
        <img
          src={url}
          alt={`Preview of ${avatar.displayName}`}
          width={avatar.preview?.width || undefined}
          height={avatar.preview?.height || undefined}
        />
      ) : (
        <span className="photo-avatar-preview-empty">
          {unavailable ? "Preview unavailable" : "Loading preview…"}
        </span>
      )}
      <figcaption className="photo-avatar-disclosure">{label}</figcaption>
    </figure>
  );
}
