"use client";

// The chosen avatar before a live session starts. Expanded, it is a large
// portrait on the conversation stage; compact, a slim bar above the composer.
// Either way it carries the billing disclosure, and picking an avatar never
// starts the microphone or the meter by itself. The portrait carries its own
// AI-generated label (PhotoAvatarPreview); the compact bar shows no picture,
// so no likeness ever appears without that label. The live session replaces
// the lobby on the stage once the user chooses Start talking.
import { useEffect, useId, useRef, type CSSProperties } from "react";

import type { PhotoAvatar } from "@/lib/photoAvatars";
import { formatVoiceName } from "@/lib/voiceNames";
import { Icon } from "./Icon";
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
  compact = false,
  onToggleSize,
}: {
  avatar: Pick<PhotoAvatar, "id" | "displayName" | "preview" | "disclosure"> | null;
  voice: string;
  disabledReason: string | null;
  locked: boolean;
  focusRequest: number;
  onStart: () => void;
  onChoose: () => void;
  onClear: () => void;
  /** The slim bar form, shown when the user minimizes the stage. */
  compact?: boolean;
  onToggleSize?: () => void;
}) {
  const heading = useRef<HTMLHeadingElement>(null);
  const reasonId = useId();
  useEffect(() => {
    if (focusRequest > 0) heading.current?.focus();
  }, [focusRequest]);
  const title = avatar ? `Talk with ${avatar.displayName}` : "Talk with your avatar";
  const voiceName = formatVoiceName(voice);
  const width = avatar?.preview?.width ?? 0;
  const height = avatar?.preview?.height ?? 0;
  const frameStyle = {
    "--video-ar": width > 0 && height > 0 ? width / height : 1,
  } as CSSProperties;

  const start = (
    <button
      type="button"
      className="btn btn-primary"
      disabled={disabledReason !== null}
      aria-describedby={disabledReason ? reasonId : undefined}
      onClick={onStart}
    >
      <Icon name="mic" size={18} />
      Start talking
    </button>
  );
  const choices = (
    <>
      <button type="button" className="btn btn-ghost btn-sm" disabled={locked} onClick={onChoose}>
        Choose avatar
      </button>
      <button type="button" className="btn btn-ghost btn-sm" disabled={locked} onClick={onClear}>
        Voice only
      </button>
    </>
  );
  const reason = disabledReason ? (
    <p id={reasonId} role="status" className="lobby-reason">
      {disabledReason}
    </p>
  ) : null;

  if (compact) {
    return (
      <section className="avatar-lobby-bar" aria-label="Avatar voice">
        <span className="avatar-lobby-icon" aria-hidden="true">
          <Icon name="avatar" size={22} />
        </span>
        <div className="avatar-lobby-text">
          <h3 ref={heading} tabIndex={-1}>
            {title}
          </h3>
          <p>
            {avatar ? `Azure Speech · ${voiceName}. ` : ""}Avatar time is billed while the session
            is connected, even when nobody is talking.
          </p>
          {reason}
        </div>
        <div className="avatar-lobby-actions">
          {start}
          {choices}
          {onToggleSize ? (
            <button
              type="button"
              className="icon-btn"
              onClick={onToggleSize}
              aria-label="Show the avatar stage"
              title="Show the avatar stage"
            >
              <Icon name="expand" />
            </button>
          ) : null}
        </div>
      </section>
    );
  }

  return (
    <section className="live-stage live-stage-lobby" aria-label="Avatar voice" data-fit="fit">
      <div className="live-stage-viewport">
        <div className="live-stage-frame" style={frameStyle}>
          {avatar ? (
            <div className="live-stage-media">
              <PhotoAvatarPreview avatar={avatar} />
            </div>
          ) : (
            <div className="live-stage-placeholder">
              <Icon name="avatar" size={72} />
            </div>
          )}
        </div>
      </div>
      <div className="live-stage-bar">
        <div className="lobby-copy">
          <h3 ref={heading} tabIndex={-1}>
            {title}
          </h3>
          {avatar ? <p className="lobby-meta">Azure Speech · {voiceName}</p> : null}
          <p className="lobby-note">
            <span className="lobby-how">Speak or type to your AI-generated avatar in this chat. </span>
            Avatar time is billed while the session is connected, even when nobody is talking.
          </p>
          {reason}
        </div>
        <div className="lobby-actions">
          {start}
          <div className="lobby-secondary">{choices}</div>
        </div>
      </div>
      {onToggleSize ? (
        <button
          type="button"
          className="icon-btn lobby-minimize"
          onClick={onToggleSize}
          aria-label="Minimize the avatar stage"
          title="Minimize the avatar stage"
        >
          <Icon name="shrink" />
        </button>
      ) : null}
    </section>
  );
}
