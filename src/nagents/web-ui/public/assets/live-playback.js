// Continuous mono PCM16/24kHz playout. Network delivery never schedules a new
// AudioBufferSource or resets a resampler at a packet boundary.
const PCM_RATE = 24000;
const CAPACITY = PCM_RATE; // One second, independent of WebSocket packet size.
const MIN_TARGET = 0.04;
const MAX_TARGET = 0.16;
const RESERVE_DECAY = 0.002; // Give back two milliseconds per second of clean delivery.
const REALTIME_TARGET = .28;
const REALTIME_DECAY = .00025;
const REALTIME_LIMIT = PCM_RATE * .32;

class LivePlayback extends AudioWorkletProcessor {
  constructor() {
    super();
    this.samples = new Int16Array(CAPACITY);
    // Only downsampling needs an anti-alias filter. Keep its delay line across
    // packets; a packet-local filter would itself create periodic transients.
    this.filter = new Float64Array(sampleRate < PCM_RATE ? 48 : 0);
    this.history = new Float64Array(this.filter.length);
    this.filterIndex = 0;
    this.primed = false;
    this.nextReady = false;
    this.a = this.b = 0;
    if (this.filter.length) {
      const cutoff = sampleRate * 0.45 / PCM_RATE;
      let sum = 0;
      for (let i = 0; i < this.filter.length; i++) {
        const x = i - (this.filter.length - 1) / 2;
        const window = 0.42 - 0.5 * Math.cos(2 * Math.PI * i / (this.filter.length - 1)) + 0.08 * Math.cos(4 * Math.PI * i / (this.filter.length - 1));
        this.filter[i] = Math.sin(2 * Math.PI * cutoff * x) / (Math.PI * x) * window;
        sum += this.filter[i];
      }
      for (let i = 0; i < this.filter.length; i++) this.filter[i] /= sum;
    }
    this.read = 0;
    this.count = 0;
    this.phase = 0;
    this.playing = false;
    this.epoch = 0;
    this.firstArrival = -1;
    this.lastArrival = -1;
    this.packetDuration = 0.02;
    this.jitter = 0;
    this.target = MIN_TARGET;
    this.meanDepth = 0;
    this.ratio = 1;
    this.lastValue = 0;
    this.fadeFrom = 0;
    this.fade = 0;
    this.report = new Int16Array(480);
    this.reportSize = 0;
    this.reportAt = 0;
    this.reportUntil = 0;
    this.reportSourceStart = 0;
    this.nextHealth = 0;
    this.healthPending = false;
    this.playedPending = false;
    this.receivedSamples = 0;
    this.playedSamples = 0;
    this.underruns = 0;
    this.droppedSamples = 0;
    this.interruptedSamples = 0;
    this.peakBufferedSamples = 0;
    this.realtime = false;
    this.latencyRecoveries = 0;
    this.silenceRefills = 0;
    this.quietFrames = 0;
    this.lastUnderrunAt = this.lastRecoveryAt = -1;
    this.port.onmessage = ({ data }) => {
      if (data?.type === "reset") { this.reset(data.epoch); return; }
      if (data?.type === "barrier") { this.fenceReports(data.epoch); return; }
      if (data?.type === "played-ack" && data.epoch === this.epoch) { this.playedPending = false; return; }
      if (data?.type === "health") { this.health(true); return; }
      if (data?.type === "health-ack" && data.epoch === this.epoch) { this.healthPending = false; return; }
      if (data?.type === "mode" && data.epoch === this.epoch && typeof data.realtime === "boolean") {
        this.realtime = data.realtime;
        return;
      }
      if (data?.type !== "pcm" || !(data.buffer instanceof ArrayBuffer) || data.epoch !== this.epoch) return;
      const length = data.buffer.byteLength;
      if (!length || length > 48000 || length % 2) return;
      this.append(data.buffer);
    };
  }

  reset(epoch) {
    if (!Number.isSafeInteger(epoch)) return;
    this.interruptedSamples += this.count;
    this.epoch = epoch;
    this.samples.fill(0); this.history.fill(0); this.filterIndex = 0; this.primed = false;
    this.count = this.read = this.phase = this.reportSize = 0;
    this.playing = false;
    this.firstArrival = this.lastArrival = -1;
    this.meanDepth = this.lastValue = this.fadeFrom = this.fade = 0;
    this.quietFrames = 0;
    this.ratio = 1;
    this.healthPending = this.playedPending = false;
    this.health();
  }

  fenceReports(epoch) {
    if (!Number.isSafeInteger(epoch)) return;
    this.epoch = epoch;
    this.reportSize = 0;
    this.healthPending = this.playedPending = false;
    this.health();
  }

  append(buffer) {
    const now = currentTime;
    const incoming = buffer.byteLength / 2;
    if (this.lastArrival >= 0) {
      const interval = now - this.lastArrival;
      const relaxedTarget = Math.max(MIN_TARGET, this.target - interval * (this.realtime ? REALTIME_DECAY : RESERVE_DECAY));
      // Learn short transport/render-message variation. A pause between spoken
      // replies is not network jitter, nor is an unpaced API audio batch.
      if (interval < 0.25 && this.packetDuration <= 0.06) {
        this.jitter += (Math.abs(interval - this.packetDuration) - this.jitter) * 0.08;
        // Averages alone learn a 70–150ms stall too slowly. Remember its peak
        // immediately so the next re-prime can absorb repeated delivery gaps.
        // This cannot recover audio already missing during the first stall.
        const late = Math.max(0, interval - this.packetDuration);
        this.target = Math.min(this.realtime ? REALTIME_TARGET : MAX_TARGET, Math.max(relaxedTarget, MIN_TARGET + late, MIN_TARGET + 3 * this.jitter));
      } else {
        this.target = relaxedTarget;
      }
    }
    this.lastArrival = now;
    this.packetDuration = incoming / PCM_RATE;
    if (!this.count) this.firstArrival = now;
    const recover = this.realtime && this.count + incoming > REALTIME_LIMIT;
    const limit = recover ? Math.ceil(Math.max(.08, this.target) * PCM_RATE) : CAPACITY;
    const excess = Math.max(0, this.count + incoming - limit);
    let incomingOffset = 0;
    if (excess) {
      const queued = Math.min(this.count, excess);
      this.read = (this.read + queued) % CAPACITY;
      this.count -= queued;
      incomingOffset = excess - queued;
      this.droppedSamples += excess;
      if (recover) { this.latencyRecoveries++; this.lastRecoveryAt = now; }
      this.phase = 0; this.primed = false;
      this.quietFrames = 0;
      this.history.fill(0); this.filterIndex = 0;
      this.reportSize = 0;
      this.fadeFrom = this.lastValue; this.fade = 1;
    }
    const bytes = new DataView(buffer);
    for (let i = incomingOffset; i < incoming; i++) this.samples[(this.read + this.count + i - incomingOffset) % CAPACITY] = bytes.getInt16(i * 2, true);
    this.count += incoming - incomingOffset;
    this.receivedSamples += incoming;
    this.peakBufferedSamples = Math.max(this.peakBufferedSamples, this.count);
  }

  filtered(value) {
    if (!this.filter.length) return value;
    this.history[this.filterIndex] = value;
    value = 0;
    for (let tap = 0; tap < this.filter.length; tap++) value += this.filter[tap] * this.history[(this.filterIndex - tap + this.filter.length) % this.filter.length];
    this.filterIndex = (this.filterIndex + 1) % this.filter.length;
    return Math.max(-32768, Math.min(32767, value));
  }

  health(force = false) {
    if (this.healthPending && !force) return;
    this.healthPending = true;
    this.port.postMessage({ type: "health", epoch: this.epoch,
      receivedSamples: this.receivedSamples, playedSamples: this.playedSamples,
      underruns: this.underruns, droppedSamples: this.droppedSamples,
      bufferedMs: this.count / PCM_RATE * 1000, peakBufferedMs: this.peakBufferedSamples / PCM_RATE * 1000,
      targetMs: this.target * 1000, sampleRate, correctionPpm: (this.ratio - 1) * 1000000,
      renderedAt: currentTime, lastUnderrunAt: this.lastUnderrunAt,
      latencyRecoveries: this.latencyRecoveries, lastRecoveryAt: this.lastRecoveryAt,
      interruptedSamples: this.interruptedSamples, silenceRefills: this.silenceRefills });
    this.nextHealth = currentTime + 1;
  }

  reportSample(value, at) {
    if (!this.reportSize) {
      this.reportAt = at;
      // Absolute coordinates in the accepted PCM stream survive resets and
      // skipped telemetry reports. Queue removals remain explicitly accounted.
      this.reportSourceStart = this.playedSamples + this.droppedSamples + this.interruptedSamples;
    }
    this.reportUntil = at;
    this.report[this.reportSize++] = value;
    if (this.reportSize === this.report.length) this.flushReport();
  }

  flushReport() {
    if (this.reportSize && !this.playedPending) {
      const buffer = this.reportSize === this.report.length ? this.report.buffer : this.report.slice(0, this.reportSize).buffer;
      this.playedPending = true;
      this.port.postMessage({ type: "played", epoch: this.epoch, at: this.reportAt,
        until: this.reportUntil, sourceStart: this.reportSourceStart, buffer }, [buffer]);
      this.report = new Int16Array(480);
    }
    // One in-flight report plus the latest working frame bounds UI telemetry.
    // A stalled UI skips old visual frames; audible PCM is never dropped here.
    this.reportSize = 0;
  }

  process(_inputs, outputs) {
    const output = outputs[0]?.[0];
    if (!output) return true;
    // A learned target does not refill a stream whose producer runs slightly
    // behind the speaker. Rebuild only after sustained digital silence; keep
    // every queued sample, and bound the pause by the ordinary prime timeout.
    // This leaves voiced PCM and its playback rate untouched.
    if (this.realtime && this.playing && this.count > 0 &&
        this.quietFrames >= sampleRate * .06 && this.target * PCM_RATE - this.count > PCM_RATE * .02) {
      this.playing = false;
      this.firstArrival = currentTime;
      this.quietFrames = 0;
      this.silenceRefills++;
    }
    if (!this.playing && this.count > 0 &&
        (this.count >= this.target * PCM_RATE || currentTime - this.firstArrival >= this.target)) {
      this.playing = true;
      this.fadeFrom = this.lastValue; this.fade = 1;
      this.meanDepth = 0;
    }
    if (this.playing) {
      // Correct small source/device clock drift without periodically deleting or
      // duplicating entire 20ms packets. The slow servo is capped at 0.1% (~1.7 cents).
      const center = Math.max(0.01, this.target - Math.min(this.packetDuration, 0.04) / 2) * PCM_RATE;
      this.meanDepth += (this.count - center - this.meanDepth) * output.length / (sampleRate * 5);
      this.ratio = 1 + Math.max(-0.001, Math.min(0.001, this.meanDepth / (PCM_RATE * 10)));
    }
    const step = PCM_RATE / sampleRate * this.ratio;
    const fadeStep = 1 / (sampleRate * 0.004);
    for (let i = 0; i < output.length; i++) {
      let value = 0;
      if (this.playing && this.count > 0) {
        if (!this.primed) {
          this.a = this.filtered(this.samples[this.read]);
          this.nextReady = this.count > 1;
          this.b = this.nextReady ? this.filtered(this.samples[(this.read + 1) % CAPACITY]) : this.a;
          this.primed = true;
        }
        if (!this.nextReady && this.count > 1) {
          this.b = this.filtered(this.samples[(this.read + 1) % CAPACITY]); this.nextReady = true;
        }
        value = (this.a + (this.b - this.a) * this.phase) / 32768;
        this.phase += step;
        while (this.phase >= 1 && this.count > 0) {
          this.reportSample(this.a, currentTime + i / sampleRate);
          this.read = (this.read + 1) % CAPACITY;
          this.count--; this.playedSamples++; this.phase--;
          this.a = this.b;
          this.nextReady = this.count > 1;
          this.b = this.nextReady ? this.filtered(this.samples[(this.read + 1) % CAPACITY]) : this.a;
        }
      } else if (this.playing) {
        this.playing = false; this.underruns++;
        this.lastUnderrunAt = currentTime + i / sampleRate;
        this.firstArrival = -1;
        this.flushReport();
        this.phase = 0;
        this.primed = false; this.history.fill(0); this.filterIndex = 0;
        this.fadeFrom = this.lastValue; this.fade = 1;
      }
      if (this.fade > 0) {
        value = value * (1 - this.fade) + this.fadeFrom * this.fade;
        this.fade = Math.max(0, this.fade - fadeStep);
      }
      output[i] = this.lastValue = value;
      this.quietFrames = this.playing && Math.abs(value) <= 2 / 32768 ? this.quietFrames + 1 : 0;
    }
    // The node is mono. Extra requested channels receive the same playout.
    for (let channel = 1; channel < outputs[0].length; channel++) outputs[0][channel].set(output);
    if (currentTime >= this.nextHealth) this.health();
    return true;
  }
}

registerProcessor("ngn-live-playback", LivePlayback);
