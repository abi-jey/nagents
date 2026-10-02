// Microphone PCM16 at 24 kHz, sent in 20 ms frames over the same-origin relay.
class LiveCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.phase = 0;
    this.frame = new Int16Array(480);
    this.offset = 0;
  }

  process(inputs, outputs) {
    const samples = inputs[0]?.[0];
    if (samples) for (const value of samples) {
      this.phase += 24000;
      // Bluetooth and low-rate devices can run below 24 kHz. Emit every
      // required output sample so those devices retain real-time PCM timing.
      while (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        this.frame[this.offset++] = Math.max(-32768, Math.min(32767, Math.round(value * 32768)));
        if (this.offset === this.frame.length) {
          this.port.postMessage(this.frame.buffer, [this.frame.buffer]);
          this.frame = new Int16Array(480);
          this.offset = 0;
        }
      }
    }
    for (const output of outputs[0] || []) output.fill(0);
    return true;
  }
}

registerProcessor("ngn-live-capture", LiveCapture);
