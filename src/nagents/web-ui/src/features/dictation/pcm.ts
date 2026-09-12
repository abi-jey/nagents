export const SAMPLE_RATE = 16000;

// Integrate source samples into 16 kHz windows. Integer time units keep 44.1 kHz
// and arbitrary chunk boundaries exact; memory is bounded by the output duration.
export class WavEncoder {
  private readonly pcm: Int16Array;
  private readonly maxInputFrames: number;
  private inputFrames = 0;
  private outputFrames = 0;
  private units = 0;
  private sum = 0;

  constructor(
    private readonly inputRate: number,
    maxSeconds: number,
    private readonly maxBytes: number,
  ) {
    if (
      !Number.isInteger(inputRate) || inputRate < 8000 || inputRate > 384000 ||
      !Number.isInteger(maxSeconds) || maxSeconds < 1 || maxSeconds > 300 ||
      !Number.isSafeInteger(maxBytes) || maxBytes < SAMPLE_RATE * maxSeconds * 2 + 44
    ) throw new Error("Unsupported audio rate or recording limits. Refresh settings.");
    this.maxInputFrames = inputRate * maxSeconds;
    this.pcm = new Int16Array(SAMPLE_RATE * maxSeconds);
  }

  push(samples: Float32Array) {
    if (this.inputFrames + samples.length > this.maxInputFrames)
      throw new Error("Recording exceeded its duration limit. Audio was discarded.");
    for (const raw of samples) {
      if (!Number.isFinite(raw)) throw new Error("Invalid microphone audio. Try recording again.");
      const sample = Math.max(-1, Math.min(1, raw));
      let remaining = SAMPLE_RATE;
      while (remaining > 0) {
        const taken = Math.min(remaining, this.inputRate - this.units);
        this.sum += sample * taken;
        this.units += taken;
        remaining -= taken;
        if (this.units === this.inputRate) {
          const value = this.sum / this.inputRate;
          this.pcm[this.outputFrames++] = Math.round(value * (value < 0 ? 32768 : 32767));
          this.sum = 0;
          this.units = 0;
        }
      }
    }
    this.inputFrames += samples.length;
  }

  finish(): Blob {
    if (!this.outputFrames) throw new Error("No audio was recorded. Try again or type your message.");
    const size = this.outputFrames * 2;
    const buffer = new ArrayBuffer(44 + size);
    if (buffer.byteLength > this.maxBytes) throw new Error("Recording exceeds the upload limit.");
    const view = new DataView(buffer);
    const tag = (offset: number, text: string) => {
      for (let index = 0; index < text.length; index++) view.setUint8(offset + index, text.charCodeAt(index));
    };
    tag(0, "RIFF");
    view.setUint32(4, 36 + size, true);
    tag(8, "WAVE");
    tag(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true); // PCM, not a compressed container with a WAV label.
    view.setUint16(22, 1, true);
    view.setUint32(24, SAMPLE_RATE, true);
    view.setUint32(28, SAMPLE_RATE * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    tag(36, "data");
    view.setUint32(40, size, true);
    for (let index = 0; index < this.outputFrames; index++)
      view.setInt16(44 + index * 2, this.pcm[index], true);
    return new Blob([buffer], { type: "audio/wav" });
  }
}
