// Test-only destination recorder. Never imported by the shipped application.
class VoiceE2ECapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(2048);
    this.used = 0;
    this.start = 0;
    this.expectedFrame = undefined;
    this.enabled = true;
    this.armed = false;
    this.startPending = false;
    this.port.onmessage = ({ data }) => {
      if (data === "arm" && !this.armed) { this.armed = true; this.startPending = true; }
      if (data === "stop") {
        this.flush(); this.enabled = false;
        this.port.postMessage({ type: "capture-stop", finalFrame: this.expectedFrame ?? currentFrame,
          sampleRate, armed: this.armed });
      }
    };
  }
  flush() {
    if (!this.used) return;
    const samples = this.block.slice(0, this.used);
    this.port.postMessage({ frame: this.start, sampleRate, samples }, [samples.buffer]);
    this.used = 0;
  }
  process(inputs, outputs) {
    const input = inputs[0] || [];
    const length = outputs[0]?.[0]?.length || 128;
    if (!this.enabled) return true;
    // A render-clock jump can happen inside a partially accumulated block.
    // Flush first so the WAV exporter cannot place later samples before their
    // actual render frame. Report every jump, including silent initialization.
    if (this.expectedFrame !== undefined && currentFrame !== this.expectedFrame) {
      this.flush();
      this.port.postMessage({ type: "clock-discontinuity", fromFrame: this.expectedFrame,
        toFrame: currentFrame, sampleRate, phase: this.armed ? "recording" : "warmup" });
    }
    this.expectedFrame = currentFrame + length;
    if (!this.armed) return true;
    if (this.startPending) {
      this.startPending = false;
      this.port.postMessage({ type: "capture-start", frame: currentFrame, sampleRate });
    }
    for (let i = 0; i < length; i++) {
      if (!this.used) this.start = currentFrame + i;
      let value = 0;
      for (const channel of input) value += channel[i] || 0;
      this.block[this.used++] = input.length ? value / input.length : 0;
      if (this.used === this.block.length) this.flush();
    }
    // The virtual speaker terminates in silence. Captured samples are taken
    // after the app's playback worklet and volume node, before hardware output.
    return true;
  }
}
registerProcessor("ngn-voice-e2e-capture", VoiceE2ECapture);
