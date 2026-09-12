// Plain JavaScript: Vite emits this self-hosted module as an asset, never a
// data/blob URL. The output stays silent; microphone samples only go to the port.
class PcmCapture extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.maxFrames = options.processorOptions.maxFrames;
    this.frames = 0;
    this.used = 0;
    this.buffer = new Float32Array(2048);
    this.finished = false;
    this.port.onmessage = (event) => {
      if (event.data?.type === "stop") this.finish(false);
    };
  }

  flush() {
    if (!this.used) return;
    const samples = this.buffer.slice(0, this.used);
    this.port.postMessage({ type: "samples", samples }, [samples.buffer]);
    this.used = 0;
  }

  finish(limited) {
    if (this.finished) return;
    this.finished = true;
    this.flush();
    this.port.postMessage({ type: "stopped", limited });
  }

  process(inputs) {
    if (this.finished) return false;
    const channels = inputs[0];
    if (!channels?.length) return true;
    const length = Math.min(channels[0].length, this.maxFrames - this.frames);
    for (let frame = 0; frame < length; frame++) {
      let sample = 0;
      for (const channel of channels) sample += channel[frame] || 0;
      this.buffer[this.used++] = sample / channels.length;
      if (this.used === this.buffer.length) this.flush();
    }
    this.frames += length;
    if (this.frames >= this.maxFrames) this.finish(true);
    return !this.finished;
  }
}

registerProcessor("ngn-pcm-capture", PcmCapture);
