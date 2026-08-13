"use client";

// Deep-link media player. Plays a ready audio/video library document and
// renders the analyzer's scene timeline (camera shots + keyframes) as clickable
// deep-links that seek the player. Rendered as a modal from the LibraryPanel, only
// for ready documents whose modality is audio or video — so it is inert otherwise.
//
// The media bytes and the timeline both ride the authenticated apiFetch path: a raw
// <video src> URL could not carry the bearer token, so we fetch the full blob and
// wrap it in an object URL (which also gives client-side seeking for free).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { fetchLibraryMedia, fetchLibraryTimeline } from "@/lib/api";
import type {
  LibraryDocument,
  MediaTimeline,
  MediaTimelineSegment,
} from "@/lib/library";
import { useModalFocus, useModalKeyDown } from "./useModalFocus";

function formatTimecode(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

// Flatten a timeline into a sorted, de-duplicated list of seekable markers. Camera
// shots and keyframes are distinct kinds so the strip can label them, but both are
// just timestamps the user can jump to.
type Marker = { ms: number; kind: "shot" | "keyframe"; segment: number };
type MediaLoadState =
  | {
      documentId: string;
      mediaUrl: string;
      timeline: MediaTimeline | null;
      error: null;
    }
  | {
      documentId: string;
      mediaUrl: null;
      timeline: null;
      error: string;
    };

function buildMarkers(segments: MediaTimelineSegment[]): Marker[] {
  const seen = new Set<number>();
  const markers: Marker[] = [];
  for (const seg of segments) {
    for (const ms of seg.shots) {
      if (!seen.has(ms)) {
        seen.add(ms);
        markers.push({ ms, kind: "shot", segment: seg.index });
      }
    }
    for (const ms of seg.keyframes) {
      if (!seen.has(ms)) {
        seen.add(ms);
        markers.push({ ms, kind: "keyframe", segment: seg.index });
      }
    }
  }
  markers.sort((a, b) => a.ms - b.ms);
  return markers;
}

export function MediaPlayer({
  doc,
  seekToMs,
  onClose,
}: {
  doc: LibraryDocument;
  seekToMs?: number;
  onClose: () => void;
}) {
  const modalRef = useModalFocus();
  const onModalKeyDown = useModalKeyDown(onClose);
  const isVideo = doc.modality === "video";
  const mediaRef = useRef<HTMLVideoElement | HTMLAudioElement | null>(null);
  const [loadState, setLoadState] = useState<MediaLoadState | null>(null);
  const currentLoad =
    loadState?.documentId === doc.id ? loadState : null;
  const mediaUrl = currentLoad?.mediaUrl ?? null;
  const timeline = currentLoad?.timeline ?? null;
  const error = currentLoad?.error ?? null;
  const loading = currentLoad === null;

  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;
    (async () => {
      try {
        // Timeline is best-effort: a missing analyzer sidecar yields empty segments
        // server-side, so the player still works as a plain audio/video element.
        const [blob, tl] = await Promise.all([
          fetchLibraryMedia(doc.id),
          fetchLibraryTimeline(doc.id).catch(() => null),
        ]);
        if (cancelled) return;
        const nextObjectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(nextObjectUrl);
          return;
        }
        objectUrl = nextObjectUrl;
        setLoadState({
          documentId: doc.id,
          mediaUrl: objectUrl,
          timeline: tl,
          error: null,
        });
      } catch (e) {
        if (!cancelled) {
          setLoadState({
            documentId: doc.id,
            mediaUrl: null,
            timeline: null,
            error: (e as Error).message,
          });
        }
      }
    })();
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [doc.id]);

  // Esc closes the modal.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const seek = useCallback((ms: number) => {
    const el = mediaRef.current;
    if (!el) return;
    el.currentTime = ms / 1000;
    void el.play().catch(() => {
      /* autoplay may be blocked; the user can press play */
    });
  }, []);

  // Honor an optional initial deep-link once the media element can seek.
  const seekTarget = seekToMs;
  useEffect(() => {
    if (seekTarget == null || !mediaUrl) return;
    const el = mediaRef.current;
    if (!el) return;
    const apply = () => seek(seekTarget);
    if (el.readyState >= 1) {
      apply();
    } else {
      el.addEventListener("loadedmetadata", apply, { once: true });
      return () => el.removeEventListener("loadedmetadata", apply);
    }
  }, [seekTarget, mediaUrl, seek]);

  const markers = useMemo(
    () => (timeline ? buildMarkers(timeline.segments) : []),
    [timeline],
  );

  return (
    <div
      ref={modalRef}
      onKeyDown={onModalKeyDown}
      role="dialog"
      aria-label={`Media player for ${doc.filename}`}
      aria-modal="true"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.45)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 60,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-elevated)",
          color: "var(--fg)",
          width: "min(640px, 94vw)",
          borderRadius: "var(--radius)",
          border: "1px solid var(--border)",
          padding: 24,
          display: "flex",
          flexDirection: "column",
          gap: 16,
          maxHeight: "90vh",
          overflowY: "auto",
        }}
      >
        <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
          <h2
            style={{
              margin: 0,
              fontSize: "1.1em",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
            title={doc.filename}
          >
            {isVideo ? "🎬" : "🔊"} {doc.filename}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close player"
            style={{
              border: "none",
              background: "transparent",
              color: "var(--fg)",
              fontSize: "1.2em",
            }}
          >
            ✕
          </button>
        </div>

        {error && (
          <div
            role="alert"
            style={{
              fontSize: "0.8em",
              color: "var(--danger)",
              border: "1px solid var(--danger)",
              borderRadius: 8,
              padding: "8px 10px",
            }}
          >
            {error}
          </div>
        )}

        {loading ? (
          <span style={{ fontSize: "0.85em", color: "var(--fg-muted)" }}>
            Loading media…
          </span>
        ) : mediaUrl ? (
          isVideo ? (
            <video
              ref={mediaRef as React.RefObject<HTMLVideoElement>}
              src={mediaUrl}
              controls
              style={{
                width: "100%",
                borderRadius: 8,
                background: "#000",
                maxHeight: "60vh",
              }}
            />
          ) : (
            <audio
              ref={mediaRef as React.RefObject<HTMLAudioElement>}
              src={mediaUrl}
              controls
              style={{ width: "100%" }}
            />
          )
        ) : null}

        {markers.length > 0 && (
          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            <div style={{ fontSize: "0.8em", color: "var(--fg-muted)" }}>
              Scenes &amp; keyframes — click to jump
            </div>
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 6,
              }}
            >
              {markers.map((m) => (
                <button
                  key={`${m.kind}-${m.ms}`}
                  onClick={() => seek(m.ms)}
                  aria-label={`Jump to ${formatTimecode(m.ms)} (${
                    m.kind === "shot" ? "camera shot" : "keyframe"
                  })`}
                  title={`Jump to ${formatTimecode(m.ms)} (${
                    m.kind === "shot" ? "camera shot" : "keyframe"
                  })`}
                  style={{
                    border: "1px solid var(--border)",
                    background:
                      m.kind === "shot" ? "var(--accent)" : "var(--bg)",
                    color: m.kind === "shot" ? "var(--accent-fg)" : "var(--fg)",
                    borderRadius: 6,
                    padding: "4px 8px",
                    fontSize: "0.75em",
                    cursor: "pointer",
                    fontVariantNumeric: "tabular-nums",
                  }}
                >
                  {m.kind === "shot" ? "🎞" : "🖼"} {formatTimecode(m.ms)}
                </button>
              ))}
            </div>
          </div>
        )}

        {!loading && markers.length === 0 && (
          <span style={{ fontSize: "0.78em", color: "var(--fg-muted)" }}>
            No scene markers detected for this media.
          </span>
        )}
      </div>
    </div>
  );
}
