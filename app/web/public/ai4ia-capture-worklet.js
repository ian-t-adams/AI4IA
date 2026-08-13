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
