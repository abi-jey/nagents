import { silentSignal, type AudioFrame, type SignalFrame } from "./types.js";

export type PcmEncoding = "s16le" | "f32le";

/** Interleaved PCM. Format is fixed for the lifetime of a channel. */
export interface PcmFormat {
  encoding: PcmEncoding;
  sampleRate: number;
  channels: 1 | 2;
}

export interface PcmTiming {
  /** First frame's time, in seconds on the same clock supplied to createPcmAudio. */
  at: number;
}

export interface PcmChannelStats {
  format: Readonly<PcmFormat>;
  bufferedFrames: number;
  bufferedSeconds: number;
  pendingBytes: number;
  writtenFrames: number;
  acceptedFrames: number;
  overlapFrames: number;
  droppedFrames: number;
  discardedBytes: number;
  closed: boolean;
}

export interface PcmChannel {
  /** Copies bytes into a bounded queue. Missing timing appends at max(now, last end). */
  write(bytes: Uint8Array, timing?: PcmTiming): void;
  readonly writable: WritableStream<Uint8Array>;
  /** EOF closes writes and drains scheduled frames; abort/error discards pending audio. */
  consume(readable: ReadableStream<Uint8Array>, options?: { signal?: AbortSignal }): Promise<void>;
  /** Clears audio, partial frames and timestamp history. Does not reopen a closed stream. */
  reset(): void;
  /** Rejects further writes; already queued future frames remain available until their end. */
  close(): void;
  stats(): PcmChannelStats;
}

export interface PcmAudioOptions {
  inputFormat: PcmFormat;
  outputFormat: PcmFormat;
  /** Monotonic seconds; defaults to performance.now() / 1000. */
  clock?: () => number;
  /** Per-channel frame capacity. Oldest frames are dropped deterministically. Default: 2. */
  maxBufferedSeconds?: number;
}

export interface PcmAudio {
  readonly input: PcmChannel;
  readonly output: PcmChannel;
  /** Renderer bridge. Applications feed PCM; they do not calculate levels or frequency bands. */
  sample(): AudioFrame;
  /** Cancels readers and releases both queues. Idempotent. */
  dispose(): void;
}

const WINDOW_SECONDS = .032;
const EPSILON_FRAMES = 1e-6;

function validateFormat(format: PcmFormat): Readonly<PcmFormat> {
  if (format.encoding !== "s16le" && format.encoding !== "f32le") throw new TypeError("PCM encoding must be s16le or f32le.");
  if (!Number.isInteger(format.sampleRate) || format.sampleRate < 8000 || format.sampleRate > 192000) throw new RangeError("PCM sampleRate must be an integer from 8000 to 192000.");
  if (format.channels !== 1 && format.channels !== 2) throw new RangeError("PCM channels must be 1 or 2.");
  return Object.freeze({ ...format });
}

function invalidState(message: string): DOMException {
  return new DOMException(message, "InvalidStateError");
}

function clockValue(clock: () => number): number {
  const value = clock();
  if (!Number.isFinite(value)) throw new RangeError("PCM clock must return finite seconds.");
  return value;
}

/** In-place radix-2 FFT. The Hann window and channel power averaging happen outside it. */
function fft(real: Float64Array, imaginary: Float64Array): void {
  const size = real.length;
  for (let i = 1, j = 0; i < size; i++) {
    let bit = size >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [real[i], real[j]] = [real[j], real[i]];
      [imaginary[i], imaginary[j]] = [imaginary[j], imaginary[i]];
    }
  }
  for (let span = 2; span <= size; span *= 2) {
    const cosine = Math.cos(-2 * Math.PI / span), sine = Math.sin(-2 * Math.PI / span);
    for (let start = 0; start < size; start += span) {
      let wr = 1, wi = 0;
      for (let j = 0; j < span / 2; j++) {
        const a = start + j, b = a + span / 2;
        const re = real[b] * wr - imaginary[b] * wi, im = real[b] * wi + imaginary[b] * wr;
        real[b] = real[a] - re; imaginary[b] = imaginary[a] - im;
        real[a] += re; imaginary[a] += im;
        const next = wr * cosine - wi * sine;
        wi = wr * sine + wi * cosine; wr = next;
      }
    }
  }
}

class Channel implements PcmChannel {
  readonly writable: WritableStream<Uint8Array>;
  private readonly capacity: number;
  private readonly values: Float32Array;
  private readonly times: Float64Array;
  private readonly windowFrames: number;
  private readonly analysis: Float32Array;
  private readonly window: Float64Array;
  private readonly real: Float64Array;
  private readonly imaginary: Float64Array;
  private readonly windowPower: number;
  private readonly scratch = new DataView(new ArrayBuffer(4));
  private head = 0;
  private size = 0;
  private pending = new Uint8Array(0);
  private pendingAt = 0;
  private acceptedEnd = -Infinity;
  private ended = false;
  private disposed = false;
  private writtenFrames = 0;
  private acceptedFrames = 0;
  private overlapFrames = 0;
  private droppedFrames = 0;
  private discardedBytes = 0;
  private stopReader: ((reason: unknown) => void) | undefined;

  constructor(private readonly format: Readonly<PcmFormat>, private readonly clock: () => number, seconds: number) {
    this.capacity = Math.max(1, Math.floor(format.sampleRate * seconds));
    this.values = new Float32Array(this.capacity * format.channels);
    this.times = new Float64Array(this.capacity);
    this.windowFrames = Math.max(2, Math.round(format.sampleRate * WINDOW_SECONDS));
    this.analysis = new Float32Array(this.windowFrames * format.channels);
    this.window = Float64Array.from({ length: this.windowFrames }, (_, i) => .5 - .5 * Math.cos(2 * Math.PI * i / (this.windowFrames - 1)));
    this.windowPower = this.window.reduce((sum, value) => sum + value * value, 0);
    const transformSize = 2 ** Math.ceil(Math.log2(this.windowFrames));
    this.real = new Float64Array(transformSize); this.imaginary = new Float64Array(transformSize);
    this.writable = new WritableStream<Uint8Array>({
      write: bytes => this.write(bytes),
      close: () => this.close(),
      abort: reason => this.terminate(reason),
    });
  }

  write(bytes: Uint8Array, timing?: PcmTiming): void {
    if (this.ended || this.disposed) throw invalidState("PCM channel is closed.");
    if (!(bytes instanceof Uint8Array)) throw new TypeError("PCM writes require Uint8Array bytes.");
    if (timing && !Number.isFinite(timing.at)) throw new RangeError("PCM timestamp must be finite seconds.");
    if (!bytes.byteLength) return;
    const now = clockValue(this.clock), rate = this.format.sampleRate;
    if (this.pending.length && timing && Math.abs(timing.at - this.pendingAt) * rate > EPSILON_FRAMES) throw new RangeError("A fragmented PCM frame must keep its original timestamp; omit timing on continuation bytes.");
    const start = this.pending.length ? this.pendingAt : timing?.at ?? Math.max(now, this.acceptedEnd);
    const sampleBytes = this.format.encoding === "s16le" ? 2 : 4, frameBytes = sampleBytes * this.format.channels;
    const carry = this.pending, totalBytes = carry.length + bytes.length, frames = Math.floor(totalBytes / frameBytes);
    const byteAt = (offset: number) => offset < carry.length ? carry[offset] : bytes[offset - carry.length];
    const remainder = totalBytes - frames * frameBytes;
    this.pending = Uint8Array.from({ length: remainder }, (_, i) => byteAt(frames * frameBytes + i));
    this.pendingAt = start + frames / rate;
    if (!frames) return;
    this.writtenFrames += frames;
    const overlap = Math.min(frames, Math.max(0, Math.ceil((this.acceptedEnd - start) * rate - EPSILON_FRAMES)));
    this.overlapFrames += overlap;
    this.acceptedEnd = Math.max(this.acceptedEnd, start + frames / rate);
    const accepted = frames - overlap;
    this.acceptedFrames += accepted;
    this.prune(now - WINDOW_SECONDS);
    const overflow = Math.max(0, this.size + accepted - this.capacity);
    this.droppedFrames += overflow;
    const oldDrop = Math.min(overflow, this.size);
    this.head = (this.head + oldDrop) % this.capacity; this.size -= oldDrop;
    const first = overlap + overflow - oldDrop;
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    for (let frame = first; frame < frames; frame++) {
      const target = (this.head + this.size) % this.capacity;
      this.times[target] = start + frame / rate;
      for (let channel = 0; channel < this.format.channels; channel++) {
        const offset = frame * frameBytes + channel * sampleBytes;
        let sample: number;
        if (offset >= carry.length) sample = sampleBytes === 2 ? view.getInt16(offset - carry.length, true) / 32768 : view.getFloat32(offset - carry.length, true);
        else {
          for (let i = 0; i < sampleBytes; i++) this.scratch.setUint8(i, byteAt(offset + i));
          sample = sampleBytes === 2 ? this.scratch.getInt16(0, true) / 32768 : this.scratch.getFloat32(0, true);
        }
        this.values[target * this.format.channels + channel] = Number.isFinite(sample) ? Math.max(-1, Math.min(1, sample)) : 0;
      }
      this.size++;
    }
  }

  private prune(before: number): void {
    while (this.size && this.times[this.head] < before - EPSILON_FRAMES / this.format.sampleRate) {
      this.head = (this.head + 1) % this.capacity; this.size--;
    }
  }

  sample(now: number): SignalFrame {
    if (this.disposed) return silentSignal();
    if (this.ended && now >= this.acceptedEnd) { this.head = 0; this.size = 0; return silentSignal(); }
    this.prune(now - WINDOW_SECONDS);
    if (!this.size || this.times[this.head] > now) return silentSignal();
    this.analysis.fill(0);
    const rate = this.format.sampleRate, channels = this.format.channels, firstTime = now - (this.windowFrames - 1) / rate;
    let present = false;
    for (let i = 0; i < this.size; i++) {
      const source = (this.head + i) % this.capacity, time = this.times[source];
      if (time > now + EPSILON_FRAMES / rate) break;
      const target = Math.round((time - firstTime) * rate);
      if (target < 0 || target >= this.windowFrames) continue;
      present = true;
      for (let channel = 0; channel < channels; channel++) this.analysis[target * channels + channel] = this.values[source * channels + channel];
    }
    if (!present) return silentSignal();
    const power = this.analysis.reduce((sum, value) => sum + value * value, 0) / this.analysis.length;
    if (!power) return { ...silentSignal(), active: true };
    const bands = [0, 0, 0];
    for (let channel = 0; channel < channels; channel++) {
      this.real.fill(0); this.imaginary.fill(0);
      for (let i = 0; i < this.windowFrames; i++) this.real[i] = this.analysis[i * channels + channel] * this.window[i];
      fft(this.real, this.imaginary);
      for (let bin = 1; bin <= this.real.length / 2; bin++) {
        const frequency = bin * rate / this.real.length;
        if (frequency < 80) continue;
        if (frequency > 6500) break;
        const band = frequency < 350 ? 0 : frequency < 2000 ? 1 : 2;
        const sided = bin === this.real.length / 2 ? 1 : 2;
        bands[band] += sided * (this.real[bin] ** 2 + this.imaginary[bin] ** 2) / (this.real.length * this.windowPower * channels);
      }
    }
    return { active: true, rms: Math.min(1, Math.sqrt(power)), low: Math.min(1, Math.sqrt(bands[0])), mid: Math.min(1, Math.sqrt(bands[1])), high: Math.min(1, Math.sqrt(bands[2])) };
  }

  reset(): void {
    this.discardedBytes += this.pending.length;
    this.head = 0; this.size = 0; this.pending = new Uint8Array(0); this.pendingAt = 0; this.acceptedEnd = -Infinity;
    this.analysis.fill(0); this.real.fill(0); this.imaginary.fill(0);
  }

  close(): void {
    if (this.ended) return;
    this.ended = true; this.discardedBytes += this.pending.length; this.pending = new Uint8Array(0);
    this.stopReader?.(invalidState("PCM channel was closed while consuming a stream."));
  }

  private terminate(reason: unknown): void {
    this.reset(); this.ended = true; this.stopReader?.(reason);
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true; this.terminate(new DOMException("PCM audio was disposed.", "AbortError"));
    this.values.fill(0); this.times.fill(0);
  }

  stats(): PcmChannelStats {
    const now = clockValue(this.clock);
    if (this.ended && now >= this.acceptedEnd) { this.head = 0; this.size = 0; }
    else this.prune(now - WINDOW_SECONDS);
    return { format: this.format, bufferedFrames: this.size, bufferedSeconds: this.size / this.format.sampleRate, pendingBytes: this.pending.length, writtenFrames: this.writtenFrames, acceptedFrames: this.acceptedFrames, overlapFrames: this.overlapFrames, droppedFrames: this.droppedFrames, discardedBytes: this.discardedBytes, closed: this.ended };
  }

  async consume(readable: ReadableStream<Uint8Array>, options: { signal?: AbortSignal } = {}): Promise<void> {
    if (this.ended || this.disposed) throw invalidState("PCM channel is closed.");
    if (this.stopReader) throw invalidState("PCM channel already has a stream consumer.");
    const signal = options.signal;
    if (signal?.aborted) { const reason: unknown = signal.reason; this.terminate(reason); throw reason; }
    const reader = readable.getReader();
    let stopped = false, reason: unknown;
    const stop = (value: unknown) => {
      if (stopped) return;
      stopped = true; reason = value; void reader.cancel(value).catch(() => {});
    };
    this.stopReader = stop;
    const abort = () => { const value: unknown = signal?.reason; this.terminate(value ?? new DOMException("PCM stream was aborted.", "AbortError")); };
    signal?.addEventListener("abort", abort, { once: true });
    try {
      while (true) {
        const chunk = await reader.read();
        if (stopped) throw reason;
        if (chunk.done) {
          this.ended = true; this.discardedBytes += this.pending.length; this.pending = new Uint8Array(0);
          return;
        }
        this.write(chunk.value);
      }
    } catch (error: unknown) {
      if (!stopped) this.reset();
      this.ended = true;
      void reader.cancel(error).catch(() => {});
      throw error;
    }
    finally { signal?.removeEventListener("abort", abort); if (this.stopReader === stop) this.stopReader = undefined; reader.releaseLock(); }
  }
}

/** Raw PCM queues only: this module requests no microphone, creates no playback graph and owns no timer. */
export function createPcmAudio(options: PcmAudioOptions): PcmAudio {
  const inputFormat = validateFormat(options.inputFormat), outputFormat = validateFormat(options.outputFormat);
  const seconds = options.maxBufferedSeconds ?? 2;
  if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 60) throw new RangeError("maxBufferedSeconds must be greater than zero and at most 60.");
  const clock = options.clock ?? (() => performance.now() / 1000);
  const input = new Channel(inputFormat, clock, seconds), output = new Channel(outputFormat, clock, seconds);
  let disposed = false;
  return {
    input, output,
    sample: () => {
      if (disposed) return { input: silentSignal(), output: silentSignal() };
      const now = clockValue(clock); return { input: input.sample(now), output: output.sample(now) };
    },
    dispose: () => { if (!disposed) { disposed = true; input.dispose(); output.dispose(); } },
  };
}
