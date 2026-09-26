import { describe, expect, it, vi } from "vitest";

import {
  AVATAR_FALLBACK_MIME,
  AvatarVideoPlayer,
  MAX_AVATAR_DELTA_CHARS,
  parseInitSegment,
  supportsAvatarVideo,
  type AvatarVideoEnvironment,
  type AvatarVideoFailure,
  type SourceBufferLike,
} from "./avatarVideo";
import {
  concatBytes,
  fragmentSequence,
  initSegment,
  mediaFragment,
  toBase64,
} from "../../test-fixtures/avatarFmp4";

class FakeTimeRanges {
  constructor(private readonly ranges: [number, number][]) {}
  get length() {
    return this.ranges.length;
  }
  start(index: number) {
    return this.ranges[index][0];
  }
  end(index: number) {
    return this.ranges[index][1];
  }
}

class FakeSourceBuffer implements SourceBufferLike {
  updating = false;
  appended: Uint8Array[] = [];
  removed: [number, number][] = [];
  ranges: [number, number][] = [];
  quotaFailures = 0;
  private listeners = { updateend: new Set<() => void>(), error: new Set<() => void>() };

  get buffered() {
    return new FakeTimeRanges(this.ranges);
  }

  appendBuffer(data: Uint8Array) {
    if (this.updating) throw new Error("InvalidStateError: still updating");
    if (this.quotaFailures > 0) {
      this.quotaFailures -= 1;
      const error = new Error("The SourceBuffer is full.");
      error.name = "QuotaExceededError";
      throw error;
    }
    this.updating = true;
    this.appended.push(data);
  }

  remove(start: number, end: number) {
    if (this.updating) throw new Error("InvalidStateError: still updating");
    this.updating = true;
    this.removed.push([start, end]);
  }

  // The test drives completion, like the browser's asynchronous updateend. The
  // spec clears `updating` first and delivers updateend in a later task.
  settle() {
    this.updating = false;
  }

  deliver() {
    for (const listener of [...this.listeners.updateend]) listener();
  }

  finish() {
    this.settle();
    this.deliver();
  }

  addEventListener(type: "updateend" | "error", listener: () => void) {
    this.listeners[type].add(listener);
  }

  removeEventListener(type: "updateend" | "error", listener: () => void) {
    this.listeners[type].delete(listener);
  }
}

class FakeMediaSource {
  readyState = "closed";
  types: string[] = [];
  buffers: FakeSourceBuffer[] = [];
  endOfStream = vi.fn(() => {
    this.readyState = "ended";
  });
  private openListeners: (() => void)[] = [];

  addSourceBuffer(type: string) {
    this.types.push(type);
    const buffer = new FakeSourceBuffer();
    this.buffers.push(buffer);
    return buffer;
  }

  addEventListener(_type: "sourceopen", listener: () => void) {
    this.openListeners.push(listener);
  }

  open() {
    this.readyState = "open";
    for (const listener of this.openListeners) listener();
  }
}

class FakeVideo {
  src = "";
  currentTime = 0;
  paused = true;
  play = vi.fn(() => Promise.resolve());
  removeAttribute = vi.fn((name: string) => {
    if (name === "src") this.src = "";
  });
  load = vi.fn();
}

function environment(supports: (type: string) => boolean = () => true) {
  const sources: FakeMediaSource[] = [];
  const env: AvatarVideoEnvironment = {
    createMediaSource: () => {
      const source = new FakeMediaSource();
      sources.push(source);
      return source;
    },
    isTypeSupported: vi.fn(supports),
    createObjectURL: vi.fn(() => "blob:avatar"),
    revokeObjectURL: vi.fn(),
  };
  return { env, sources };
}

function openPlayer(options: ConstructorParameters<typeof AvatarVideoPlayer>[2] = {}) {
  const video = new FakeVideo();
  const { env, sources } = environment();
  const failures: AvatarVideoFailure[] = [];
  const player = new AvatarVideoPlayer(video, env, {
    ...options,
    onFailure: (reason) => failures.push(reason),
  });
  player.prime();
  const source = sources[0];
  source.open();
  return { player, video, env, source, failures };
}

describe("parseInitSegment", () => {
  it("derives the codec string from avcC and esds", () => {
    expect(parseInitSegment(initSegment())).toEqual({
      status: "ready",
      mime: AVATAR_FALLBACK_MIME,
      derived: true,
    });
    expect(parseInitSegment(initSegment({ profile: 0x4d, compatibility: 0x40, level: 0x1f }))).toEqual({
      status: "ready",
      mime: 'video/mp4; codecs="avc1.4D401F, mp4a.40.2"',
      derived: true,
    });
    expect(parseInitSegment(initSegment({ audioObjectType: 5 }))).toMatchObject({
      mime: 'video/mp4; codecs="avc1.64001E, mp4a.40.5"',
    });
    expect(parseInitSegment(initSegment({ audioObjectType: null }))).toMatchObject({
      mime: 'video/mp4; codecs="avc1.64001E"',
    });
  });

  it("falls back to the observed stream type when avcC is missing", () => {
    expect(parseInitSegment(initSegment({ withAvcC: false }))).toEqual({
      status: "ready",
      mime: AVATAR_FALLBACK_MIME,
      derived: false,
    });
  });

  it("waits for a split moov and refuses streams that are not init segments", () => {
    const init = initSegment();
    expect(parseInitSegment(init.slice(0, 5))).toEqual({ status: "incomplete" });
    expect(parseInitSegment(init.slice(0, init.length - 10))).toEqual({ status: "incomplete" });
    expect(parseInitSegment(mediaFragment(1))).toEqual({ status: "invalid" });
    const ftypOnly = init.slice(0, new DataView(init.buffer).getUint32(0));
    expect(parseInitSegment(concatBytes(ftypOnly, mediaFragment(1)))).toEqual({ status: "invalid" });
  });
});

describe("supportsAvatarVideo", () => {
  it("needs MediaSource and the stream's codecs", () => {
    expect(supportsAvatarVideo(null)).toBe(false);
    expect(supportsAvatarVideo(environment(() => false).env)).toBe(false);
    expect(supportsAvatarVideo(environment((type) => type === AVATAR_FALLBACK_MIME).env)).toBe(true);
  });
});

describe("AvatarVideoPlayer", () => {
  it("appends strictly in arrival order, one operation at a time", () => {
    const { player, source } = openPlayer();
    const init = initSegment();
    player.push(toBase64(init));
    player.push(toBase64(mediaFragment(1)));
    player.push(toBase64(mediaFragment(2)));
    player.push(toBase64(mediaFragment(3)));
    expect(source.types).toEqual([AVATAR_FALLBACK_MIME]);
    const buffer = source.buffers[0];
    // Only the init segment is in flight until the browser finishes it.
    expect(buffer.appended).toEqual([init]);
    buffer.finish();
    buffer.finish();
    buffer.finish();
    expect(buffer.appended.map((data) => fragmentSequence(data))).toEqual([null, 1, 2, 3]);
    expect(player.backlog.chunks).toBe(1);
    buffer.finish();
    expect(player.backlog).toEqual({ chunks: 0, bytes: 0 });
  });

  it("waits for updateend even after updating clears, never overlapping operations", () => {
    const { player, source } = openPlayer();
    player.push(toBase64(initSegment()));
    const buffer = source.buffers[0];
    buffer.settle(); // updating is false, but updateend has not been delivered yet
    player.push(toBase64(mediaFragment(1)));
    expect(buffer.appended).toHaveLength(1);
    buffer.deliver();
    expect(buffer.appended.map((data) => fragmentSequence(data))).toEqual([null, 1]);
    buffer.finish();
    expect(player.backlog).toEqual({ chunks: 0, bytes: 0 });
  });

  it("collects an init segment split across deltas before choosing a codec", () => {
    const { player, source, failures } = openPlayer();
    const init = initSegment({ profile: 0x4d, compatibility: 0x40, level: 0x1f });
    player.push(toBase64(init.slice(0, 40)));
    expect(source.types).toEqual([]);
    player.push(toBase64(concatBytes(init.slice(40), mediaFragment(7))));
    expect(source.types).toEqual(['video/mp4; codecs="avc1.4D401F, mp4a.40.2"']);
    expect(source.buffers[0].appended).toEqual([concatBytes(init, mediaFragment(7))]);
    expect(failures).toEqual([]);
  });

  it("fails on a backlog past its bound instead of growing or dropping frames", () => {
    const { player, source, failures } = openPlayer({ maxQueuedChunks: 3 });
    player.push(toBase64(initSegment()));
    player.push(toBase64(mediaFragment(1)));
    player.push(toBase64(mediaFragment(2)));
    expect(failures).toEqual([]);
    player.push(toBase64(mediaFragment(3)));
    expect(failures).toEqual(["video_backlog"]);
    expect(player.backlog).toEqual({ chunks: 0, bytes: 0 });
    player.push(toBase64(mediaFragment(4)));
    const buffer = source.buffers[0];
    buffer.finish();
    expect(buffer.appended).toHaveLength(1);
    expect(failures).toEqual(["video_backlog"]);
  });

  it("keeps an in-bound stream flowing (control for the backlog bound)", () => {
    const { player, source, failures } = openPlayer({ maxQueuedChunks: 10 });
    player.push(toBase64(initSegment()));
    for (let sequence = 1; sequence <= 3; sequence += 1) {
      player.push(toBase64(mediaFragment(sequence)));
    }
    for (let index = 0; index < 4; index += 1) source.buffers[0].finish();
    expect(failures).toEqual([]);
    expect(source.buffers[0].appended).toHaveLength(4);
  });

  it("evicts played media on QuotaExceeded and retries the same fragment once", () => {
    const { player, source, video, failures } = openPlayer();
    player.push(toBase64(initSegment()));
    const buffer = source.buffers[0];
    buffer.finish();
    buffer.ranges = [[0, 20]];
    video.currentTime = 15;
    video.paused = false;
    buffer.quotaFailures = 1;
    player.push(toBase64(mediaFragment(9)));
    expect(buffer.removed).toEqual([[0, 14]]);
    buffer.finish();
    expect(buffer.appended.map((data) => fragmentSequence(data))).toEqual([null, 9]);
    expect(failures).toEqual([]);
    buffer.finish(); // fragment 9 lands; the routine retention eviction starts
    expect(buffer.removed).toHaveLength(2);
    buffer.finish(); // the retention eviction completes
    buffer.quotaFailures = 2;
    player.push(toBase64(mediaFragment(10)));
    expect(buffer.removed).toHaveLength(3); // the urgent eviction is in flight
    buffer.finish();
    expect(failures).toEqual(["append_failed"]);
  });

  it("keeps playback near the live edge and can jump to it", () => {
    const { player, source, video } = openPlayer({ liveEdgeSeconds: 0.25, maxLagSeconds: 1.5 });
    player.push(toBase64(initSegment()));
    const buffer = source.buffers[0];
    buffer.ranges = [[0, 5]];
    video.currentTime = 1;
    buffer.finish();
    expect(video.currentTime).toBe(4.75);
    buffer.ranges = [[0, 5.6]];
    player.jumpToLiveEdge();
    expect(video.currentTime).toBeCloseTo(5.35);
  });

  it("refuses an unsupported codec, a non-init stream and an oversized delta", () => {
    const video = new FakeVideo();
    const unsupported = environment((type) => type === AVATAR_FALLBACK_MIME);
    const reasons: AvatarVideoFailure[] = [];
    const player = new AvatarVideoPlayer(video, unsupported.env, {
      onFailure: (reason) => reasons.push(reason),
    });
    player.prime();
    unsupported.sources[0].open();
    player.push(toBase64(initSegment({ profile: 0x4d, compatibility: 0x40, level: 0x1f })));
    expect(reasons).toEqual(["codec_unsupported"]);
    expect(unsupported.sources[0].types).toEqual([]);

    const notInit = openPlayer();
    notInit.player.push(toBase64(mediaFragment(1)));
    expect(notInit.failures).toEqual(["stream_invalid"]);

    const oversized = openPlayer();
    oversized.player.push("A".repeat(MAX_AVATAR_DELTA_CHARS + 4));
    expect(oversized.failures).toEqual(["stream_invalid"]);
  });

  it("reports an environment without MediaSource as unsupported", () => {
    const reasons: AvatarVideoFailure[] = [];
    const env: AvatarVideoEnvironment = {
      createMediaSource: () => null,
      isTypeSupported: () => true,
      createObjectURL: vi.fn(() => "blob:x"),
      revokeObjectURL: vi.fn(),
    };
    new AvatarVideoPlayer(new FakeVideo(), env, { onFailure: (reason) => reasons.push(reason) }).prime();
    expect(reasons).toEqual(["unsupported"]);
  });

  it("primes playback inside the gesture and releases everything on destroy", () => {
    const { player, video, env, source } = openPlayer();
    expect(video.src).toBe("blob:avatar");
    expect(video.play).toHaveBeenCalledTimes(1);
    player.push(toBase64(initSegment()));
    player.destroy();
    expect(source.endOfStream).toHaveBeenCalled();
    expect(env.revokeObjectURL).toHaveBeenCalledWith("blob:avatar");
    expect(video.src).toBe("");
    player.push(toBase64(mediaFragment(1)));
    expect(source.buffers[0].appended).toHaveLength(1);
  });

  it("stops retrying blocked autoplay until the user resumes it", async () => {
    const video = new FakeVideo();
    const blocked = Object.assign(new Error("autoplay"), { name: "NotAllowedError" });
    video.play = vi.fn(() => Promise.reject(blocked));
    const onPlaybackBlocked = vi.fn();
    const { env, sources } = environment();
    const player = new AvatarVideoPlayer(video, env, { onPlaybackBlocked });
    player.prime();
    sources[0].open();
    await Promise.resolve();
    await Promise.resolve();
    expect(onPlaybackBlocked).toHaveBeenCalledTimes(1);
    player.push(toBase64(initSegment()));
    const buffer = sources[0].buffers[0];
    buffer.ranges = [[0, 1]];
    buffer.finish(); // appends complete while the element is still paused
    player.push(toBase64(mediaFragment(1)));
    buffer.finish();
    await Promise.resolve();
    await Promise.resolve();
    expect(video.play).toHaveBeenCalledTimes(1);
    expect(onPlaybackBlocked).toHaveBeenCalledTimes(1);
    video.play = vi.fn(() => Promise.resolve());
    player.resume();
    expect(video.play).toHaveBeenCalledTimes(1);
    // Control: once resumed, a paused element is asked to play after each append.
    player.push(toBase64(mediaFragment(2)));
    buffer.finish();
    expect(video.play).toHaveBeenCalledTimes(2);
  });

  it("asks the caller for a gesture when autoplay with sound is blocked", async () => {
    const video = new FakeVideo();
    const blocked = Object.assign(new Error("autoplay"), { name: "NotAllowedError" });
    video.play = vi.fn(() => Promise.reject(blocked));
    const onPlaybackBlocked = vi.fn();
    const { env } = environment();
    new AvatarVideoPlayer(video, env, { onPlaybackBlocked }).prime();
    await Promise.resolve();
    await Promise.resolve();
    expect(onPlaybackBlocked).toHaveBeenCalledTimes(1);
  });
});
