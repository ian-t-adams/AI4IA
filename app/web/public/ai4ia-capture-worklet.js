class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._count = 0;
    this._target = 2400;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel && channel.length) {
      this._chunks.push(channel.slice(0));
      this._count += channel.length;
      if (this._count >= this._target) {
        const merged = new Float32Array(this._count);
        let offset = 0;
        for (const chunk of this._chunks) {
          merged.set(chunk, offset);
          offset += chunk.length;
        }
        this.port.postMessage(merged, [merged.buffer]);
        this._chunks = [];
        this._count = 0;
      }
    }
    return true;
  }
}

registerProcessor("ai4ia-capture", CaptureProcessor);

function toPcm16(sample) {
  const s = Math.max(-1, Math.min(1, sample));
  return s < 0 ? s * 0x8000 : s * 0x7fff;
}

// Live-Reference AEC: input 0 is the microphone and input 1 is what the page
// plays. Each ~100 ms chunk is interleaved stereo PCM16 with the microphone
// sample first ([mic0, ref0, mic1, ref1, ...]), the layout Voice Live expects
// for `reference_source: "client"` with `channels: 2`. Both inputs are read in
// the same render quantum, so every reference sample is aligned with its
// microphone sample. A silent or unconnected reference sends zeros.
class StereoCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this._chunks = [];
    this._count = 0;
    this._target = 2400;
  }

  process(inputs) {
    const mic = inputs[0] && inputs[0][0];
    if (mic && mic.length) {
      const reference = inputs[1] || [];
      const frames = new Int16Array(mic.length * 2);
      for (let i = 0; i < mic.length; i++) {
        let sum = 0;
        for (let channel = 0; channel < reference.length; channel++) {
          sum += reference[channel][i] || 0;
        }
        frames[2 * i] = toPcm16(mic[i]);
        frames[2 * i + 1] = toPcm16(reference.length ? sum / reference.length : 0);
      }
      this._chunks.push(frames);
      this._count += mic.length;
      if (this._count >= this._target) {
        const merged = new Int16Array(this._count * 2);
        let offset = 0;
        for (const chunk of this._chunks) {
          merged.set(chunk, offset);
          offset += chunk.length;
        }
        this.port.postMessage(merged, [merged.buffer]);
        this._chunks = [];
        this._count = 0;
      }
    }
    return true;
  }
}

registerProcessor("ai4ia-capture-stereo", StereoCaptureProcessor);
