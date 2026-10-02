import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

import {
  CAPTURE_PROCESSOR,
  CAPTURE_WORKLET_PATH,
  floatTo16BitPCM,
  STEREO_CAPTURE_PROCESSOR,
} from "./voiceLive";

// The real same-origin module the browser loads, run in a minimal
// AudioWorkletGlobalScope: the processors it registers and what they post.
interface Posted {
  data: unknown;
  transfer: unknown[] | undefined;
}
interface Processor {
  port: { posted: Posted[] };
  process(inputs: Float32Array[][]): boolean;
}

function loadWorklet(): Map<string, new () => Processor> {
  const source = readFileSync(new URL(`../../public${CAPTURE_WORKLET_PATH}`, import.meta.url), "utf8");
  const processors = new Map<string, new () => Processor>();
  class AudioWorkletProcessor {
    port = {
      posted: [] as Posted[],
      postMessage(data: unknown, transfer?: unknown[]) {
        this.posted.push({ data, transfer });
      },
    };
  }
  new Function("AudioWorkletProcessor", "registerProcessor", source)(
    AudioWorkletProcessor,
    (name: string, processor: new () => Processor) => processors.set(name, processor),
  );
  return processors;
}

const QUANTUM = 128;
// 19 render quanta are the first to reach the 2400-frame (100 ms) chunk.
const QUANTA_PER_CHUNK = Math.ceil(2400 / QUANTUM);

// Distinct, sample-indexed values so any reordering or offset shows.
const micAt = (frame: number) => Math.sin(frame / 7) * 0.8;
const refAt = (frame: number) => -0.5 + ((frame * 37) % 101) / 101;

function quantum(start: number, value: (frame: number) => number): Float32Array {
  return Float32Array.from({ length: QUANTUM }, (_, i) => value(start + i));
}

function stereoProcessor(): Processor {
  const Stereo = loadWorklet().get(STEREO_CAPTURE_PROCESSOR);
  if (!Stereo) throw new Error("stereo processor not registered");
  return new Stereo();
}

describe("capture worklet", () => {
  it("registers the mono and the Live-Reference AEC processors", () => {
    expect([...loadWorklet().keys()]).toEqual([CAPTURE_PROCESSOR, STEREO_CAPTURE_PROCESSOR]);
  });

  it("interleaves each microphone sample with the reference sample of the same frame", () => {
    const processor = stereoProcessor();
    for (let q = 0; q < QUANTA_PER_CHUNK; q += 1) {
      expect(processor.process([[quantum(q * QUANTUM, micAt)], [quantum(q * QUANTUM, refAt)]])).toBe(true);
      // Nothing is posted before the 100 ms chunk is complete.
      expect(processor.port.posted).toHaveLength(q + 1 === QUANTA_PER_CHUNK ? 1 : 0);
    }
    const [{ data, transfer }] = processor.port.posted;
    const frames = QUANTA_PER_CHUNK * QUANTUM;
    expect(data).toBeInstanceOf(Int16Array);
    const stereo = data as Int16Array;
    expect(stereo).toHaveLength(frames * 2);
    // Whole sample pairs only: Voice Live needs a byte length divisible by 4.
    expect(stereo.byteLength % 4).toBe(0);
    expect(transfer).toEqual([stereo.buffer]);
    const mic = floatTo16BitPCM(Float32Array.from({ length: frames }, (_, i) => micAt(i)));
    const ref = floatTo16BitPCM(Float32Array.from({ length: frames }, (_, i) => refAt(i)));
    // Channel 0 is the microphone and channel 1 the reference, microphone first.
    expect(stereo.filter((_, i) => i % 2 === 0)).toEqual(mic);
    expect(stereo.filter((_, i) => i % 2 === 1)).toEqual(ref);
  });

  it("sends the same microphone samples as the mono capture (control)", () => {
    const Mono = loadWorklet().get(CAPTURE_PROCESSOR)!;
    const mono = new Mono();
    const stereo = stereoProcessor();
    for (let q = 0; q < QUANTA_PER_CHUNK; q += 1) {
      mono.process([[quantum(q * QUANTUM, micAt)]]);
      stereo.process([[quantum(q * QUANTUM, micAt)], [quantum(q * QUANTUM, refAt)]]);
    }
    const monoPcm = floatTo16BitPCM(mono.port.posted[0].data as Float32Array);
    const stereoPcm = stereo.port.posted[0].data as Int16Array;
    expect(stereoPcm.filter((_, i) => i % 2 === 0)).toEqual(monoPcm);
    // The reference channel is not the microphone.
    expect(stereoPcm.filter((_, i) => i % 2 === 1)).not.toEqual(monoPcm);
  });

  it.each([
    ["no reference input", (q: number) => [[quantum(q * QUANTUM, micAt)]]],
    ["an inactive reference input", (q: number) => [[quantum(q * QUANTUM, micAt)], []]],
  ])("sends silence as the reference with %s, and still sends the microphone", (_label, inputs) => {
    const processor = stereoProcessor();
    for (let q = 0; q < QUANTA_PER_CHUNK; q += 1) processor.process(inputs(q));
    const stereo = processor.port.posted[0].data as Int16Array;
    expect(stereo.filter((_, i) => i % 2 === 1).every((sample) => sample === 0)).toBe(true);
    expect(stereo.filter((_, i) => i % 2 === 0).some((sample) => sample !== 0)).toBe(true);
  });

  it("downmixes a multi-channel reference to its average", () => {
    const processor = stereoProcessor();
    const left = (frame: number) => refAt(frame);
    const right = (frame: number) => -refAt(frame) * 0.5;
    for (let q = 0; q < QUANTA_PER_CHUNK; q += 1) {
      processor.process([
        [quantum(q * QUANTUM, micAt)],
        [quantum(q * QUANTUM, left), quantum(q * QUANTUM, right)],
      ]);
    }
    const stereo = processor.port.posted[0].data as Int16Array;
    const frames = QUANTA_PER_CHUNK * QUANTUM;
    const expected = floatTo16BitPCM(
      Float32Array.from({ length: frames }, (_, i) => (left(i) + right(i)) / 2),
    );
    expect(stereo.filter((_, i) => i % 2 === 1)).toEqual(expected);
  });

  it("clamps out-of-range samples like the mono path", () => {
    const processor = stereoProcessor();
    for (let q = 0; q < QUANTA_PER_CHUNK; q += 1) {
      processor.process([
        [new Float32Array(QUANTUM).fill(2)],
        [new Float32Array(QUANTUM).fill(-3)],
      ]);
    }
    const stereo = processor.port.posted[0].data as Int16Array;
    expect([stereo[0], stereo[1]]).toEqual([0x7fff, -0x8000]);
  });

  it("posts nothing until the microphone is connected, and restarts each chunk after posting", () => {
    const processor = stereoProcessor();
    for (let q = 0; q < QUANTA_PER_CHUNK * 2; q += 1) {
      processor.process([[], [quantum(q * QUANTUM, refAt)]]);
    }
    // The reference alone is never audio to send.
    expect(processor.port.posted).toHaveLength(0);
    for (let q = 0; q < QUANTA_PER_CHUNK * 2; q += 1) {
      processor.process([[quantum(q * QUANTUM, micAt)], [quantum(q * QUANTUM, refAt)]]);
    }
    expect(processor.port.posted).toHaveLength(2);
    for (const { data } of processor.port.posted) {
      expect(data as Int16Array).toHaveLength(QUANTA_PER_CHUNK * QUANTUM * 2);
    }
  });
});
