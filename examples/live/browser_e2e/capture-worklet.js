// Test-only destination recorder. Never imported by the shipped application.
class VoiceE2ECapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(2048);
    this.used = 0;
    this.blockSampleOffset = 0;
    this.blockObservedFrame = 0;
    this.frameOrigin = undefined;
    this.expectedObservedFrame = undefined;
    this.samplesCaptured = 0;
    this.blocksCaptured = 0;
    this.sequence = 0;
    this.enabled = true;
    this.armed = false;
    this.startPending = false;
    this.port.onmessage = ({ data }) => {
      if (data === "arm" && !this.armed) {
        this.armed = true;
        this.startPending = true;
      }
      if (data === "stop") {
        this.flush();
        this.enabled = false;
        this.port.postMessage({
          type: "capture-stop",
          schema: 2,
          finalFrame: this.frameOrigin === undefined ? 0 : this.frameOrigin + this.samplesCaptured,
          observedFinalFrame: this.expectedObservedFrame ?? currentFrame,
          frameOrigin: this.frameOrigin ?? null,
          samplesCaptured: this.samplesCaptured,
          blocksCaptured: this.blocksCaptured,
          sampleRate,
          armed: this.armed,
        });
      }
    };
  }

  flush() {
    if (!this.used) return;
    const samples = this.block.slice(0, this.used);
    this.port.postMessage({
      schema: 2,
      frame: this.frameOrigin + this.blockSampleOffset,
      sampleOffset: this.blockSampleOffset,
      sequence: this.sequence,
      observedFrame: this.blockObservedFrame,
      sampleRate,
      samples,
    }, [samples.buffer]);
    this.used = 0;
    this.sequence += 1;
    this.blocksCaptured += 1;
  }

  process(inputs, outputs) {
    const input = inputs[0] || [];
    const output = outputs[0] || [];
    const length = output[0]?.length ?? input[0]?.length ?? 128;
    if (!this.enabled) return true;

    // Keep raw render-clock diagnostics separate from the monotonic sample stream.
    if (this.expectedObservedFrame !== undefined && currentFrame !== this.expectedObservedFrame) {
      this.flush();
      this.port.postMessage({
        type: "clock-discontinuity",
        schema: 2,
        fromFrame: this.expectedObservedFrame,
        toFrame: currentFrame,
        observedFrame: currentFrame,
        capturedSamples: this.samplesCaptured,
        sampleOffset: this.samplesCaptured,
        sequence: this.sequence,
        sampleRate,
        phase: this.armed ? "recording" : "warmup",
      });
    }
    this.expectedObservedFrame = currentFrame + length;
    if (!this.armed) return true;

    if (this.startPending) {
      this.startPending = false;
      this.frameOrigin = currentFrame;
      this.port.postMessage({
        type: "capture-start",
        schema: 2,
        frame: this.frameOrigin,
        frameOrigin: this.frameOrigin,
        observedFrame: currentFrame,
        sampleOffset: 0,
        sequence: 0,
        sampleRate,
      });
    }

    for (let i = 0; i < length; i++) {
      if (!this.used) {
        this.blockSampleOffset = this.samplesCaptured;
        this.blockObservedFrame = currentFrame + i;
      }
      let value = 0;
      for (const channel of input) value += channel[i] || 0;
      this.block[this.used++] = input.length ? value / input.length : 0;
      this.samplesCaptured += 1;
      if (this.used === this.block.length) this.flush();
    }
    // The virtual speaker terminates in silence. Captured samples are taken
    // after the app's playback worklet and volume control, before hardware output.
    return true;
  }
}
registerProcessor("ngn-voice-e2e-capture", VoiceE2ECapture);
