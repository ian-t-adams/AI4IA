"use client";

import { useEffect, useId, useRef } from "react";

import type { PhotoAvatar } from "@/lib/photoAvatars";
import { primaryBtn, secondaryBtn } from "./builderStyles";
import { PhotoAvatarPreview } from "./PhotoAvatarPreview";

export function PhotoAvatarVoiceCard({
  avatar,
  voice,
  disabledReason,
  locked,
  focusRequest,
  onStart,
  onChoose,
  onClear,
}: {
  avatar: Pick<PhotoAvatar, "id" | "displayName" | "preview" | "disclosure"> | null;
  voice: string;
  disabledReason: string | null;
  locked: boolean;
  focusRequest: number;
  onStart: () => void;
  onChoose: () => void;
  onClear: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const reasonId = useId();
  useEffect(() => {
    if (focusRequest > 0) heading.current?.focus();
  }, [focusRequest]);

  return (
    <section className="photo-avatar-voice-card" aria-label="Avatar voice">
      {avatar ? (
        <div className="photo-avatar-voice-portrait">
          <PhotoAvatarPreview avatar={avatar} />
        </div>
      ) : null}
      <div className="photo-avatar-voice-body">
        <h3 ref={heading} tabIndex={-1}>
          {avatar ? `Talk with ${avatar.displayName}` : "Talk with your avatar"}
        </h3>
        {avatar ? <p>Azure Speech · {voice}</p> : null}
        <p>
          Speak with your AI-generated avatar in this chat. Avatar time is billed while the
          session is connected, even when nobody is talking.
        </p>
        {disabledReason ? <p id={reasonId} role="status">{disabledReason}</p> : null}
        <div className="photo-avatar-actions">
          <button
            type="button"
            style={primaryBtn}
            disabled={disabledReason !== null}
            aria-describedby={disabledReason ? reasonId : undefined}
            onClick={onStart}
          >
            Start talking
          </button>
          <button type="button" style={secondaryBtn} disabled={locked} onClick={onChoose}>
            Choose avatar
          </button>
          <button type="button" style={secondaryBtn} disabled={locked} onClick={onClear}>
            Voice only
          </button>
        </div>
      </div>
    </section>
  );
}
