"use client";

// Live photo avatar video. Voice Live streams the avatar as
// `response.video.delta` events over the governed WebSocket: base64 fragmented
// MP4 (an ftyp+moov init segment, then moof/mdat fragments) carrying H.264
// video and the AAC speech audio. There is no separate PCM audio in avatar
// mode, so the video element is also the speaker.
//
// The player feeds MediaSource strictly in arrival order through one bounded
// operation queue. It never drops a fragment silently: a backlog past the
// bound, an unsupported codec or a malformed stream fails the avatar, and the
// caller falls back to voice only. Played ranges are evicted, and playback is
// kept near the live edge so a barge-in doesn't wait out buffered speech.

// The spike's stream: H.264 High level 3.0 (avcC 0x64 0x00 0x1E) plus AAC-LC.
export const AVATAR_FALLBACK_MIME = 'video/mp4; codecs="avc1.64001E, mp4a.40.2"';
// The relay bounds a whole frame at 256 KiB of text; a delta can never be longer.
export const MAX_AVATAR_DELTA_CHARS = 256 * 1024;
// An init segment is a few kilobytes; anything this large without a moov is not one.
export const MAX_AVATAR_INIT_BYTES = 256 * 1024;
// About ten seconds at 25 fps: arrivals only outrun appends when the tab stalls.
export const DEFAULT_MAX_QUEUED_CHUNKS = 250;
export const DEFAULT_MAX_QUEUED_BYTES = 4 * 1024 * 1024;
export const DEFAULT_RETAIN_BEHIND_SECONDS = 10;
export const DEFAULT_MAX_LAG_SECONDS = 1.5;
export const DEFAULT_LIVE_EDGE_SECONDS = 0.25;

export type AvatarVideoFailure =
  | "unsupported"
  | "codec_unsupported"
  | "stream_invalid"
  | "video_backlog"
  | "append_failed";

// --- ISO-BMFF init segment parsing (pure, bounded) ---------------------------

interface Box {
  type: string;
  start: number;
  end: number;
  body: number;
}

// ``complete`` is false when the range ends inside a box; ``malformed`` marks a
// box size that can't be read (smaller than its header, or beyond 32 bits).
type BoxScan = { boxes: Box[]; complete: boolean; malformed: boolean };

function fourcc(view: DataView, offset: number): string {
  return String.fromCharCode(
    view.getUint8(offset),
    view.getUint8(offset + 1),
    view.getUint8(offset + 2),
    view.getUint8(offset + 3),
  );
}

// Reads sibling boxes in [start, end). ``complete`` is false when the range
// ends inside a box header or body, which for the top level means "wait for
// more bytes"; a malformed size stops the scan without reading past `end`.
function readBoxes(view: DataView, start: number, end: number): BoxScan {
  const boxes: Box[] = [];
  let offset = start;
  while (offset < end) {
    if (offset + 8 > end) return { boxes, complete: false, malformed: false };
    let size = view.getUint32(offset);
    let header = 8;
    if (size === 1) {
      if (offset + 16 > end) return { boxes, complete: false, malformed: false };
      if (view.getUint32(offset + 8) !== 0) return { boxes, complete: true, malformed: true };
      size = view.getUint32(offset + 12);
      header = 16;
    } else if (size === 0) {
      size = end - offset;
    }
    if (size < header) return { boxes, complete: true, malformed: true };
    if (offset + size > end) return { boxes, complete: false, malformed: false };
    boxes.push({ type: fourcc(view, offset + 4), start: offset, end: offset + size, body: offset + header });
    offset += size;
  }
  return { boxes, complete: true, malformed: false };
}

function child(view: DataView, parent: Box, type: string, skip = 0): Box | undefined {
  if (parent.body + skip > parent.end) return undefined;
  return readBoxes(view, parent.body + skip, parent.end).boxes.find((box) => box.type === type);
}

function hex2(value: number): string {
  return value.toString(16).toUpperCase().padStart(2, "0");
}

function avcCodec(view: DataView, entry: Box): string | null {
  // VisualSampleEntry: 78 bytes of fields precede its child boxes.
  const avcC = child(view, entry, "avcC", 78);
  if (!avcC || avcC.body + 4 > avcC.end) return null;
  const profile = view.getUint8(avcC.body + 1);
  const compatibility = view.getUint8(avcC.body + 2);
  const level = view.getUint8(avcC.body + 3);
  return `avc1.${hex2(profile)}${hex2(compatibility)}${hex2(level)}`;
}

function descriptor(view: DataView, offset: number, end: number): { tag: number; body: number; end: number } | null {
  if (offset + 2 > end) return null;
  const tag = view.getUint8(offset);
  let length = 0;
  let cursor = offset + 1;
  for (let index = 0; index < 4; index += 1) {
    if (cursor >= end) return null;
    const byte = view.getUint8(cursor);
    cursor += 1;
    length = (length << 7) | (byte & 0x7f);
    if ((byte & 0x80) === 0) break;
  }
  if (cursor + length > end) return null;
  return { tag, body: cursor, end: cursor + length };
}

function aacCodec(view: DataView, entry: Box): string {
  const fallback = "mp4a.40.2";
  // AudioSampleEntry: 28 bytes of fields precede its child boxes.
  const esds = child(view, entry, "esds", 28);
  if (!esds) return fallback;
  const es = descriptor(view, esds.body + 4, esds.end); // skip version + flags
  if (!es || es.tag !== 0x03 || es.body + 3 > es.end) return fallback;
  const flags = view.getUint8(es.body + 2);
  let cursor = es.body + 3;
  if (flags & 0x80) cursor += 2; // dependsOn_ES_ID
  if (flags & 0x40) {
    if (cursor >= es.end) return fallback;
    cursor += 1 + view.getUint8(cursor); // URL
  }
  if (flags & 0x20) cursor += 2; // OCR_ES_Id
  const config = descriptor(view, cursor, es.end);
  if (!config || config.tag !== 0x04 || config.body + 13 > config.end) return fallback;
  if (view.getUint8(config.body) !== 0x40) return fallback; // not MPEG-4 audio
  const specific = descriptor(view, config.body + 13, config.end);
  if (!specific || specific.tag !== 0x05 || specific.body >= specific.end) return fallback;
  let objectType = view.getUint8(specific.body) >> 3;
  if (objectType === 31 && specific.body + 1 < specific.end) {
    objectType = 32 + (((view.getUint8(specific.body) & 0x07) << 3) | (view.getUint8(specific.body + 1) >> 5));
  }
  return objectType > 0 ? `mp4a.40.${objectType}` : fallback;
}

export type InitSegmentParse =
  | { status: "incomplete" }
  | { status: "invalid" }
  | { status: "ready"; mime: string; derived: boolean };

/**
 * Derives the MediaSource type from the stream's own init segment. The codec
 * string comes from the `avcC` box (and `esds` for audio); a well-formed init
 * segment without them falls back to the observed Voice Live stream type.
 */
export function parseInitSegment(bytes: Uint8Array): InitSegmentParse {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const scan = readBoxes(view, 0, bytes.byteLength);
  const first = scan.boxes[0];
  if (!first) {
    if (bytes.byteLength >= 8 && fourcc(view, 4) !== "ftyp") return { status: "invalid" };
    return bytes.byteLength > MAX_AVATAR_INIT_BYTES ? { status: "invalid" } : { status: "incomplete" };
  }
  if (first.type !== "ftyp" || scan.malformed) return { status: "invalid" };
  const moov = scan.boxes.find((box) => box.type === "moov");
  if (!moov) {
    // More top-level boxes may still arrive: a delta can end exactly after ftyp.
    // Only media before the moov, or an init that outgrows its bound, is invalid.
    if (scan.boxes.some((box) => box.type === "moof" || box.type === "mdat")) {
      return { status: "invalid" };
    }
    return bytes.byteLength > MAX_AVATAR_INIT_BYTES ? { status: "invalid" } : { status: "incomplete" };
  }
  let video: string | null = null;
  let audio: string | null = null;
  for (const trak of readBoxes(view, moov.body, moov.end).boxes.filter((box) => box.type === "trak")) {
    const mdia = child(view, trak, "mdia");
    const minf = mdia && child(view, mdia, "minf");
    const stbl = minf && child(view, minf, "stbl");
    const stsd = stbl && child(view, stbl, "stsd");
    if (!stsd || stsd.body + 8 > stsd.end) continue;
    // stsd is a full box: version/flags and an entry count precede the entries.
    for (const entry of readBoxes(view, stsd.body + 8, stsd.end).boxes) {
      if ((entry.type === "avc1" || entry.type === "avc3") && video === null) {
        video = avcCodec(view, entry);
      } else if (entry.type === "mp4a" && audio === null) {
        audio = aacCodec(view, entry);
      }
    }
  }
  if (video === null) return { status: "ready", mime: AVATAR_FALLBACK_MIME, derived: false };
  const codecs = [video, ...(audio ? [audio] : [])].join(", ");
  return { status: "ready", mime: `video/mp4; codecs="${codecs}"`, derived: true };
}

// --- MediaSource abstraction (real browser or test double) -------------------

export interface TimeRangesLike {
  readonly length: number;
  start(index: number): number;
  end(index: number): number;
}

export interface SourceBufferLike {
  readonly updating: boolean;
  readonly buffered: TimeRangesLike;
  appendBuffer(data: Uint8Array): void;
  remove(start: number, end: number): void;
  addEventListener(type: "updateend" | "error", listener: () => void): void;
  removeEventListener(type: "updateend" | "error", listener: () => void): void;
}

export interface MediaSourceLike {
  readonly readyState: string;
  addSourceBuffer(type: string): SourceBufferLike;
  endOfStream?(): void;
  addEventListener(type: "sourceopen", listener: () => void): void;
}

export interface AvatarVideoElement {
  src: string;
  currentTime: number;
  readonly paused: boolean;
  play(): Promise<void> | void;
  removeAttribute(name: string): void;
  load(): void;
}

export interface AvatarVideoEnvironment {
  createMediaSource(): MediaSourceLike | null;
  isTypeSupported(type: string): boolean;
  createObjectURL(source: MediaSourceLike): string;
  revokeObjectURL(url: string): void;
}

type MediaSourceConstructor = {
  new (): MediaSource;
  isTypeSupported(type: string): boolean;
};

export function browserAvatarVideoEnvironment(): AvatarVideoEnvironment | null {
  if (typeof window === "undefined") return null;
  const Source = (window as unknown as { MediaSource?: MediaSourceConstructor }).MediaSource;
  if (typeof Source !== "function" || typeof Source.isTypeSupported !== "function") return null;
  return {
    createMediaSource: () => new Source() as unknown as MediaSourceLike,
    isTypeSupported: (type) => Source.isTypeSupported(type),
    createObjectURL: (source) => URL.createObjectURL(source as unknown as MediaSource),
    revokeObjectURL: (url) => URL.revokeObjectURL(url),
  };
}

type ExitableDocument = Document & {
  exitPictureInPicture?: () => Promise<void>;
  pictureInPictureElement?: Element | null;
};

// Some engines return nothing, or throw, instead of a promise.
function quietly(run: (() => Promise<void> | void) | undefined): void {
  try {
    const result = run?.();
    if (result && typeof (result as Promise<void>).catch === "function") {
      (result as Promise<void>).catch(() => {});
    }
  } catch {
    /* not supported here */
  }
}

/**
 * Keep the avatar video inside its stage, where the AI-generated label is
 * drawn. Picture-in-picture, fullscreen, remote playback and the context menu
 * are disabled, and entering either mode anyway exits it at once.
 */
export function hardenAvatarVideoElement(element: HTMLVideoElement): void {
  const doc = element.ownerDocument as ExitableDocument;
  element.controls = false;
  // Both IDL properties reflect these content attributes where they are supported.
  element.setAttribute("disablepictureinpicture", "");
  element.setAttribute("disableremoteplayback", "");
  element.setAttribute("controlslist", "nofullscreen noremoteplayback nodownload");
  element.addEventListener("contextmenu", (event) => event.preventDefault());
  element.addEventListener("enterpictureinpicture", () => {
    if (doc.pictureInPictureElement === element) quietly(doc.exitPictureInPicture?.bind(doc));
  });
  element.addEventListener("fullscreenchange", () => {
    if (doc.fullscreenElement === element) quietly(doc.exitFullscreen?.bind(doc));
  });
}

/** Whether this browser can play the Voice Live avatar stream at all. */
export function supportsAvatarVideo(
  env: AvatarVideoEnvironment | null = browserAvatarVideoEnvironment(),
): boolean {
  try {
    return env !== null && env.isTypeSupported(AVATAR_FALLBACK_MIME);
  } catch {
    return false;
  }
}

function decodeBase64(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function concat(parts: Uint8Array[], total: number): Uint8Array {
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const part of parts) {
    joined.set(part, offset);
    offset += part.length;
  }
  return joined;
}

function isQuotaExceeded(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { name?: unknown }).name === "QuotaExceededError"
  );
}

type Operation =
  | { kind: "append"; data: Uint8Array; retried: boolean }
  | { kind: "remove"; start: number; end: number };

export interface AvatarVideoPlayerOptions {
  maxQueuedChunks?: number;
  maxQueuedBytes?: number;
  retainBehindSeconds?: number;
  maxLagSeconds?: number;
  liveEdgeSeconds?: number;
  onFailure?: (reason: AvatarVideoFailure) => void;
  onPlaybackBlocked?: () => void;
  onFirstFrame?: () => void;
}

export class AvatarVideoPlayer {
  private readonly queue: Operation[] = [];
  private queuedChunks = 0;
  private queuedBytes = 0;
  private inFlight: Operation | null = null;
  private pendingInit: Uint8Array[] = [];
  private pendingInitBytes = 0;
  private mime: string | null = null;
  private mediaSource: MediaSourceLike | null = null;
  private sourceBuffer: SourceBufferLike | null = null;
  private objectUrl: string | null = null;
  private opened = false;
  private started = false;
  // Set by a NotAllowedError: only resume(), from a user gesture, retries play().
  private playbackBlocked = false;
  private failure: AvatarVideoFailure | null = null;
  private destroyed = false;
  private readonly maxQueuedChunks: number;
  private readonly maxQueuedBytes: number;
  private readonly retainBehindSeconds: number;
  private readonly maxLagSeconds: number;
  private readonly liveEdgeSeconds: number;

  constructor(
    private readonly video: AvatarVideoElement,
    private readonly env: AvatarVideoEnvironment,
    private readonly options: AvatarVideoPlayerOptions = {},
  ) {
    this.maxQueuedChunks = options.maxQueuedChunks ?? DEFAULT_MAX_QUEUED_CHUNKS;
    this.maxQueuedBytes = options.maxQueuedBytes ?? DEFAULT_MAX_QUEUED_BYTES;
    this.retainBehindSeconds = options.retainBehindSeconds ?? DEFAULT_RETAIN_BEHIND_SECONDS;
    this.maxLagSeconds = options.maxLagSeconds ?? DEFAULT_MAX_LAG_SECONDS;
    this.liveEdgeSeconds = options.liveEdgeSeconds ?? DEFAULT_LIVE_EDGE_SECONDS;
  }

  get failed(): AvatarVideoFailure | null {
    return this.failure;
  }

  /** The derived MediaSource type, once the init segment has arrived. */
  get type(): string | null {
    return this.mime;
  }

  /** Queued plus in-flight operations, for diagnostics and tests. */
  get backlog(): { chunks: number; bytes: number } {
    return { chunks: this.queuedChunks, bytes: this.queuedBytes };
  }

  /**
   * Attach the MediaSource and ask to play. Call it inside the user's gesture
   * (the Voice Live start button) so the audio track may autoplay later.
   */
  prime(): void {
    if (this.destroyed || this.failure || this.mediaSource) return;
    let source: MediaSourceLike | null;
    try {
      source = this.env.createMediaSource();
    } catch {
      source = null;
    }
    if (!source) {
      this.fail("unsupported");
      return;
    }
    this.mediaSource = source;
    source.addEventListener("sourceopen", this.onSourceOpen);
    this.objectUrl = this.env.createObjectURL(source);
    this.video.src = this.objectUrl;
    this.requestPlay();
  }

  /** Feed one `response.video.delta` payload, in arrival order. */
  push(base64: string): void {
    if (this.destroyed || this.failure) return;
    if (base64.length > MAX_AVATAR_DELTA_CHARS) {
      this.fail("stream_invalid");
      return;
    }
    let bytes: Uint8Array;
    try {
      bytes = decodeBase64(base64);
    } catch {
      this.fail("stream_invalid");
      return;
    }
    if (bytes.length === 0) return;
    if (this.mime === null) {
      this.collectInit(bytes);
      return;
    }
    this.enqueueAppend(bytes);
  }

  /** Skip buffered frames, for example when the user barges in. */
  jumpToLiveEdge(): void {
    this.keepNearLiveEdge(0);
  }

  /** Retry playback after the browser blocked autoplay with sound. */
  resume(): void {
    this.playbackBlocked = false;
    this.requestPlay();
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.queue.length = 0;
    this.queuedChunks = 0;
    this.queuedBytes = 0;
    this.inFlight = null;
    this.pendingInit = [];
    if (this.sourceBuffer) {
      this.sourceBuffer.removeEventListener("updateend", this.onUpdateEnd);
      this.sourceBuffer.removeEventListener("error", this.onError);
    }
    try {
      if (this.mediaSource?.readyState === "open") this.mediaSource.endOfStream?.();
    } catch {
      /* already ended */
    }
    if (this.objectUrl) this.env.revokeObjectURL(this.objectUrl);
    this.objectUrl = null;
    try {
      this.video.removeAttribute("src");
      this.video.load();
    } catch {
      /* detached element */
    }
  }

  // --- internals -------------------------------------------------------------

  private collectInit(bytes: Uint8Array): void {
    // The init segment may span deltas: collect until a complete moov arrives.
    this.pendingInit.push(bytes);
    this.pendingInitBytes += bytes.length;
    const joined = concat(this.pendingInit, this.pendingInitBytes);
    const parsed = parseInitSegment(joined);
    if (parsed.status === "incomplete") {
      if (this.pendingInitBytes > MAX_AVATAR_INIT_BYTES) this.fail("stream_invalid");
      return;
    }
    if (parsed.status === "invalid") {
      this.fail("stream_invalid");
      return;
    }
    let supported = false;
    try {
      supported = this.env.isTypeSupported(parsed.mime);
    } catch {
      supported = false;
    }
    if (!supported) {
      this.fail("codec_unsupported");
      return;
    }
    this.mime = parsed.mime;
    this.pendingInit = [];
    this.pendingInitBytes = 0;
    this.enqueueAppend(joined);
  }

  private enqueueAppend(bytes: Uint8Array): void {
    if (
      this.queuedChunks + 1 > this.maxQueuedChunks ||
      this.queuedBytes + bytes.length > this.maxQueuedBytes
    ) {
      // The socket cannot be slowed down, and dropping a fragment would corrupt
      // decoding until the next keyframe, so a backlog ends the avatar instead.
      this.fail("video_backlog");
      return;
    }
    this.queue.push({ kind: "append", data: bytes, retried: false });
    this.queuedChunks += 1;
    this.queuedBytes += bytes.length;
    this.pump();
  }

  private readonly onSourceOpen = (): void => {
    this.opened = true;
    this.pump();
  };

  private readonly onUpdateEnd = (): void => {
    const done = this.inFlight;
    this.inFlight = null;
    if (done?.kind === "append") {
      this.queuedChunks -= 1;
      this.queuedBytes -= done.data.length;
      if (!this.started) {
        this.started = true;
        this.options.onFirstFrame?.();
      }
      this.keepNearLiveEdge(this.maxLagSeconds);
      this.scheduleEviction(false);
    }
    this.pump();
  };

  private readonly onError = (): void => {
    this.fail("append_failed");
  };

  private pump(): void {
    if (this.destroyed || this.failure || !this.opened || !this.mediaSource) return;
    if (this.inFlight) return;
    if (!this.sourceBuffer) {
      if (this.mime === null || this.queue.length === 0) return;
      try {
        this.sourceBuffer = this.mediaSource.addSourceBuffer(this.mime);
      } catch {
        this.fail("codec_unsupported");
        return;
      }
      this.sourceBuffer.addEventListener("updateend", this.onUpdateEnd);
      this.sourceBuffer.addEventListener("error", this.onError);
    }
    const buffer = this.sourceBuffer;
    if (buffer.updating) return;
    const operation = this.queue.shift();
    if (!operation) return;
    this.inFlight = operation;
    try {
      if (operation.kind === "append") buffer.appendBuffer(operation.data);
      else buffer.remove(operation.start, operation.end);
    } catch (error) {
      this.inFlight = null;
      if (operation.kind === "append" && !operation.retried && isQuotaExceeded(error)) {
        // Free played media first, then retry this exact fragment once, in place.
        operation.retried = true;
        this.queue.unshift(operation);
        if (this.scheduleEviction(true)) {
          this.pump();
          return;
        }
      }
      this.fail("append_failed");
    }
  }

  private scheduleEviction(urgent: boolean): boolean {
    const buffer = this.sourceBuffer;
    if (!buffer || buffer.buffered.length === 0) return false;
    if (this.queue.some((operation) => operation.kind === "remove")) return false;
    const start = buffer.buffered.start(0);
    const keep = urgent ? 1 : this.retainBehindSeconds;
    const cutoff = this.video.currentTime - keep;
    if (cutoff - start <= (urgent ? 0 : 1)) return false;
    const removal: Operation = { kind: "remove", start, end: cutoff };
    if (urgent) this.queue.unshift(removal);
    else this.queue.push(removal);
    return true;
  }

  private keepNearLiveEdge(maxLag: number): void {
    const buffer = this.sourceBuffer;
    if (!buffer || buffer.buffered.length === 0) return;
    const last = buffer.buffered.length - 1;
    const end = buffer.buffered.end(last);
    const start = buffer.buffered.start(last);
    try {
      if (end - this.video.currentTime > maxLag + this.liveEdgeSeconds) {
        this.video.currentTime = Math.max(start, end - this.liveEdgeSeconds);
      }
      if (this.video.paused) this.requestPlay();
    } catch {
      /* the element may be detached during teardown */
    }
  }

  private requestPlay(): void {
    // Every completed append asks a paused element to play. Once the browser has
    // refused sound without a gesture, asking again only repeats the refusal.
    if (this.playbackBlocked || this.destroyed) return;
    try {
      const result = this.video.play();
      if (result && typeof (result as Promise<void>).catch === "function") {
        (result as Promise<void>).catch((error: unknown) => {
          const name = (error as { name?: unknown } | null)?.name;
          if (name !== "NotAllowedError" || this.playbackBlocked || this.destroyed) return;
          this.playbackBlocked = true;
          this.options.onPlaybackBlocked?.();
        });
      }
    } catch {
      /* play() is unimplemented in some environments */
    }
  }

  private fail(reason: AvatarVideoFailure): void {
    if (this.failure || this.destroyed) return;
    this.failure = reason;
    this.queue.length = 0;
    this.queuedChunks = 0;
    this.queuedBytes = 0;
    this.pendingInit = [];
    this.pendingInitBytes = 0;
    this.options.onFailure?.(reason);
  }
}
