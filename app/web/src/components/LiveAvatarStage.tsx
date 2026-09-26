"use client";

// The live photo avatar during a Speech Voice Live session. It adopts the
// controller-owned <video> (the avatar's speech plays from it), keeps the
// AI-generated label visible for the whole session, and surfaces the relay's
// idle timeout and session cap with an explicit way to end the session.
import { useEffect, useRef, useSyncExternalStore } from "react";

import type { LiveAvatarView } from "@/lib/voiceLive";

// The cap countdown appears once this little time is left.
const CAP_NOTICE_SECONDS = 60;

// A whole-second clock for the countdowns, subscribed only while a live avatar
// session is on screen.
function subscribeClock(onTick: () => void): () => void {
  const timer = setInterval(onTick, 250);
  return () => clearInterval(timer);
}

function readClock(): number {
  return Math.floor(Date.now() / 1000) * 1000;
}

function secondsUntil(deadline: number | null, now: number): number | null {
  if (deadline === null) return null;
  return Math.max(0, Math.ceil((deadline - now) / 1000));
}

function clock(seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  return `${minutes}:${String(seconds % 60).padStart(2, "0")}`;
}

const BUTTON_STYLE: React.CSSProperties = {
  border: "1px solid var(--border)",
  borderRadius: 8,
  padding: "5px 12px",
  background: "var(--bg)",
  color: "var(--fg)",
  font: "inherit",
  fontSize: "0.85em",
  cursor: "pointer",
};

export function LiveAvatarStage({
  avatar,
  active,
  onEnd,
}: {
  avatar: LiveAvatarView | null;
  active: boolean;
  onEnd: () => void;
}) {
  if (!avatar || !active) return null;
  return <ActiveLiveAvatarStage avatar={avatar} onEnd={onEnd} />;
}

function ActiveLiveAvatarStage({
  avatar,
  onEnd,
}: {
  avatar: LiveAvatarView;
  onEnd: () => void;
}) {
  const mount = useRef<HTMLDivElement | null>(null);
  const now = useSyncExternalStore(subscribeClock, readClock, () => 0);
  const element = avatar.element;
  const label = avatar.label.trim() || "AI-generated";

  useEffect(() => {
    const host = mount.current;
    if (!host || !element) return;
    host.appendChild(element);
    return () => {
      if (element.parentNode === host) host.removeChild(element);
    };
  }, [element]);

  const idleEndsAt = avatar.idleEndsAt;
  const sessionEndsAt = avatar.sessionEndsAt;

  if (avatar.unsupported || !element) {
    return (
      <p
        role="status"
        style={{
          margin: 0,
          padding: "6px max(16px, 6%)",
          borderTop: "1px solid var(--border)",
          background: "var(--bg-elevated)",
          color: "var(--fg-muted)",
          fontSize: "0.8em",
        }}
      >
        This browser can&apos;t play avatar video, so this session is voice only.
      </p>
    );
  }

  const idleLeft = secondsUntil(idleEndsAt, now);
  const capLeft = secondsUntil(sessionEndsAt, now);
  const showCap = capLeft !== null && capLeft <= CAP_NOTICE_SECONDS;
  const state = !avatar.started ? "Starting the avatar…" : avatar.speaking ? "Speaking" : "Listening";

  return (
    <section
      aria-label="Live avatar"
      style={{
        display: "flex",
        alignItems: "stretch",
        gap: 14,
        margin: "8px max(16px, 6%) 0",
        padding: 10,
        border: "1px solid var(--border)",
        borderRadius: 12,
        background: "var(--bg-elevated)",
      }}
    >
      <div
        style={{
          position: "relative",
          flex: "0 0 auto",
          width: "clamp(112px, 22vw, 176px)",
          aspectRatio: "1 / 1",
          borderRadius: 8,
          overflow: "hidden",
          background: "var(--bg-sidebar)",
        }}
      >
        <div ref={mount} style={{ position: "absolute", inset: 0 }} />
        <span
          style={{
            position: "absolute",
            left: 6,
            bottom: 6,
            padding: "2px 7px",
            borderRadius: 6,
            background: "var(--bg-sidebar)",
            color: "var(--sidebar-fg)",
            fontSize: "0.72em",
            fontWeight: 600,
          }}
        >
          {label}
        </span>
      </div>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          justifyContent: "space-between",
          gap: 8,
          minWidth: 0,
          fontSize: "0.85em",
        }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <strong style={{ display: "flex", alignItems: "center", gap: 7, color: "var(--fg)" }}>
            <span
              aria-hidden="true"
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: avatar.started ? "var(--accent)" : "var(--border)",
              }}
            />
            {state}
          </strong>
          <span style={{ color: "var(--fg-muted)" }}>
            A synthetic, AI-generated likeness speaks the replies. Avatar time is billed while the
            session is connected, even when nobody is talking.
          </span>
          {idleLeft !== null && (
            <span role="status" style={{ color: "var(--warn)" }}>
              Nobody has spoken for a while. The session ends in {clock(idleLeft)} unless you keep
              talking.
            </span>
          )}
          {showCap && (
            <span role="status" style={{ color: "var(--warn)" }}>
              The session reaches its time limit in {clock(capLeft)}.
            </span>
          )}
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
          {avatar.playbackBlocked && (
            <button type="button" onClick={avatar.resume} style={BUTTON_STYLE}>
              Play avatar sound
            </button>
          )}
          <button type="button" onClick={onEnd} style={BUTTON_STYLE}>
            End session
          </button>
        </div>
      </div>
    </section>
  );
}
