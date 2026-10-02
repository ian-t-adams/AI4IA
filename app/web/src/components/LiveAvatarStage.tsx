"use client";

// The live photo avatar during a Speech Voice Live session. It adopts the
// controller-owned <video> (the avatar's speech plays from it), keeps the
// AI-generated label on the picture for the whole session (in every size,
// in focus view and in full screen: the label lives inside the element that
// goes full screen, and picture-in-picture stays disabled on the video), and
// surfaces the relay's idle timeout and session cap with an explicit way to
// end the session.
//
// The frame takes the stream's own aspect ratio, read from the video once it
// has dimensions, so the avatar is never squeezed into an arbitrary box. "Fit"
// shows the whole picture; "Fill" crops to the stage for a closer view.
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type CSSProperties,
  type RefObject,
} from "react";

import type { LiveAvatarView } from "@/lib/voiceLive";
import { Icon } from "./Icon";

export type StageFit = "fit" | "fill";

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

// What the live region says. It changes only when a warning appears and when
// about ten seconds remain, never on every tick of the visible countdown.
const FINAL_SECONDS = 10;

export function countdownAnnouncement(idleLeft: number | null, capLeft: number | null): string {
  const parts: string[] = [];
  if (idleLeft !== null) {
    parts.push(
      idleLeft <= FINAL_SECONDS
        ? "About ten seconds until the avatar session ends. Keep talking to continue."
        : "Nobody has spoken for a while. The avatar session ends soon unless you keep talking.",
    );
  }
  if (capLeft !== null && capLeft <= CAP_NOTICE_SECONDS) {
    parts.push(
      capLeft <= FINAL_SECONDS
        ? "About ten seconds until the avatar session reaches its time limit."
        : "The avatar session reaches its time limit in about a minute.",
    );
  }
  return parts.join(" ");
}

function useFullscreen(target: RefObject<HTMLElement | null>) {
  const [active, setActive] = useState(false);
  const supported =
    typeof document !== "undefined" &&
    document.fullscreenEnabled === true &&
    typeof HTMLElement !== "undefined" &&
    typeof HTMLElement.prototype.requestFullscreen === "function";
  useEffect(() => {
    if (!supported) return;
    const sync = () => setActive(document.fullscreenElement === target.current);
    document.addEventListener("fullscreenchange", sync);
    // Removing the stage from the document leaves full screen on its own.
    return () => document.removeEventListener("fullscreenchange", sync);
  }, [supported, target]);
  const toggle = useCallback(() => {
    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => {});
    } else {
      void target.current?.requestFullscreen().catch(() => {});
    }
  }, [target]);
  return { supported, active, toggle };
}

export function LiveAvatarStage({
  avatar,
  active,
  onEnd,
  variant = "stage",
  fit = "fit",
  onFitChange,
  focused = false,
  onToggleFocus,
  captions = null,
  onReturn,
}: {
  avatar: LiveAvatarView | null;
  active: boolean;
  onEnd: () => void;
  /** "mini" is the floating player shown while another page is open. */
  variant?: "stage" | "mini";
  fit?: StageFit;
  onFitChange?: (fit: StageFit) => void;
  focused?: boolean;
  onToggleFocus?: () => void;
  /** The latest spoken line, shown over the picture in focus view. */
  captions?: string | null;
  onReturn?: () => void;
}) {
  if (!avatar || !active) return null;
  return (
    <ActiveLiveAvatarStage
      avatar={avatar}
      onEnd={onEnd}
      variant={variant}
      fit={fit}
      onFitChange={onFitChange}
      focused={focused}
      onToggleFocus={onToggleFocus}
      captions={captions}
      onReturn={onReturn}
    />
  );
}

function ActiveLiveAvatarStage({
  avatar,
  onEnd,
  variant,
  fit,
  onFitChange,
  focused,
  onToggleFocus,
  captions,
  onReturn,
}: {
  avatar: LiveAvatarView;
  onEnd: () => void;
  variant: "stage" | "mini";
  fit: StageFit;
  onFitChange?: (fit: StageFit) => void;
  focused: boolean;
  onToggleFocus?: () => void;
  captions: string | null;
  onReturn?: () => void;
}) {
  const mount = useRef<HTMLDivElement | null>(null);
  const stageRef = useRef<HTMLElement | null>(null);
  const now = useSyncExternalStore(subscribeClock, readClock, () => 0);
  const element = avatar.element;
  const label = avatar.label.trim() || "AI-generated";
  const [ratio, setRatio] = useState<number | null>(null);
  const fullscreen = useFullscreen(stageRef);
  const mini = variant === "mini";

  useEffect(() => {
    const host = mount.current;
    if (!host || !element) return;
    host.appendChild(element);
    return () => {
      if (element.parentNode === host) host.removeChild(element);
    };
  }, [element]);

  useEffect(() => {
    if (!element) return;
    const measure = () => {
      if (element.videoWidth > 0 && element.videoHeight > 0) {
        setRatio(element.videoWidth / element.videoHeight);
      }
    };
    measure();
    element.addEventListener("loadedmetadata", measure);
    element.addEventListener("resize", measure);
    return () => {
      element.removeEventListener("loadedmetadata", measure);
      element.removeEventListener("resize", measure);
    };
  }, [element]);

  const idleEndsAt = avatar.idleEndsAt;
  const sessionEndsAt = avatar.sessionEndsAt;

  if (avatar.unsupported || !element) {
    return (
      <p role="status" className="live-stage-unsupported">
        This browser can&apos;t play avatar video, so this session is voice only.
      </p>
    );
  }

  const idleLeft = secondsUntil(idleEndsAt, now);
  const capLeft = secondsUntil(sessionEndsAt, now);
  const showCap = capLeft !== null && capLeft <= CAP_NOTICE_SECONDS;
  const state = !avatar.started ? "Starting…" : avatar.speaking ? "Speaking" : "Listening";
  const stateKey = !avatar.started ? "starting" : avatar.speaking ? "speaking" : "listening";
  const frameStyle = { "--video-ar": ratio ?? 1 } as CSSProperties;

  return (
    <section
      ref={stageRef}
      className="live-stage live-stage-session"
      aria-label="Live avatar"
      data-state={stateKey}
      data-fit={fit}
      data-variant={variant}
    >
      <div className="live-stage-viewport">
        <div className="live-stage-frame" style={frameStyle}>
          <div ref={mount} className="live-stage-media" />
          <span className="live-stage-label">{label}</span>
          <span className="live-stage-state">
            <span>{state}</span>
          </span>
          {(focused || fullscreen.active) && captions ? (
            <p className="live-stage-captions" aria-hidden="true">
              <span>{captions}</span>
            </p>
          ) : null}
          {avatar.playbackBlocked ? (
            <button type="button" className="btn btn-primary live-stage-unmute" onClick={avatar.resume}>
              <Icon name="speaker" size={18} />
              Play avatar sound
            </button>
          ) : null}
        </div>
      </div>
      {/* The visible countdowns tick every second, so they are timers, which
          screen readers don't announce; the status region below speaks only
          at a couple of thresholds. */}
      {idleLeft !== null || showCap ? (
        <div className="live-stage-warnings">
          {idleLeft !== null && (
            <span role="timer" aria-live="off">
              Nobody has spoken for a while. The session ends in {clock(idleLeft)} unless you keep
              talking.
            </span>
          )}
          {showCap && (
            <span role="timer" aria-live="off">
              The session reaches its time limit in {clock(capLeft)}.
            </span>
          )}
        </div>
      ) : null}
      <span className="visually-hidden" role="status" aria-live="polite" aria-atomic="true">
        {countdownAnnouncement(idleLeft, capLeft)}
      </span>
      <div className="live-stage-bar">
        {mini ? null : (
          <p className="live-stage-note">
            A synthetic, AI-generated likeness speaks the replies. Avatar time is billed while the
            session is connected, even when nobody is talking.
          </p>
        )}
        <div className="live-stage-controls">
          {mini ? (
            onReturn ? (
              <button type="button" className="btn btn-sm" onClick={onReturn}>
                <Icon name="chat" size={16} />
                Return to conversation
              </button>
            ) : null
          ) : (
            <>
              {onFitChange ? (
                <button
                  type="button"
                  className="btn"
                  aria-pressed={fit === "fill"}
                  onClick={() => onFitChange(fit === "fill" ? "fit" : "fill")}
                  title={fit === "fill" ? "Show the whole picture" : "Crop to fill the stage"}
                >
                  <Icon name={fit === "fill" ? "fill" : "fit"} size={18} />
                  <span className="btn-label">Fill frame</span>
                </button>
              ) : null}
              {onToggleFocus ? (
                <button
                  type="button"
                  className="btn"
                  aria-pressed={focused}
                  onClick={onToggleFocus}
                  title={focused ? "Show the transcript again" : "Give the avatar the whole conversation area"}
                >
                  <Icon name={focused ? "shrink" : "expand"} size={18} />
                  <span className="btn-label">Focus view</span>
                </button>
              ) : null}
              {fullscreen.supported ? (
                <button
                  type="button"
                  className="btn"
                  aria-pressed={fullscreen.active}
                  onClick={fullscreen.toggle}
                  title={fullscreen.active ? "Leave full screen" : "Show the avatar full screen"}
                >
                  <Icon name="fullscreen" size={18} />
                  <span className="btn-label">Full screen</span>
                </button>
              ) : null}
            </>
          )}
          <button
            type="button"
            className={mini ? "btn btn-sm btn-danger" : "btn btn-danger"}
            onClick={onEnd}
            title="End session"
          >
            <Icon name="stop" size={mini ? 16 : 18} />
            <span className="btn-label">End session</span>
          </button>
        </div>
      </div>
    </section>
  );
}
